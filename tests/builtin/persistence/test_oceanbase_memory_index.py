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
import re
from typing import Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemorySearchRequest
from powercontext.builtin.persistence.memory_index import CompositeMemoryIndex
from powercontext.builtin.persistence.oceanbase.memory_index import (
    OceanBaseMemoryFTSIndex,
    OceanBaseMemoryVectorIndex,
)
from powercontext.builtin.tags import TagFilter

_PROFILE = EmbeddingProfile(
    profile_id="memory-oceanbase-test-v1",
    model="test",
    dimension=3,
    distance="l2",
    normalization="unit",
)
_MEMORY_REF = ArtifactRef(family="memory", artifact_id="memory-a", revision=7)


def _request(
    mode: Literal["fts", "vector", "hybrid"],
    *,
    tag_filter: TagFilter | None = None,
) -> MemorySearchRequest:
    return MemorySearchRequest(
        query="alpha",
        analyzed_query="alpha",
        memories=(_MEMORY_REF,),
        candidate_limit=32,
        mode=mode,
        query_vector=None if mode == "fts" else (1.0, 0.0, 0.0),
        embedding_profile=None if mode == "fts" else _PROFILE,
        tag_filter=tag_filter,
    )


def test_oceanbase_hybrid_executes_one_statement_and_restores_channels() -> None:
    async def scenario() -> None:
        connection = AsyncMock(spec=AsyncConnection)
        connection.execute.return_value.mappings = MagicMock(
            return_value=[
                {
                    "source_channel": "vector",
                    "memory_artifact_id": "memory-a",
                    "head_revision": 1,
                    "entry_id": "entry-a",
                    "entry_version_id": "version-current",
                    "text": "Alpha current text.",
                    "distance": 0.25,
                },
                {
                    "source_channel": "fts",
                    "memory_artifact_id": "memory-a",
                    "head_revision": 1,
                    "entry_id": "entry-a",
                    "entry_version_id": "version-current",
                    "text": "Alpha current text.",
                    "distance": None,
                },
            ]
        )
        vector = OceanBaseMemoryVectorIndex(_PROFILE)
        index = CompositeMemoryIndex(OceanBaseMemoryFTSIndex(), vector)

        with patch.object(vector, "vector_complete", new=AsyncMock(return_value=True)):
            channels = await index.search(cast(AsyncConnection, connection), "scope-a", _request("hybrid"))

        assert connection.execute.await_count == 1
        statement = str(connection.execute.await_args.args[0])
        parameters = connection.execute.await_args.args[1]
        assert "pc_memory_entry_heads" in statement
        assert "pc_memory_vector_entries" in statement
        assert "MATCH (f.searchable_text) AGAINST" in statement
        assert "l2_distance(m.embedding, :query_vector)" in statement
        assert statement.count("LIMIT :candidate_limit") == 2
        assert statement.count("UNION ALL") == 1
        assert parameters["analyzed_query"] == "alpha"
        assert parameters["memory_artifact_ids"] == ("memory-a",)
        assert parameters["candidate_limit"] == 32

        vector_selection = statement[statement.index("vector_candidates AS") : statement.index("APPROXIMATE")]
        assert "JOIN" not in vector_selection
        assert statement.index("FROM vector_candidates AS n") > statement.index("APPROXIMATE")
        assert "AND h.entry_version_id = n.entry_version_id" in statement
        assert statement.count("AND v.entry_version_id = h.entry_version_id") == 2
        assert not re.search(r"\bhead_revision\s*(?:=|IN\b)", statement)
        assert "m.head_revision" not in statement
        assert (
            "ORDER BY source_channel, fts_score DESC, distance, memory_artifact_id, entry_id, entry_version_id"
            in statement
        )

        assert len(channels.fts) == 1
        assert len(channels.vector) == 1
        assert channels.fts[0].memory_ref == _MEMORY_REF
        assert channels.vector[0].memory_ref == _MEMORY_REF
        assert channels.fts[0].text == "Alpha current text."
        assert channels.fts[0].entry_version_id == "version-current"
        assert channels.vector[0].distance == 0.25

    asyncio.run(scenario())


def test_oceanbase_tagged_hybrid_filters_each_channel_before_exact_top_k() -> None:
    async def scenario() -> None:
        connection = AsyncMock(spec=AsyncConnection)
        connection.execute.return_value.mappings = MagicMock(return_value=[])
        vector = OceanBaseMemoryVectorIndex(_PROFILE)
        tag_filter = TagFilter(tags=("selected",))

        with patch.object(vector, "vector_complete", new=AsyncMock(return_value=True)):
            channels = await vector.search(
                cast(AsyncConnection, connection),
                "scope-a",
                _request("hybrid", tag_filter=tag_filter),
            )

        assert connection.execute.await_count == 1
        statement = str(connection.execute.await_args.args[0])
        parameters = connection.execute.await_args.args[1]
        assert statement.count("pc_artifact_tags") == 2
        assert statement.count("tag_key_hash IN") == 2
        assert statement.count("LIMIT :candidate_limit") == 2
        assert "APPROXIMATE" not in statement
        vector_limit = statement.index("LIMIT :candidate_limit", statement.index("vector_candidates AS"))
        vector_selection = statement[statement.index("vector_candidates AS") : vector_limit]
        assert "pc_artifact_tags" in vector_selection
        assert "JOIN" not in vector_selection
        assert parameters["tag_minimum"] == 1
        assert channels.fts == ()
        assert channels.vector == ()

    asyncio.run(scenario())


def test_oceanbase_single_channel_searches_keep_their_legacy_statements() -> None:
    async def scenario() -> None:
        connection = AsyncMock(spec=AsyncConnection)
        connection.execute.return_value.mappings = MagicMock(return_value=[])
        fts = OceanBaseMemoryFTSIndex()
        vector = OceanBaseMemoryVectorIndex(_PROFILE)

        fts_channels = await fts.search(cast(AsyncConnection, connection), "scope-a", _request("fts"))
        with patch.object(vector, "vector_complete", new=AsyncMock(return_value=True)):
            vector_channels = await vector.search(cast(AsyncConnection, connection), "scope-a", _request("vector"))

        assert connection.execute.await_count == 2
        fts_call, vector_call = connection.execute.await_args_list
        fts_statement = str(fts_call.args[0].compile(dialect=mysql.dialect()))
        vector_statement = str(vector_call.args[0])
        assert "MATCH (pc_memory_entry_heads.searchable_text) AGAINST" in fts_statement
        assert "pc_memory_vector_entries" not in fts_statement
        assert "l2_distance" not in fts_statement
        assert vector_call.args[0] is vector._search_sql
        assert "WITH fts_candidates" not in vector_statement
        assert "MATCH" not in vector_statement
        assert "ORDER BY l2_distance(m.embedding, :query_vector) APPROXIMATE" in vector_statement
        assert fts_channels.fts == ()
        assert fts_channels.vector == ()
        assert vector_channels.fts == ()
        assert vector_channels.vector == ()

    asyncio.run(scenario())
