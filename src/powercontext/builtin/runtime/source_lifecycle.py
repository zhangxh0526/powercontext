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

"""Source dependency lifecycle registration, propagation, and exact references."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Literal, NoReturn, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory import Memory, MemoryEntryVersion, MemoryService
from powercontext.builtin.artifacts.memory.errors import InvalidMemoryCitationError, MemoryEntryNotFoundError
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.errors import InvalidRepositoryArgumentError, RepositoryNotFoundError
from powercontext.builtin.persistence.source_lifecycle import (
    SourceLifecyclePolicy,
    SourceLifecycleReference,
    SourceLifecycleRepository,
    StoredSourceLifecycleEvent,
    StoredSourceLifecycleNode,
)
from powercontext.builtin.persistence.sources import SourceRepository
from powercontext.builtin.persistence.topic_memory import TopicMemoryRepository
from powercontext.builtin.sources import validate_scope_id
from powercontext.errors import ArtifactNotFoundError
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime

SourceLifecyclePurpose: TypeAlias = Literal["generation", "audit"]
MemoryServiceFactory = Callable[[AsyncConnection], MemoryService]


class SourceLifecycleError(ValueError):
    """Reject an invalid Source lifecycle binding, event, or resolution."""


class SourceLifecycleEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    node_id: StrictStr
    revision: StrictInt = Field(gt=0)
    sequence: StrictInt = Field(gt=0)
    available: StrictBool
    reason: StrictStr

    @field_validator("node_id", "reason")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")  # noqa: TRY003
        return value


class RelationalSourceLifecycle:
    def __init__(
        self,
        *,
        database: AsyncDatabase,
        scope_id: str,
        repository: SourceLifecycleRepository,
        sources: SourceRepository,
        topic_memories: TopicMemoryRepository,
        memory_service: MemoryServiceFactory,
    ) -> None:
        self._database = database
        self._scope_id = scope_id
        self._repository = repository
        self._sources = sources
        self._topic_memories = topic_memories
        self._memory_service = memory_service

    async def register_source(
        self,
        node_id: str,
        source_ref: SourceRef,
        /,
        *,
        revision: int,
    ) -> None:
        _validate_node_id(node_id)
        _require(isinstance(source_ref, SourceRef), "source-reference")
        _require(type(revision) is int and revision > 0, "source-revision")
        candidate = StoredSourceLifecycleNode(
            node_id=node_id,
            kind="source",
            reference=source_ref,
            parents=(),
            policy=None,
            available=True,
            revision=revision,
        )
        async with self._database.transaction() as connection:
            existing = await self._repository.get(connection, self._scope_id, node_id, for_update=True)
            if existing is not None:
                _require(_same_binding(existing, candidate), "registration-conflict")
                return
            try:
                await self._sources.get(connection, self._scope_id, source_ref)
            except RepositoryNotFoundError:
                _fail("source-reference")
            await self._repository.insert(connection, self._scope_id, candidate)

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        /,
        *,
        parents: tuple[str, ...],
        policy: SourceLifecyclePolicy,
    ) -> None:
        _require(isinstance(citation, MemoryCitation), "memory-reference")
        await self._register_derived("memory", node_id, citation, parents=parents, policy=policy)

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        /,
        *,
        parents: tuple[str, ...],
        policy: SourceLifecyclePolicy,
    ) -> None:
        _require(isinstance(artifact_ref, ArtifactRef), "topic-reference")
        await self._register_derived("topic", node_id, artifact_ref, parents=parents, policy=policy)

    async def _register_derived(
        self,
        kind: Literal["memory", "topic"],
        node_id: str,
        reference: MemoryCitation | ArtifactRef,
        /,
        *,
        parents: tuple[str, ...],
        policy: SourceLifecyclePolicy,
    ) -> None:
        _validate_node_id(node_id)
        _require(policy in {"all", "any"}, "dependency-policy")
        normalized_parents = _validate_parents(node_id, parents)
        async with self._database.transaction() as connection:
            nodes = await self._repository.list(connection, self._scope_id)
            existing = next((node for node in nodes if node.node_id == node_id), None)
            if existing is not None:
                candidate = StoredSourceLifecycleNode(
                    node_id=node_id,
                    kind=kind,
                    reference=reference,
                    parents=normalized_parents,
                    policy=policy,
                    available=existing.available,
                    revision=None,
                )
                _require(_same_binding(existing, candidate), "registration-conflict")
                return
            by_id = {node.node_id: node for node in nodes}
            for parent in normalized_parents:
                _require(parent in by_id, "unknown-parent")
            available = _derived_availability(normalized_parents, policy, by_id)
            candidate = StoredSourceLifecycleNode(
                node_id=node_id,
                kind=kind,
                reference=reference,
                parents=normalized_parents,
                policy=policy,
                available=available,
                revision=None,
            )
            updated = (*nodes, candidate)
            _validate_graph(updated)
            if kind == "memory":
                if not isinstance(reference, MemoryCitation):
                    _fail("memory-reference")
                await self._validate_memory_reference(connection, reference)
            else:
                if not isinstance(reference, ArtifactRef) or reference.family != "topic-memory":
                    _fail("topic-reference")
                try:
                    await self._topic_memories.get_exact(connection, self._scope_id, reference)
                except (RepositoryNotFoundError, InvalidRepositoryArgumentError):
                    _fail("topic-reference")
            await self._repository.insert(connection, self._scope_id, candidate)
            if kind == "memory":
                await self._sync_memory(connection, updated, target_ids={node_id}, reason=None)

    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        _require(isinstance(event, SourceLifecycleEvent), "lifecycle-event")
        async with self._database.transaction() as connection:
            nodes = await self._repository.list(connection, self._scope_id)
            by_id = {node.node_id: node for node in nodes}
            source = by_id.get(event.node_id)
            if source is None or source.kind != "source":
                _fail("unknown-source")
            _require(event.revision == source.revision, "source-revision")
            if event.sequence < source.sequence:
                _fail("event-sequence")
            if event.sequence == source.sequence:
                previous = await self._repository.get_event(
                    connection,
                    self._scope_id,
                    event.node_id,
                    event.sequence,
                )
                if previous is None or not _same_event(previous, event):
                    _fail("event-conflict")
                return sorted(node.node_id for node in nodes if node.available)

            availability = _propagated_availability(nodes, event)
            await self._repository.record_event(
                connection,
                self._scope_id,
                StoredSourceLifecycleEvent(
                    node_id=event.node_id,
                    revision=event.revision,
                    sequence=event.sequence,
                    available=event.available,
                    reason=event.reason,
                ),
                expected_sequence=source.sequence,
            )
            updated = tuple(
                replace(
                    node,
                    available=availability[node.node_id],
                    sequence=event.sequence if node.node_id == event.node_id else node.sequence,
                )
                for node in nodes
            )
            for before, after in zip(nodes, updated, strict=True):
                if before.available != after.available:
                    await self._repository.update_availability(
                        connection,
                        self._scope_id,
                        before.node_id,
                        after.available,
                    )
            await self._sync_memory(connection, updated, reason=event.reason)
            return sorted(node.node_id for node in updated if node.available)

    async def resolve(
        self,
        node_id: str,
        /,
        *,
        purpose: SourceLifecyclePurpose,
    ) -> SourceLifecycleReference:
        _require(purpose in {"generation", "audit"}, "resolve-purpose")
        _validate_node_id(node_id)
        async with self._database.transaction() as connection:
            node = await self._repository.get(connection, self._scope_id, node_id)
        if node is None:
            _fail("unknown-node")
        _require(purpose == "audit" or node.available, "node-unavailable")
        return node.reference

    async def _validate_memory_reference(self, connection: AsyncConnection, citation: MemoryCitation) -> None:
        _require(citation.memory_ref.family == Memory.family, "memory-reference")
        service = self._memory_service(connection)
        try:
            exact = await service.revision(citation.memory_ref)
            _require(
                any(
                    item.entry_id == citation.entry_id and item.entry_version_id == citation.entry_version_id
                    for item in exact.content.manifest.entries
                ),
                "memory-reference",
            )
            await service.validate_citation(citation)
            current = await service.head(citation.memory_ref.artifact_id)
            _require(
                any(item.entry_id == citation.entry_id for item in current.content.manifest.entries),
                "memory-reference",
            )
        except (ArtifactNotFoundError, InvalidMemoryCitationError, MemoryEntryNotFoundError):
            _fail("memory-reference")

    async def _sync_memory(
        self,
        connection: AsyncConnection,
        nodes: tuple[StoredSourceLifecycleNode, ...],
        *,
        target_ids: set[str] | None = None,
        reason: str | None,
    ) -> None:
        desired: dict[tuple[str, str], bool] = {}
        selected: set[tuple[str, str]] = set()
        for node in nodes:
            if node.kind != "memory" or not isinstance(node.reference, MemoryCitation):
                continue
            key = (node.reference.memory_ref.artifact_id, node.reference.entry_id)
            desired[key] = desired.get(key, False) or node.available
            if target_ids is None or node.node_id in target_ids:
                selected.add(key)
        if target_ids is None:
            selected = set(desired)
        by_artifact: dict[str, list[tuple[str, bool]]] = {}
        for artifact_id, entry_id in sorted(selected):
            by_artifact.setdefault(artifact_id, []).append((entry_id, desired[(artifact_id, entry_id)]))
        if not by_artifact:
            return
        service = self._memory_service(connection)
        for artifact_id, targets in by_artifact.items():
            await self._sync_memory_artifact(service, artifact_id, targets, reason=reason)

    @staticmethod
    async def _sync_memory_artifact(
        service: MemoryService,
        artifact_id: str,
        targets: list[tuple[str, bool]],
        *,
        reason: str | None,
    ) -> None:
        current = await service.head(artifact_id)
        entries = {entry.entry_id: entry for entry in await service.entries(current)}
        manifest = {item.entry_id: item for item in current.content.manifest.entries}
        activate: list[MemoryEntryVersion] = []
        deactivate: list[MemoryEntryVersion] = []
        for entry_id, available in targets:
            _require(entry_id in entries and entry_id in manifest, "memory-binding")
            entry = entries[entry_id]
            if available and manifest[entry_id].state == "inactive":
                activate.append(entry)
            elif not available and manifest[entry_id].state == "active":
                deactivate.append(entry)
        if deactivate:
            current = await service.forget(current, entries=deactivate, reason=reason)
        if activate:
            await service.reactivate(current, entries=activate, reason=reason)


class SourceLifecycleApplication:
    def __init__(self, runtime: BuiltinRuntime) -> None:
        self._runtime = runtime

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycleApplication:
        return ScopedSourceLifecycleApplication(self._runtime, scope_id)


class ScopedSourceLifecycleApplication:
    def __init__(self, runtime: BuiltinRuntime, scope_id: str) -> None:
        self._runtime = runtime
        self.scope_id = validate_scope_id(scope_id)

    def _service(self) -> RelationalSourceLifecycle:
        factory = self._runtime._source_lifecycle
        if factory is None:
            raise RuntimeError("Source lifecycle is unavailable")  # noqa: TRY003
        return factory(self.scope_id)

    async def register_source(
        self,
        node_id: str,
        source_ref: SourceRef,
        /,
        *,
        revision: int,
    ) -> None:
        async with self._runtime._scoped_operation(self.scope_id), self._runtime._locked(self.scope_id):
            await self._service().register_source(node_id, source_ref, revision=revision)

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        /,
        *,
        parents: tuple[str, ...],
        policy: SourceLifecyclePolicy,
    ) -> None:
        async with self._runtime._scoped_operation(self.scope_id), self._runtime._locked(self.scope_id):
            await self._service().register_memory(node_id, citation, parents=parents, policy=policy)

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        /,
        *,
        parents: tuple[str, ...],
        policy: SourceLifecyclePolicy,
    ) -> None:
        async with self._runtime._scoped_operation(self.scope_id), self._runtime._locked(self.scope_id):
            await self._service().register_topic(node_id, artifact_ref, parents=parents, policy=policy)

    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        async with self._runtime._scoped_operation(self.scope_id), self._runtime._locked(self.scope_id):
            return await self._service().apply(event)

    async def resolve(
        self,
        node_id: str,
        /,
        *,
        purpose: SourceLifecyclePurpose,
    ) -> SourceLifecycleReference:
        async with self._runtime._scoped_operation(self.scope_id):
            return await self._service().resolve(node_id, purpose=purpose)


SourceLifecycleFactory = Callable[[str], RelationalSourceLifecycle]


def _derived_availability(
    parents: tuple[str, ...],
    policy: SourceLifecyclePolicy,
    nodes: dict[str, StoredSourceLifecycleNode],
) -> bool:
    support = (nodes[parent].available for parent in parents)
    return all(support) if policy == "all" else any(support)


def _propagated_availability(
    nodes: tuple[StoredSourceLifecycleNode, ...],
    event: SourceLifecycleEvent,
) -> dict[str, bool]:
    by_id = {node.node_id: node for node in nodes}
    computed: dict[str, bool] = {}
    visiting: set[str] = set()

    def compute(node_id: str) -> bool:
        if node_id in computed:
            return computed[node_id]
        _require(node_id not in visiting, "dependency-cycle")
        visiting.add(node_id)
        node = by_id[node_id]
        if node.node_id == event.node_id:
            available = event.available
        elif node.kind == "source":
            available = node.available
        else:
            if node.policy is None:
                _fail("dependency-policy")
            support = tuple(compute(parent) for parent in node.parents)
            available = all(support) if node.policy == "all" else any(support)
        visiting.remove(node_id)
        computed[node_id] = available
        return available

    for node in nodes:
        compute(node.node_id)
    return computed


def _validate_parents(node_id: str, parents: tuple[str, ...]) -> tuple[str, ...]:
    _require(not isinstance(parents, str), "dependency-parents")
    normalized = tuple(parents)
    _require(bool(normalized), "dependency-parents")
    for parent in normalized:
        _validate_node_id(parent)
        _require(parent != node_id, "dependency-cycle")
    _require(len(set(normalized)) == len(normalized), "duplicate-parent")
    return tuple(sorted(normalized))


def _validate_graph(nodes: tuple[StoredSourceLifecycleNode, ...]) -> None:
    by_id = {node.node_id: node for node in nodes}
    _require(len(by_id) == len(nodes), "registration-conflict")
    completed: set[str] = set()
    visiting: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in completed:
            return
        _require(node_id not in visiting, "dependency-cycle")
        visiting.add(node_id)
        node = by_id[node_id]
        for parent in node.parents:
            _require(parent in by_id and parent != node_id, "dependency-cycle")
            visit(parent)
        visiting.remove(node_id)
        completed.add(node_id)

    for node_id in by_id:
        visit(node_id)


def _same_binding(existing: StoredSourceLifecycleNode, candidate: StoredSourceLifecycleNode) -> bool:
    return (
        existing.kind == candidate.kind
        and existing.reference == candidate.reference
        and existing.parents == candidate.parents
        and existing.policy == candidate.policy
        and existing.revision == candidate.revision
    )


def _same_event(stored: StoredSourceLifecycleEvent, event: SourceLifecycleEvent) -> bool:
    return (
        stored.node_id == event.node_id
        and stored.revision == event.revision
        and stored.sequence == event.sequence
        and stored.available == event.available
        and stored.reason == event.reason
    )


def _validate_node_id(node_id: str) -> None:
    _require(type(node_id) is str and bool(node_id.strip()), "node-id")


def _require(condition: bool, code: str) -> None:
    if not condition:
        _fail(code)


def _fail(code: str) -> NoReturn:
    raise SourceLifecycleError(code)


__all__ = [
    "RelationalSourceLifecycle",
    "ScopedSourceLifecycleApplication",
    "SourceLifecycleApplication",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
    "SourceLifecycleFactory",
]
