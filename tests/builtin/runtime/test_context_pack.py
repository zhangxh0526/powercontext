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

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest

from powercontext.builtin.runtime.application import ScopedContextApplication
from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    apply_context_pack_delta,
)
from powercontext.builtin.runtime.models import PrepareContextRequest, PreparedContext


def _delta() -> ContextPackDelta:
    base = ContextPack.build((b"alpha", b"beta"))
    return ContextPackDelta.between(base, ContextPack.build((b"alpha", b"beta", b"gamma")))


@pytest.mark.parametrize("field", ["schema", "items", "digest", "byte_count"])
def test_pack_rejects_missing_fields(field: str) -> None:
    payload = json.loads(ContextPack.build((b"alpha",)).to_bytes())
    del payload[field]
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps(payload).encode())


@pytest.mark.parametrize("field", ["schema", "base_digest", "target_digest", "prefix_items", "appended"])
def test_delta_rejects_missing_fields(field: str) -> None:
    payload = json.loads(_delta().to_bytes())
    del payload[field]
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(json.dumps(payload).encode())


def test_wire_decoders_reject_additional_fields() -> None:
    pack_payload = json.loads(ContextPack.build((b"alpha",)).to_bytes())
    pack_payload["unexpected"] = None
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps(pack_payload).encode())

    delta_payload = json.loads(_delta().to_bytes())
    delta_payload["byte_count"] = _delta().byte_count
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(json.dumps(delta_payload).encode())


def test_wire_decoders_accept_noncanonical_key_order_and_whitespace() -> None:
    pack = ContextPack.build((b"alpha", "海".encode()))
    pack_payload = json.loads(pack.to_bytes())
    reordered_pack = {key: pack_payload[key] for key in reversed(tuple(pack_payload))}
    assert ContextPack.from_bytes(json.dumps(reordered_pack, indent=2).encode()) == pack

    delta = _delta()
    delta_payload = json.loads(delta.to_bytes())
    reordered_delta = {key: delta_payload[key] for key in reversed(tuple(delta_payload))}
    assert ContextPackDelta.from_bytes(json.dumps(reordered_delta, indent=2).encode()) == delta


@pytest.mark.parametrize("encoded", ["not base64!", "Zg", "Zg==\n", "Zh==", "-_8=", "海", 1, None])
def test_pack_rejects_invalid_base64(encoded: object) -> None:
    payload = json.loads(ContextPack.build((b"alpha",)).to_bytes())
    payload["items"] = [encoded]
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps(payload).encode())


@pytest.mark.parametrize("encoded", ["not base64!", "Zg", "Zg==\n", "Zh==", "-_8=", "海", 1, None])
def test_delta_rejects_invalid_base64(encoded: object) -> None:
    payload = json.loads(_delta().to_bytes())
    payload["appended"] = [encoded]
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(json.dumps(payload).encode())


def test_pack_byte_count_remains_exact_across_decimal_widths() -> None:
    three_digits = ContextPack.build((b"x" * 630,))
    four_digits = ContextPack.build((b"x" * 650,))
    assert len(str(three_digits.byte_count)) == 3
    assert len(str(four_digits.byte_count)) == 4
    for pack in (three_digits, four_digits):
        assert pack.byte_count == len(pack.to_bytes())
        assert ContextPack.from_bytes(pack.to_bytes()) == pack


def test_pack_rejects_invalid_or_inaccurate_byte_count() -> None:
    pack = ContextPack.build((b"alpha",))
    invalid_counts = (True, 1.5, "100", -1, 0, pack.byte_count - 1, pack.byte_count + 1)
    for byte_count in invalid_counts:
        payload = json.loads(pack.to_bytes())
        payload["byte_count"] = byte_count
        with pytest.raises(ContextPackError):
            ContextPack.from_bytes(json.dumps(payload).encode())


@pytest.mark.parametrize("chunk_bytes", [0, -1, True, 1.5, "512"])
def test_from_content_requires_a_strictly_positive_integer_limit(chunk_bytes: object) -> None:
    with pytest.raises(ContextPackError):
        ContextPack.from_content("content", chunk_bytes=cast(Any, chunk_bytes))


def test_from_content_chunks_only_at_utf8_codepoint_boundaries() -> None:
    content = "A海🙂B"
    pack = ContextPack.from_content(content, chunk_bytes=4)
    assert all(0 < len(item) <= 4 for item in pack.items)
    assert all(item.decode("utf-8") for item in pack.items)
    assert b"".join(pack.items).decode("utf-8") == content
    with pytest.raises(ContextPackError):
        ContextPack.from_content("🙂", chunk_bytes=3)


def test_apply_accepts_every_valid_prefix() -> None:
    base = ContextPack.build((b"a", b"b", b"c"))
    target = ContextPack.build((b"a", b"b", b"c", b"d"))
    restored_base = ContextPack.from_bytes(base.to_bytes())
    for prefix_items in range(len(base.items) + 1):
        delta = ContextPackDelta(base.digest, target.digest, prefix_items, target.items[prefix_items:])
        restored_delta = ContextPackDelta.from_bytes(delta.to_bytes())
        assert apply_context_pack_delta(restored_base, restored_delta) == target


def test_invalid_direct_delta_fails_at_use_rather_than_construction() -> None:
    base = ContextPack.build((b"alpha",))
    delta = ContextPackDelta(base.digest, base.digest, -1, ())
    with pytest.raises(ContextPackError):
        delta.to_bytes()
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, delta)


def test_empty_content_and_delta_round_trip() -> None:
    empty = ContextPack.from_content("")
    assert empty.items == ()
    assert ContextPack.from_bytes(empty.to_bytes()) == empty
    delta = ContextPackDelta.between(empty, empty)
    assert delta.prefix_items == 0
    assert delta.appended == ()
    rebuilt = apply_context_pack_delta(empty, ContextPackDelta.from_bytes(delta.to_bytes()))
    assert rebuilt == empty
    assert b"".join(rebuilt.items) == b""


def test_prepare_incremental_reuses_prepare_and_maps_none_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        application = object.__new__(ScopedContextApplication)
        request = PrepareContextRequest(query="empty")
        calls: list[PrepareContextRequest] = []

        async def fake_prepare(received: PrepareContextRequest) -> PreparedContext:
            calls.append(received)
            return PreparedContext(status="empty", content=None, content_bytes=0)

        monkeypatch.setattr(application, "prepare", fake_prepare)
        first = await application.prepare_incremental(request)
        assert isinstance(first, ContextPack)
        assert first.items == ()
        second = await application.prepare_incremental(request, first)
        assert isinstance(second, ContextPackDelta)
        rebuilt = apply_context_pack_delta(first, second)
        assert b"".join(rebuilt.items) == b""
        assert calls == [request, request]

    asyncio.run(scenario())
