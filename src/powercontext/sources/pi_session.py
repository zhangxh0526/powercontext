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

"""Capture the visible events on one branch of a Pi v3 session."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator

from powercontext.errors import InvalidConnectorRunError
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
    ConnectorSubmissionStatus,
    Source,
    SourceMaterialization,
    SourceProjection,
    TextEvidence,
)

PI_EVENT_SOURCE_NAME = "pi-event"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"


class PiSessionInput(BaseModel):
    """One complete UTF-8 JSONL session and an optional selected leaf."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible Pi event and its complete original JSON record."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """Captured immutable evidence for one Pi session event."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventTextEvidenceProjection:
    """Expose a Pi event through the shared text-evidence capability."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.name,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


class PiEventSourceDefinition:
    """Resolve and read back captured Pi event evidence."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    version = "1"
    source_class = PiEventSource

    def __init__(self) -> None:
        projection = cast(SourceProjection[PiEventSource], PiEventTextEvidenceProjection())
        self.projections: tuple[SourceProjection[PiEventSource], ...] = (projection,)

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        return PiEventSource(
            name=_event_identity(value.session_id, value.event_id),
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


@dataclass(frozen=True, slots=True)
class _PiGraph:
    session_id: str
    records: dict[str, dict[str, JsonValue]]
    order: tuple[str, ...]
    parents: dict[str, str | None]


@dataclass(frozen=True, slots=True)
class _PiBranch:
    session_id: str
    records: tuple[dict[str, JsonValue], ...]
    events: tuple[PiEventInput, ...]


class _PiSessionCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["1"] = "1"
    events: dict[str, str] = Field(default_factory=dict)

    @field_validator("events")
    @classmethod
    def validate_digests(cls, value: dict[str, str]) -> dict[str, str]:
        for identity, digest in value.items():
            if not identity:
                raise ValueError("checkpoint event identities must be non-empty")  # noqa: TRY003
            if len(digest) != 64 or digest != digest.lower():
                raise ValueError("checkpoint event digests must be 64 lowercase hex characters")  # noqa: TRY003
            try:
                int(digest, 16)
            except ValueError as error:
                raise ValueError("checkpoint event digests must be hexadecimal") from error
        return value


class PiSessionConnector:
    """Incrementally capture the selected branch using a durable lifecycle checkpoint."""

    name = PI_SESSION_CONNECTOR_NAME
    version = "1"
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        previous = _checkpoint(session.checkpoint)
        content = await asyncio.to_thread(self._path.read_bytes)
        branch = _parse_pi_branch(PiSessionInput(content=content, leaf_id=self._leaf_id))
        current = {
            _event_identity(branch.session_id, cast(str, record["id"])): _record_digest(record)
            for record in branch.records
        }
        for identity, digest in current.items():
            imported_digest = previous.events.get(identity)
            if imported_digest is not None and imported_digest != digest:
                raise ValueError(f"Pi event {identity!r} was modified after import")  # noqa: TRY003

        imported = dict(previous.events)
        incomplete = False
        for event in branch.events:
            identity = _event_identity(event.session_id, event.event_id)
            if identity in imported:
                continue
            outcome = await session.submit(identity, PI_EVENT_SOURCE_NAME, event)
            if outcome.status is ConnectorSubmissionStatus.ACCEPTED:
                imported[identity] = current[identity]
            else:
                incomplete = True

        if incomplete:
            return ConnectorRunCompletion(status=ConnectorRunStatus.INCOMPLETE, checkpoint=session.checkpoint)
        checkpoint = _PiSessionCheckpoint(events=dict(sorted(imported.items())))
        return ConnectorRunCompletion(status=ConnectorRunStatus.COMPLETE, checkpoint=checkpoint.model_dump(mode="json"))


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Parse and validate the whole graph, then return visible events on the selected branch."""

    return _parse_pi_branch(value).events


def _parse_pi_branch(value: PiSessionInput) -> _PiBranch:
    graph = _parse_graph(_decode_records(value.content))
    leaf_id = value.leaf_id
    if leaf_id is None:
        if not graph.order:
            raise ValueError("Pi session must contain a node to select as the leaf")  # noqa: TRY003
        leaf_id = graph.order[-1]
    if leaf_id not in graph.records:
        raise ValueError(f"Pi session leaf {leaf_id!r} does not exist")  # noqa: TRY003

    selected: list[dict[str, JsonValue]] = []
    current: str | None = leaf_id
    while current is not None:
        selected.append(graph.records[current])
        current = graph.parents[current]
    selected.reverse()
    events = tuple(event for record in selected if (event := _visible_event(graph.session_id, record)) is not None)
    return _PiBranch(session_id=graph.session_id, records=tuple(selected), events=events)


