# Copyright (c) 2026 OceanBase.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software distributed
# under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.

"""Preserve native vector ranking as the no-FTS hybrid policy."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemoryEntryInput
from powercontext.builtin.inference import EmbeddingResult
from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts

pytest.importorskip("pylibseekdb")

_PROFILE = EmbeddingProfile(
    profile_id="hybrid-native-order-test-v1",
    model="deterministic",
    dimension=3,
    distance="l2",
    normalization="unit",
)


class _Embedding:
    profile = _PROFILE

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        return EmbeddingResult(vectors=tuple((0.0, 1.0, 0.0) if "far" in text else (1.0, 0.0, 0.0) for text in texts))


def test_hybrid_preserves_native_vector_ranking_without_lexical_matches(short_tmp_path: Path) -> None:
    async def scenario() -> None:
        async with open_builtin_contexts(
            BuiltinConfig(database=SeekDBConfig(path=short_tmp_path / "db")), embedding_model=_Embedding()
        ) as contexts:
            memory = (await contexts.get("native-order")).artifacts.memory
            head = await memory.remember(
                memory=None,
                entries=tuple(
                    MemoryEntryInput(kind="fact", text=f"{group} record {index:03d}")
                    for group, count in (("near", 15), ("far", 158))
                    for index in range(count)
                ),
                mode="append",
            )
            assert head is not None
            vector = await memory.search("quasarunmatchedtoken", memories=(head,), mode="vector", limit=10)
            hybrid = await memory.search("quasarunmatchedtoken", memories=(head,), mode="hybrid", limit=10)

            repeated = await memory.search("quasarunmatchedtoken", memories=(head,), mode="hybrid", limit=10)

            assert len(vector.hits) == len(hybrid.hits) == len(repeated.hits) == 10
            assert all(hit.matched_by == ("vector",) for hit in hybrid.hits)
            vector_anchors = [(hit.memory_ref, hit.entry_id, hit.entry_version_id, hit.text) for hit in vector.hits]
            hybrid_anchors = [(hit.memory_ref, hit.entry_id, hit.entry_version_id, hit.text) for hit in hybrid.hits]
            repeated_anchors = [(hit.memory_ref, hit.entry_id, hit.entry_version_id, hit.text) for hit in repeated.hits]
            assert hybrid_anchors == vector_anchors
            assert repeated_anchors == hybrid_anchors

    asyncio.run(scenario())
