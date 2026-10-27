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
import sqlite3
from typing import Any, cast

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.records import ArtifactWrite
from powercontext.builtin.runtime import (
    BuiltinConfig,
    BuiltinRuntime,
    CaptureSource,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    SearchTopicMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleError, SourceLifecycleEvent
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceRef


async def _scope(runtime: BuiltinRuntime, key: str) -> str:
    scope = await runtime.scopes.create(ScopeDraft(title="Lifecycle", summary="Source evidence", idempotency_key=key))
    return scope.scope_id


async def _source(runtime: BuiltinRuntime, scope_id: str, node_id: str) -> SourceRef:
    receipt = await runtime.sources.for_scope(scope_id).capture(
        CaptureSource(source_id=node_id, content=f"Origin {node_id}", metadata={})
    )
    return receipt.source_ref


async def _memory(runtime: BuiltinRuntime, scope_id: str, text: str) -> MemoryCitation:
    result = await runtime.memory.for_scope(scope_id).remember(
        RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text=text),))
    )
    assert result.entry is not None
    return result.entry.citation


async def _topic(runtime: BuiltinRuntime, scope_id: str, title: str) -> ArtifactRef:
    created = await runtime.records.for_scope(scope_id).create_artifact(
        "topic-memory",
        ArtifactWrite(content={"title": title, "summary": f"{title} supported summary", "detail": title}),
    )
    return ArtifactRef(family="topic-memory", artifact_id=created.artifact_id, revision=created.revision)


def _event(
    node_id: str = "root-a",
    *,
    revision: int = 3,
    sequence: int,
    available: bool,
    reason: str = "controlled lifecycle",
) -> SourceLifecycleEvent:
    return SourceLifecycleEvent(
        node_id=node_id,
        revision=revision,
        sequence=sequence,
        available=available,
        reason=reason,
    )


async def _state(runtime: BuiltinRuntime, scope_id: str, citation: MemoryCitation) -> str:
    entries = await runtime.memory.for_scope(scope_id).list(include_inactive=True)
    return next(record.state for record in entries.entries if record.entry.entry_id == citation.entry_id)


async def _topic_hits(runtime: BuiltinRuntime, scope_id: str, query: str) -> tuple[ArtifactRef, ...]:
    result = await runtime.topic_memory.for_scope(scope_id).search(SearchTopicMemoryRequest(query=query, limit=10))
    return tuple(hit.artifact_ref for hit in result.hits)


def test_source_lifecycle_event_validation_is_strict() -> None:
    valid = {
        "node_id": "root",
        "revision": 1,
        "sequence": 1,
        "available": False,
        "reason": "withdrawn",
    }
    for invalid in (
        {"node_id": " \t"},
        {"node_id": 1},
        {"reason": "\n"},
        {"reason": 1},
        {"revision": 0},
        {"revision": True},
        {"revision": "1"},
        {"sequence": 0},
        {"sequence": True},
        {"sequence": "1"},
        {"available": 0},
        {"available": "false"},
        {"extra": True},
    ):
        with pytest.raises(ValidationError):
            SourceLifecycleEvent(**(valid | invalid))