def _decode_records(content: bytes) -> list[dict[str, JsonValue]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session content must be valid UTF-8") from error

    records: list[dict[str, JsonValue]] = []
    for line_number, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(
                line,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
                parse_float=_finite_json_float,
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f"Pi session line {line_number} must contain valid JSON") from error
        if not isinstance(record, dict):
            raise ValueError(f"Pi session line {line_number} must contain a JSON object")  # noqa: TRY003
        records.append(cast(dict[str, JsonValue], record))
    return records


def _parse_graph(records: list[dict[str, JsonValue]]) -> _PiGraph:
    if not records:
        raise ValueError("Pi session must start with a session header")  # noqa: TRY003
    header = records[0]
    session_id = header.get("id")
    if header.get("type") != "session" or type(header.get("version")) is not int or header.get("version") != 3:
        raise ValueError("Pi session header must have type 'session' and version 3")  # noqa: TRY003
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Pi session header must have a non-empty string id")  # noqa: TRY003

    nodes: dict[str, dict[str, JsonValue]] = {}
    parents: dict[str, str | None] = {}
    order: list[str] = []
    for record in records[1:]:
        event_id = record.get("id")
        event_type = record.get("type")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("Pi session nodes must have a non-empty string id")  # noqa: TRY003
        if event_id in nodes:
            raise ValueError(f"Pi session node id {event_id!r} is duplicated")  # noqa: TRY003
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("Pi session nodes must have a non-empty string type")  # noqa: TRY003
        if "parentId" not in record:
            raise ValueError(f"Pi session node {event_id!r} is missing parentId")  # noqa: TRY003
        parent_id = record["parentId"]
        if parent_id is not None and (not isinstance(parent_id, str) or parent_id not in nodes):
            raise ValueError(f"Pi session node {event_id!r} parent must be null or an earlier node")  # noqa: TRY003
        nodes[event_id] = record
        parents[event_id] = parent_id
        order.append(event_id)

    _validate_acyclic(parents)
    return _PiGraph(session_id=session_id, records=nodes, order=tuple(order), parents=parents)


def _validate_acyclic(parents: Mapping[str, str | None]) -> None:
    complete: set[str] = set()
    for event_id in parents:
        if event_id in complete:
            continue
        path: list[str] = []
        visiting: set[str] = set()
        current: str | None = event_id
        while current is not None and current not in complete:
            if current in visiting:
                raise ValueError("Pi session parent graph must not contain a cycle")  # noqa: TRY003
            visiting.add(current)
            path.append(current)
            current = parents[current]
        complete.update(path)


def _visible_event(session_id: str, record: dict[str, JsonValue]) -> PiEventInput | None:
    rendered = _render_record(record)
    if rendered is None:
        return None
    role, body = rendered
    return PiEventInput(
        session_id=session_id,
        event_id=cast(str, record["id"]),
        parent_id=cast(str | None, record["parentId"]),
        event_type=cast(str, record["type"]),
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _render_record(record: Mapping[str, object]) -> tuple[str, str] | None:
    event_type = record["type"]
    if event_type == "message":
        return _render_message(record.get("message"))
    if event_type in {"compaction", "branch_summary"}:
        summary = record.get("summary")
        if not isinstance(summary, str):
            raise ValueError(f"Pi {event_type} summary must be a string")  # noqa: TRY003
        return _visible_body(cast(str, event_type), summary)
    if event_type == "custom_message":
        return _visible_body("custom", _render_content(record.get("content"), include_tool_calls=False))
    return None


def _render_message(value: object) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        raise ValueError("Pi message record must contain a message object")  # noqa: TRY003
    role = value.get("role")
    if not isinstance(role, str) or not role:
        raise ValueError("Pi message must have a non-empty string role")  # noqa: TRY003
    if role == "toolResult":
        tool_name = _required_string(value.get("toolName"), "toolName")
        tool_call_id = _required_string(value.get("toolCallId"), "toolCallId")
        text = _render_content(value.get("content"), include_tool_calls=False) or ""
        return role, f"[tool_result {tool_name} {tool_call_id}] {text}"
    if role == "bashExecution":
        command = value.get("command")
        output = value.get("output")
        if not isinstance(command, str) or not isinstance(output, str):
            raise ValueError("Pi bashExecution command and output must be strings")  # noqa: TRY003
        return role, f"$ {command}\n{output}"
    return _visible_body(role, _render_content(value.get("content"), include_tool_calls=True))


def _render_content(content: object, *, include_tool_calls: bool) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise ValueError("Pi text content blocks must contain string text")  # noqa: TRY003
            parts.append(text)
        elif block_type == "toolCall" and include_tool_calls:
            parts.append(_render_tool_call(block))
    return "\n".join(parts) if parts else None


def _render_tool_call(block: Mapping[str, object]) -> str:
    name = _required_string(block.get("name"), "toolCall name")
    call_id = _required_string(block.get("id"), "toolCall id")
    if "arguments" not in block:
        raise ValueError("Pi toolCall block must contain arguments")  # noqa: TRY003
    try:
        arguments = json.dumps(
            block["arguments"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Pi toolCall arguments must be finite JSON") from error
    return f"[tool_call {name} {call_id}] {arguments}"


def _visible_body(role: str, body: str | None) -> tuple[str, str] | None:
    if body is None or not body.strip():
        return None
    return role, body


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Pi {field} must be a non-empty string")  # noqa: TRY003
    return value


def _unique_json_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    value: dict[str, JsonValue] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"JSON object key {key!r} is duplicated")  # noqa: TRY003
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"JSON constant {value!r} is not supported")  # noqa: TRY003


def _finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON numbers must be finite")  # noqa: TRY003
    return number


def _event_identity(session_id: str, event_id: str) -> str:
    identity = json.dumps([session_id, event_id], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"pi_event_{hashlib.sha256(identity).hexdigest()}"


def _record_digest(record: Mapping[str, JsonValue]) -> str:
    canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )
    return hashlib.sha256(canonical).hexdigest()


def _checkpoint(value: JsonValue | None) -> _PiSessionCheckpoint:
    if value is None:
        return _PiSessionCheckpoint()
    try:
        return _PiSessionCheckpoint.model_validate(value)
    except ValidationError as error:
        raise InvalidConnectorRunError("checkpoint", "does not match PiSessionCheckpoint") from error


PI_EVENT_SOURCE_DEFINITION = PiEventSourceDefinition()

__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_SESSION_CONNECTOR_NAME",
    "PiEventInput",
    "PiEventSource",
    "PiEventSourceDefinition",
    "PiEventTextEvidenceProjection",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
]
