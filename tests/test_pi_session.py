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
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import JsonValue

from powercontext.errors import ConnectorSubmissionRejectedError, SourceConflictError
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    ConnectorBinding,
    ConnectorRunStatus,
    SourceDefinitionRegistry,
    SourceMaterialization,
    SourceRef,
)
from powercontext.sources.connectors import ConnectorLifecycle
from powercontext.sources.pi_session import (
    PI_EVENT_SOURCE_DEFINITION,
    PiEventInput,
    PiEventSource,
    PiSessionConnector,
    PiSessionInput,
    parse_pi_session,
)


def _archive(records: list[dict[str, object]], session_id: str = "session") -> bytes:
    header: dict[str, object] = {"type": "session", "version": 3, "id": session_id}
    return "\n".join(json.dumps(record, ensure_ascii=False) for record in (header, *records)).encode("utf-8")


def _message(event_id: str, parent_id: str | None, content: object, role: str = "assistant") -> dict[str, object]:
    return {
        "type": "message",
        "id": event_id,
        "parentId": parent_id,
        "message": {"role": role, "content": content},
    }


def _binding() -> ConnectorBinding:
    return ConnectorBinding(
        scope_id="scope", binding_id="pi", connector_name="pi-session-history", connector_version="1"
    )


class _Checkpoints:
    def __init__(self) -> None:
        self.value: JsonValue | None = None

    async def load(self, binding: ConnectorBinding, /) -> JsonValue | None:
        del binding
        return deepcopy(self.value)

    async def save(
        self, binding: ConnectorBinding, checkpoint: JsonValue | None, /, *, expected: JsonValue | None
    ) -> None:
        del binding
        assert self.value == expected
        self.value = deepcopy(checkpoint)


class _SinkFailure(RuntimeError):
    pass


class _MemorySink:
    def __init__(self) -> None:
        self.persisted: dict[str, PiEventInput] = {}
        self.attempts: list[str] = []
        self.fail_event_id: str | None = None
        self.reject_event_id: str | None = None

    async def submit(
        self, binding: ConnectorBinding, item_id: str, definition_name: str, value: object, /
    ) -> SourceRef:
        del binding
        assert isinstance(value, PiEventInput)
        self.attempts.append(value.event_id)
        if value.event_id == self.fail_event_id:
            raise _SinkFailure
        if value.event_id == self.reject_event_id:
            raise ConnectorSubmissionRejectedError("simulated rejection")  # noqa: TRY003
        existing = self.persisted.get(item_id)
        if existing is not None and existing != value:
            raise SourceConflictError("item_id", item_id)
        self.persisted[item_id] = value
        return SourceRef(source_type=definition_name, source_id=item_id)


async def _run(path: Path, sink: _MemorySink, checkpoints: _Checkpoints, leaf_id: str | None = None):
    lifecycle = ConnectorLifecycle(sink=sink, checkpoints=checkpoints)
    return await lifecycle.run(PiSessionConnector(path, leaf_id), _binding())


def test_default_leaf_is_the_last_graph_node_not_the_last_visible_event() -> None:
    records: list[dict[str, object]] = [
        _message("root", None, "selected ancestor"),
        _message("other", None, "later visible event on another branch"),
        {"type": "model_change", "id": "tail-control", "parentId": "root"},
    ]

    events = parse_pi_session(PiSessionInput(content=_archive(records)))

    other = parse_pi_session(PiSessionInput(content=_archive(records), leaf_id="other"))
    assert [event.event_id for event in events] == ["root"]
    assert [event.event_id for event in other] == ["other"]


def test_unselected_branch_is_still_fully_validated() -> None:
    records: list[dict[str, object]] = [
        _message("selected", None, "valid selected branch"),
        {"type": "model_change", "id": "broken", "parentId": "future"},
        {"type": "model_change", "id": "future", "parentId": "selected"},
    ]

    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=_archive(records), leaf_id="selected"))


@pytest.mark.parametrize(
    "line",
    [
        b'{"type":"model_change","id":"a","parentId":null,"value":NaN}',
        b'{"type":"model_change","id":"a","id":"b","parentId":null}',
        b'{"type":"model_change","id":"a"}',
    ],
)
def test_strict_jsonl_rejects_non_json_constants_duplicate_keys_and_missing_parent(line: bytes) -> None:
    raw = b'{"type":"session","version":3,"id":"strict"}\n' + line

    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=raw))