def test_registration_replay_conflicts_and_withdrawn_state_are_stable() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(BuiltinConfig(database=SQLiteConfig())) as runtime:
            scope_id = await _scope(runtime, "registration")
            lifecycle = runtime.lifecycle.for_scope(scope_id)
            root_a = await _source(runtime, scope_id, "root-a")
            root_b = await _source(runtime, scope_id, "root-b")
            citation = await _memory(runtime, scope_id, "fact evidence")
            await lifecycle.register_source("root-a", root_a, revision=3)
            await lifecycle.register_source("root-a", root_a, revision=3)
            await lifecycle.register_source("root-b", root_b, revision=3)
            await lifecycle.register_memory("fact", citation, parents=("root-b", "root-a"), policy="all")
            before = (await runtime.memory.for_scope(scope_id).list()).memory_ref
            await lifecycle.register_memory("fact", citation, parents=("root-a", "root-b"), policy="all")
            assert (await runtime.memory.for_scope(scope_id).list()).memory_ref == before

            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_source("root-a", root_a, revision=4)
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("fact", citation, parents=("root-a",), policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("fact", citation, parents=("root-a", "root-b"), policy="any")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("missing", citation, parents=("unknown",), policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("duplicate", citation, parents=("root-a", "root-a"), policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("empty", citation, parents=(), policy="any")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("cycle", citation, parents=("cycle",), policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_source(
                    "unreadable",
                    SourceRef(source_type=root_a.source_type, source_id="missing"),
                    revision=3,
                )

            await lifecycle.apply(_event(sequence=1, available=False))
            withdrawn = (await runtime.memory.for_scope(scope_id).list()).memory_ref
            assert withdrawn != before
            await lifecycle.register_memory("fact", citation, parents=("root-b", "root-a"), policy="all")
            assert (await runtime.memory.for_scope(scope_id).list()).memory_ref == withdrawn
            assert await _state(runtime, scope_id, citation) == "inactive"
            with pytest.raises(SourceLifecycleError):
                await lifecycle.resolve("fact", purpose="generation")
            assert await lifecycle.resolve("fact", purpose="audit") == citation

    asyncio.run(scenario())


def test_event_replay_requires_exact_reason_and_does_not_add_memory_revisions() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(BuiltinConfig(database=SQLiteConfig())) as runtime:
            scope_id = await _scope(runtime, "event-replay")
            lifecycle = runtime.lifecycle.for_scope(scope_id)
            source_ref = await _source(runtime, scope_id, "root-a")
            citation = await _memory(runtime, scope_id, "fact evidence")
            await lifecycle.register_source("root-a", source_ref, revision=3)
            await lifecycle.register_memory("fact", citation, parents=("root-a",), policy="all")
            withdrawn_event = _event(sequence=4, available=False, reason="original reason")
            assert await lifecycle.apply(withdrawn_event) == []
            withdrawn_ref = (await runtime.memory.for_scope(scope_id).list()).memory_ref
            assert await lifecycle.apply(withdrawn_event) == []
            assert (await runtime.memory.for_scope(scope_id).list()).memory_ref == withdrawn_ref

            for invalid in (
                _event(sequence=4, available=False, reason="different reason"),
                _event(sequence=4, available=True),
                _event(sequence=4, available=False, revision=4),
                _event(sequence=3, available=False),
            ):
                with pytest.raises(SourceLifecycleError):
                    await lifecycle.apply(invalid)
                assert (await runtime.memory.for_scope(scope_id).list()).memory_ref == withdrawn_ref
            with pytest.raises(SourceLifecycleError):
                await lifecycle.resolve("fact", purpose="generation")
            assert await lifecycle.resolve("fact", purpose="audit") == citation

            restored_event = _event(sequence=5, available=True, reason="restored")
            assert await lifecycle.apply(restored_event) == ["fact", "root-a"]
            restored_ref = (await runtime.memory.for_scope(scope_id).list()).memory_ref
            assert await lifecycle.apply(restored_event) == ["fact", "root-a"]
            current = await runtime.memory.for_scope(scope_id).list(include_inactive=True)
            assert current.memory_ref == restored_ref
            assert current.entries[0].entry.entry_version_id == citation.entry_version_id
            original = await runtime.memory.for_scope(scope_id).get(GetMemoryEntryRequest(citation=citation))
            assert original.entry.text == "fact evidence"
            assert await lifecycle.resolve("fact", purpose="generation") == citation

    asyncio.run(scenario())


def test_native_memory_failure_rolls_back_event_and_same_event_retry_finishes(tmp_path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "lifecycle-retry.db"
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{database_path}"))
        async with open_builtin_runtime(config) as runtime:
            scope_id = await _scope(runtime, "failure-retry")
            lifecycle = runtime.lifecycle.for_scope(scope_id)
            source_ref = await _source(runtime, scope_id, "root-a")
            citation = await _memory(runtime, scope_id, "fact evidence")
            await lifecycle.register_source("root-a", source_ref, revision=3)
            await lifecycle.register_memory("fact", citation, parents=("root-a",), policy="all")
            before = (await runtime.memory.for_scope(scope_id).list()).memory_ref
            with sqlite3.connect(database_path) as connection:
                connection.executescript("""
                    CREATE TRIGGER reject_lifecycle_memory_write
                    BEFORE INSERT ON pc_artifacts
                    WHEN NEW.family = 'memory' AND NEW.revision > 1
                    BEGIN
                        SELECT RAISE(ABORT, 'forced lifecycle Memory failure');
                    END;
                    """)
            event = _event(sequence=1, available=False)
            with pytest.raises(IntegrityError, match="forced lifecycle Memory failure"):
                await lifecycle.apply(event)

            assert (await runtime.memory.for_scope(scope_id).list()).memory_ref == before
            assert await _state(runtime, scope_id, citation) == "active"
            assert await lifecycle.resolve("root-a", purpose="generation") == source_ref
            assert await lifecycle.resolve("fact", purpose="generation") == citation
            with sqlite3.connect(database_path) as connection:
                assert connection.execute("SELECT count(*) FROM pc_source_lifecycle_events").fetchone()[0] == 0
                connection.execute("DROP TRIGGER reject_lifecycle_memory_write")

            assert await lifecycle.apply(event) == []
            assert await _state(runtime, scope_id, citation) == "inactive"
            with pytest.raises(SourceLifecycleError):
                await lifecycle.resolve("fact", purpose="generation")
            retried_ref = (await runtime.memory.for_scope(scope_id).list()).memory_ref
            assert await lifecycle.apply(event) == []
            assert (await runtime.memory.for_scope(scope_id).list()).memory_ref == retried_ref
            original = await runtime.memory.for_scope(scope_id).get(GetMemoryEntryRequest(citation=citation))
            assert original.entry.text == "fact evidence"
            assert original.entry.entry_version_id == citation.entry_version_id

    asyncio.run(scenario())


def test_scope_isolation_and_reference_readability_are_enforced() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(BuiltinConfig(database=SQLiteConfig())) as runtime:
            scope_a = await _scope(runtime, "scope-a")
            scope_b = await _scope(runtime, "scope-b")
            lifecycle_a = runtime.lifecycle.for_scope(scope_a)
            lifecycle_b = runtime.lifecycle.for_scope(scope_b)
            root_a = await _source(runtime, scope_a, "root")
            root_b = await _source(runtime, scope_b, "foreign-only")
            citation_a = await _memory(runtime, scope_a, "fact evidence")
            await lifecycle_a.register_source("root", root_a, revision=3)
            await lifecycle_b.register_source("root", root_b, revision=3)
            await lifecycle_a.register_memory("fact", citation_a, parents=("root",), policy="all")

            with pytest.raises(SourceLifecycleError):
                await lifecycle_a.register_source("foreign", root_b, revision=3)
            with pytest.raises(SourceLifecycleError):
                await lifecycle_b.register_memory("foreign", citation_a, parents=("root",), policy="all")
            assert await lifecycle_b.apply(_event("root", sequence=1, available=False)) == []
            assert await lifecycle_a.resolve("root", purpose="generation") == root_a
            assert await lifecycle_a.resolve("fact", purpose="generation") == citation_a
            with pytest.raises(SourceLifecycleError):
                await lifecycle_b.resolve("root", purpose="generation")
            with pytest.raises(SourceLifecycleError):
                await lifecycle_b.resolve("fact", purpose="audit")
            with pytest.raises(SourceLifecycleError):
                await lifecycle_a.resolve("fact", purpose=cast(Any, "unknown"))

    asyncio.run(scenario())


def test_unregistered_topic_is_compatible_and_registered_topic_recovers() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(BuiltinConfig(database=SQLiteConfig())) as runtime:
            scope_id = await _scope(runtime, "topic-compatibility")
            lifecycle = runtime.lifecycle.for_scope(scope_id)
            source_ref = await _source(runtime, scope_id, "root-a")
            registered = await _topic(runtime, scope_id, "registeredtopic")
            unregistered = await _topic(runtime, scope_id, "freetopic")
            await lifecycle.register_source("root-a", source_ref, revision=3)
            await lifecycle.register_topic("registered", registered, parents=("root-a",), policy="all")
            assert await _topic_hits(runtime, scope_id, "registeredtopic") == (registered,)
            assert await _topic_hits(runtime, scope_id, "freetopic") == (unregistered,)

            await lifecycle.apply(_event(sequence=1, available=False))
            assert await _topic_hits(runtime, scope_id, "registeredtopic") == ()
            assert await _topic_hits(runtime, scope_id, "freetopic") == (unregistered,)
            with pytest.raises(SourceLifecycleError):
                await lifecycle.resolve("registered", purpose="generation")
            assert await lifecycle.resolve("registered", purpose="audit") == registered
            exact = await runtime.records.for_scope(scope_id).get_artifact_revision(
                "topic-memory",
                registered.artifact_id,
                registered.revision,
            )
            assert exact.content["title"] == "registeredtopic"

            await lifecycle.apply(_event(sequence=2, available=True))
            assert await _topic_hits(runtime, scope_id, "registeredtopic") == (registered,)
            assert await _topic_hits(runtime, scope_id, "freetopic") == (unregistered,)

    asyncio.run(scenario())
