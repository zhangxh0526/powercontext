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

"""Relational storage for scoped Source dependency lifecycle state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import RootModel
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.codec import dump_model, load_model, stored_bytes
from powercontext.builtin.persistence.errors import StoredPayloadConflictError
from powercontext.builtin.persistence.tables import (
    SOURCE_LIFECYCLE_EVENTS_TABLE,
    SOURCE_LIFECYCLE_NODES_TABLE,
)
from powercontext.sources import SourceRef

SourceLifecycleKind = Literal["source", "memory", "topic"]
SourceLifecyclePolicy = Literal["all", "any"]
SourceLifecycleReference = SourceRef | MemoryCitation | ArtifactRef


class _ParentIds(RootModel[tuple[str, ...]]):
    pass


@dataclass(frozen=True, slots=True)
class StoredSourceLifecycleNode:
    node_id: str
    kind: SourceLifecycleKind
    reference: SourceLifecycleReference
    parents: tuple[str, ...]
    policy: SourceLifecyclePolicy | None
    available: bool
    revision: int | None
    sequence: int = 0


@dataclass(frozen=True, slots=True)
class StoredSourceLifecycleEvent:
    node_id: str
    revision: int
    sequence: int
    available: bool
    reason: str


class SourceLifecycleRepository:
    async def get(
        self,
        connection: AsyncConnection,
        scope_id: str,
        node_id: str,
        /,
        *,
        for_update: bool = False,
    ) -> StoredSourceLifecycleNode | None:
        table = SOURCE_LIFECYCLE_NODES_TABLE
        statement = select(table).where(table.c.scope_id == scope_id, table.c.node_id == node_id)
        if for_update:
            statement = statement.with_for_update()
        row = (await connection.execute(statement)).mappings().one_or_none()
        return None if row is None else _decode_node(row)

    async def list(
        self,
        connection: AsyncConnection,
        scope_id: str,
        /,
    ) -> tuple[StoredSourceLifecycleNode, ...]:
        table = SOURCE_LIFECYCLE_NODES_TABLE
        rows = (
            await connection.execute(select(table).where(table.c.scope_id == scope_id).order_by(table.c.node_id))
        ).mappings()
        return tuple(_decode_node(row) for row in rows)

    async def insert(
        self,
        connection: AsyncConnection,
        scope_id: str,
        node: StoredSourceLifecycleNode,
        /,
    ) -> None:
        artifact_id, artifact_revision = _artifact_identity(node.reference)
        await connection.execute(
            insert(SOURCE_LIFECYCLE_NODES_TABLE).values(
                scope_id=scope_id,
                node_id=node.node_id,
                kind=node.kind,
                reference=dump_model(node.reference, kind="source-lifecycle", name=node.node_id),
                parents=dump_model(_ParentIds(node.parents), kind="source-lifecycle-parents", name=node.node_id),
                policy=node.policy,
                available=node.available,
                revision=node.revision,
                sequence=node.sequence,
                artifact_id=artifact_id,
                artifact_revision=artifact_revision,
            )
        )

    async def update_availability(
        self,
        connection: AsyncConnection,
        scope_id: str,
        node_id: str,
        available: bool,
        /,
    ) -> None:
        table = SOURCE_LIFECYCLE_NODES_TABLE
        await connection.execute(
            update(table).where(table.c.scope_id == scope_id, table.c.node_id == node_id).values(available=available)
        )

    async def get_event(
        self,
        connection: AsyncConnection,
        scope_id: str,
        node_id: str,
        sequence: int,
        /,
    ) -> StoredSourceLifecycleEvent | None:
        table = SOURCE_LIFECYCLE_EVENTS_TABLE
        row = (
            (
                await connection.execute(
                    select(table).where(
                        table.c.scope_id == scope_id,
                        table.c.node_id == node_id,
                        table.c.sequence == sequence,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return StoredSourceLifecycleEvent(
            node_id=str(row["node_id"]),
            revision=int(row["revision"]),
            sequence=int(row["sequence"]),
            available=bool(row["available"]),
            reason=str(row["reason"]),
        )

    async def record_event(
        self,
        connection: AsyncConnection,
        scope_id: str,
        event: StoredSourceLifecycleEvent,
        /,
        *,
        expected_sequence: int,
    ) -> None:
        nodes = SOURCE_LIFECYCLE_NODES_TABLE
        advanced = await connection.execute(
            update(nodes)
            .where(
                nodes.c.scope_id == scope_id,
                nodes.c.node_id == event.node_id,
                nodes.c.sequence == expected_sequence,
            )
            .values(sequence=event.sequence)
        )
        if advanced.rowcount != 1:
            raise StoredPayloadConflictError(
                "source-lifecycle-event",
                (scope_id, event.node_id, event.sequence),
            )
        await connection.execute(
            insert(SOURCE_LIFECYCLE_EVENTS_TABLE).values(
                scope_id=scope_id,
                node_id=event.node_id,
                revision=event.revision,
                sequence=event.sequence,
                available=event.available,
                reason=event.reason,
            )
        )


def _decode_node(row: Mapping[Any, Any]) -> StoredSourceLifecycleNode:
    node_id = str(row["node_id"])
    kind = str(row["kind"])
    payload = stored_bytes(row["reference"], column="source lifecycle reference")
    if kind == "source":
        reference: SourceLifecycleReference = load_model(
            SourceRef,
            payload,
            kind="source-lifecycle",
            name=node_id,
        )
    elif kind == "memory":
        reference = load_model(
            MemoryCitation,
            payload,
            kind="source-lifecycle",
            name=node_id,
        )
    elif kind == "topic":
        reference = load_model(
            ArtifactRef,
            payload,
            kind="source-lifecycle",
            name=node_id,
        )
    else:
        raise ValueError(f"unknown stored Source lifecycle kind: {kind!r}")  # noqa: TRY003
    parents = load_model(
        _ParentIds,
        stored_bytes(row["parents"], column="source lifecycle parents"),
        kind="source-lifecycle-parents",
        name=node_id,
    ).root
    policy = row["policy"]
    return StoredSourceLifecycleNode(
        node_id=node_id,
        kind=cast(SourceLifecycleKind, kind),
        reference=reference,
        parents=parents,
        policy=cast(SourceLifecyclePolicy | None, None if policy is None else str(policy)),
        available=bool(row["available"]),
        revision=None if row["revision"] is None else int(row["revision"]),
        sequence=int(row["sequence"]),
    )


def _artifact_identity(reference: SourceLifecycleReference) -> tuple[str | None, int | None]:
    if isinstance(reference, ArtifactRef):
        return reference.artifact_id, reference.revision
    if isinstance(reference, MemoryCitation):
        return reference.memory_ref.artifact_id, reference.memory_ref.revision
    return None, None


__all__ = [
    "SourceLifecycleKind",
    "SourceLifecyclePolicy",
    "SourceLifecycleReference",
    "SourceLifecycleRepository",
    "StoredSourceLifecycleEvent",
    "StoredSourceLifecycleNode",
]