def test_mixed_blocks_source_identity_and_exact_readback() -> None:
    record = _message(
        "event",
        None,
        [
            {"type": "text", "text": "前文"},
            {
                "type": "toolCall",
                "name": "工具",
                "id": "调用",
                "arguments": {"z": "汉字", "a": [1, {"b": 2, "a": 1}]},
            },
            {"type": "thinking", "thinking": "hidden"},
            {"type": "text", "text": "后文"},
        ],
    )
    record["provider"] = {"unprojected": ["complete", {"record": True}]}
    event = parse_pi_session(PiSessionInput(content=_archive([record])))[0]

    async def scenario() -> None:
        registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
        source = await registry.resolve(event)
        assert isinstance(source, PiEventSource)
        projection = registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY)
        other = await registry.resolve(event.model_copy(update={"session_id": "other"}))
        long_identity = await registry.resolve(
            event.model_copy(update={"session_id": "会" * 400, "event_id": "事件" * 400})
        )

        assert source.materialization is SourceMaterialization.CAPTURED
        assert source.content == ('assistant: 前文\n[tool_call 工具 调用] {"a":[1,{"a":1,"b":2}],"z":"汉字"}\n后文')
        assert source.record == record
        assert await registry.read(source) == event
        assert source.name == (await registry.resolve(event)).name
        assert source.name != other.name
        assert len(source.name) <= 256 and len(long_identity.name) <= 256
        assert isinstance(projection, dict)
        assert projection["source_type"] == "pi-event"
        assert projection["source_id"] == source.name
        assert projection["content"] == event.content
        assert projection["metadata"] == {
            "session_id": "session",
            "event_id": "event",
            "parent_id": None,
            "event_type": "message",
            "role": "assistant",
        }

    asyncio.run(scenario())


def test_failed_run_replays_durable_prefix_without_advancing_checkpoint(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    records: list[dict[str, object]] = [_message("a", None, "A"), _message("b", "a", "B"), _message("c", "b", "C")]
    path.write_bytes(_archive(records))
    sink = _MemorySink()
    sink.fail_event_id = "b"
    checkpoints = _Checkpoints()

    async def scenario() -> None:
        with pytest.raises(_SinkFailure):
            await _run(path, sink, checkpoints)
        assert checkpoints.value is None
        assert [event.event_id for event in sink.persisted.values()] == ["a"]

        sink.fail_event_id = None
        recovered = await _run(path, sink, checkpoints)
        assert recovered.status is ConnectorRunStatus.COMPLETE
        assert sink.attempts == ["a", "b", "a", "b", "c"]
        assert len(sink.persisted) == 3

        sink.attempts.clear()
        replay = await _run(path, sink, checkpoints)
        assert replay.items == ()
        assert sink.attempts == []
        assert replay.committed_checkpoint == recovered.committed_checkpoint

    asyncio.run(scenario())


def test_full_record_mutation_is_rejected_before_any_new_submission(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    records: list[dict[str, object]] = [_message("a", None, "A"), _message("b", "a", "B")]
    records[1]["provider"] = {"unprojected": "original"}
    path.write_bytes(_archive(records))
    sink = _MemorySink()
    checkpoints = _Checkpoints()

    async def scenario() -> None:
        first = await _run(path, sink, checkpoints)
        records.append(_message("c", "b", "C"))
        records[1]["provider"] = {"unprojected": "changed"}
        path.write_bytes(_archive(records))
        sink.attempts.clear()

        with pytest.raises(ValueError):
            await _run(path, sink, checkpoints)
        assert sink.attempts == []
        assert checkpoints.value == first.committed_checkpoint
        assert len(sink.persisted) == 2

        records[1]["provider"] = {"unprojected": "original"}
        path.write_bytes(_archive(records))
        repaired = await _run(path, sink, checkpoints)
        assert sink.attempts == ["c"]
        assert repaired.previous_checkpoint == first.committed_checkpoint
        assert len(sink.persisted) == 3

    asyncio.run(scenario())


def test_making_an_imported_event_invisible_still_counts_as_mutation(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    records: list[dict[str, object]] = [_message("a", None, "captured")]
    path.write_bytes(_archive(records))
    sink = _MemorySink()
    checkpoints = _Checkpoints()

    async def scenario() -> None:
        first = await _run(path, sink, checkpoints)
        records[0]["type"] = "model_change"
        path.write_bytes(_archive(records))
        sink.attempts.clear()

        with pytest.raises(ValueError):
            await _run(path, sink, checkpoints)
        assert sink.attempts == []
        assert checkpoints.value == first.committed_checkpoint

    asyncio.run(scenario())


def test_rejected_new_event_keeps_old_progress_until_retry(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    records: list[dict[str, object]] = [_message("a", None, "A")]
    path.write_bytes(_archive(records))
    sink = _MemorySink()
    checkpoints = _Checkpoints()

    async def scenario() -> None:
        first = await _run(path, sink, checkpoints)
        records.append(_message("b", "a", "B"))
        path.write_bytes(_archive(records))
        sink.reject_event_id = "b"

        rejected = await _run(path, sink, checkpoints)
        assert rejected.status is ConnectorRunStatus.INCOMPLETE
        assert rejected.committed_checkpoint == first.committed_checkpoint
        assert rejected.proposed_checkpoint == first.committed_checkpoint

        sink.reject_event_id = None
        recovered = await _run(path, sink, checkpoints)
        assert recovered.status is ConnectorRunStatus.COMPLETE
        assert recovered.previous_checkpoint == first.committed_checkpoint
        assert len(sink.persisted) == 2

    asyncio.run(scenario())
