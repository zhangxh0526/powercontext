# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Canonical context packs and prefix-reuse deltas."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from typing import cast

_PACK_SCHEMA = "powercontext.context-pack.v1"
_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
_PACK_FIELDS = frozenset({"schema", "items", "digest", "byte_count"})
_DELTA_FIELDS = frozenset({"schema", "base_digest", "target_digest", "prefix_items", "appended"})
_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class ContextPackError(ValueError):
    """A context pack or delta violates the wire protocol."""


def _canonical_json(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ContextPackError("duplicate-json-field")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ContextPackError("invalid-json-number")


def _decode_object(data: bytes, fields: frozenset[str]) -> dict[str, object]:
    if not isinstance(data, bytes):
        raise ContextPackError("wire-must-be-bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContextPackError("wire-must-be-utf8") from error
    try:
        payload = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_json_constant)
    except ContextPackError:
        raise
    except (ValueError, RecursionError) as error:
        raise ContextPackError("invalid-json") from error
    if not isinstance(payload, dict):
        raise ContextPackError("wire-must-be-object")
    if set(payload) != fields:
        raise ContextPackError("wire-fields")
    return cast(dict[str, object], payload)


def _validate_schema(payload: dict[str, object], schema: str) -> None:
    if payload["schema"] != schema:
        raise ContextPackError("wire-schema")


def _validate_digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
        raise ContextPackError("wire-digest")
    return value


def _validate_integer(value: object, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ContextPackError("wire-integer")
    return value


def _normalize_items(value: object) -> tuple[bytes, ...]:
    if not isinstance(value, tuple) or any(not isinstance(item, bytes) for item in value):
        raise ContextPackError("items-must-be-bytes-tuple")
    return cast(tuple[bytes, ...], value)


def _decode_items(value: object) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError("wire-items")
    items: list[bytes] = []
    for encoded in value:
        if not isinstance(encoded, str):
            raise ContextPackError("wire-base64")
        try:
            ascii_encoded = encoded.encode("ascii")
        except UnicodeEncodeError as error:
            raise ContextPackError("wire-base64") from error
        try:
            item = base64.b64decode(ascii_encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ContextPackError("wire-base64") from error
        if base64.b64encode(item) != ascii_encoded:
            raise ContextPackError("wire-base64")
        items.append(item)
    return tuple(items)


def _encode_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _unsigned_pack(items: tuple[bytes, ...]) -> dict[str, object]:
    return {"schema": _PACK_SCHEMA, "items": _encode_items(items)}


def _pack_digest(items: tuple[bytes, ...]) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(_unsigned_pack(items))).hexdigest()}"


def _pack_bytes(items: tuple[bytes, ...], digest: str, byte_count: int) -> bytes:
    return _canonical_json({**_unsigned_pack(items), "digest": digest, "byte_count": byte_count})


def _stable_pack_byte_count(items: tuple[bytes, ...], digest: str) -> int:
    byte_count = 0
    while True:
        actual = len(_pack_bytes(items, digest, byte_count))
        if actual == byte_count:
            return byte_count
        byte_count = actual


def _delta_values(
    base_digest: object,
    target_digest: object,
    prefix_items: object,
    appended: object,
) -> tuple[str, str, int, tuple[bytes, ...]]:
    return (
        _validate_digest(base_digest),
        _validate_digest(target_digest),
        _validate_integer(prefix_items, minimum=0),
        _normalize_items(appended),
    )


@dataclass(frozen=True)
class ContextPack:
    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> ContextPack:
        normalized = _normalize_items(items)
        digest = _pack_digest(normalized)
        return cls(normalized, digest, _stable_pack_byte_count(normalized, digest))

    @classmethod
    def from_content(cls, content: str, *, chunk_bytes: int = 512) -> ContextPack:
        if type(chunk_bytes) is not int or chunk_bytes <= 0:
            raise ContextPackError("chunk-bytes")
        if not isinstance(content, str):
            raise ContextPackError("content-must-be-text")
        if not content:
            return cls.build(())
        items: list[bytes] = []
        current = bytearray()
        try:
            for character in content:
                encoded = character.encode("utf-8")
                if len(encoded) > chunk_bytes:
                    raise ContextPackError("chunk-codepoint")
                if current and len(current) + len(encoded) > chunk_bytes:
                    items.append(bytes(current))
                    current.clear()
                current.extend(encoded)
        except UnicodeEncodeError as error:
            raise ContextPackError("content-must-be-utf8") from error
        if current:
            items.append(bytes(current))
        return cls.build(tuple(items))

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        payload = _decode_object(data, _PACK_FIELDS)
        _validate_schema(payload, _PACK_SCHEMA)
        items = _decode_items(payload["items"])
        digest = _validate_digest(payload["digest"])
        byte_count = _validate_integer(payload["byte_count"], minimum=1)
        pack = cls.build(items)
        if digest != pack.digest:
            raise ContextPackError("pack-digest-mismatch")
        if byte_count != pack.byte_count:
            raise ContextPackError("pack-byte-count-mismatch")
        return pack

    def to_bytes(self) -> bytes:
        items = _normalize_items(self.items)
        digest = _validate_digest(self.digest)
        byte_count = _validate_integer(self.byte_count, minimum=1)
        if digest != _pack_digest(items):
            raise ContextPackError("pack-digest-mismatch")
        encoded = _pack_bytes(items, digest, byte_count)
        if len(encoded) != byte_count:
            raise ContextPackError("pack-byte-count-mismatch")
        return encoded


@dataclass(frozen=True)
class ContextPackDelta:
    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("delta-requires-packs")
        base_items = _normalize_items(base.items)
        target_items = _normalize_items(target.items)
        base_digest = _validate_digest(base.digest)
        target_digest = _validate_digest(target.digest)
        prefix_items = 0
        limit = min(len(base_items), len(target_items))
        while prefix_items < limit and base_items[prefix_items] == target_items[prefix_items]:
            prefix_items += 1
        return cls(base_digest, target_digest, prefix_items, target_items[prefix_items:])

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        payload = _decode_object(data, _DELTA_FIELDS)
        _validate_schema(payload, _DELTA_SCHEMA)
        appended = _decode_items(payload["appended"])
        base_digest, target_digest, prefix_items, appended = _delta_values(
            payload["base_digest"], payload["target_digest"], payload["prefix_items"], appended
        )
        return cls(base_digest, target_digest, prefix_items, appended)

    def to_bytes(self) -> bytes:
        base_digest, target_digest, prefix_items, appended = _delta_values(
            self.base_digest, self.target_digest, self.prefix_items, self.appended
        )
        return _canonical_json({
            "schema": _DELTA_SCHEMA,
            "base_digest": base_digest,
            "target_digest": target_digest,
            "prefix_items": prefix_items,
            "appended": _encode_items(appended),
        })

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    if not isinstance(base, ContextPack) or not isinstance(delta, ContextPackDelta):
        raise ContextPackError("delta-requires-packs")
    base_digest, target_digest, prefix_items, appended = _delta_values(
        delta.base_digest, delta.target_digest, delta.prefix_items, delta.appended
    )
    base = ContextPack.from_bytes(base.to_bytes())
    if base.digest != base_digest:
        raise ContextPackError("delta-base-mismatch")
    if prefix_items > len(base.items):
        raise ContextPackError("delta-prefix-out-of-bounds")
    target = ContextPack.build((*base.items[:prefix_items], *appended))
    if target.digest != target_digest:
        raise ContextPackError("delta-target-mismatch")
    return target


__all__ = ["ContextPack", "ContextPackDelta", "ContextPackError", "apply_context_pack_delta"]
