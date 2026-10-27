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

"""HTTP scoring and global reranking for coarse Memory candidates."""

from __future__ import annotations

import math
from typing import cast

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage


class HTTPMemoryReranker:
    """Select Memory hits using scores from a caller-provided HTTP service."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = "http-cross-encoder-v1",
    ) -> None:
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
            raise ValueError("rerank batch_size must be a positive integer")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score every candidate and select globally by score and original rank."""
        candidate_count = len(candidates)
        if candidate_count < 1:
            raise ValueError("rerank requires at least one candidate")  # noqa: TRY003
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= candidate_count:
            raise ValueError("rerank limit must be between one and the candidate count")  # noqa: TRY003

        scored_ranks: list[tuple[int, int | float]] = []
        requests = 0
        for start in range(0, candidate_count, self._batch_size):
            batch = candidates[start : start + self._batch_size]
            expected_ranks = frozenset(range(start + 1, start + len(batch) + 1))
            response = await self._client.post(
                self._endpoint,
                json={
                    "query": query,
                    "candidates": [
                        {"rank": rank, "text": candidate.text} for rank, candidate in enumerate(batch, start=start + 1)
                    ],
                },
            )
            requests += 1
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as error:
                raise ValueError("reranker returned invalid JSON") from error  # noqa: TRY003
            scored_ranks.extend(_validate_response(payload, expected_ranks=expected_ranks))

        ordered = sorted(scored_ranks, key=lambda item: (-item[1], item[0]))
        return MemoryRerankDecision(
            selected_ranks=tuple(rank for rank, _score in ordered[:limit]),
            usage=InferenceUsage(requests=requests),
        )


def _validate_response(
    payload: object,
    *,
    expected_ranks: frozenset[int],
) -> list[tuple[int, int | float]]:
    if not isinstance(payload, dict) or payload.keys() != {"scores"}:
        raise ValueError("reranker response must contain exactly the scores field")  # noqa: TRY003
    payload = cast(dict[str, object], payload)
    scores = payload["scores"]
    if not isinstance(scores, list):
        raise ValueError("reranker scores must be a list")  # noqa: TRY003, TRY004

    validated: list[tuple[int, int | float]] = []
    observed_ranks: set[int] = set()
    for item in scores:
        if not isinstance(item, dict):
            raise ValueError("reranker score entries must contain exactly rank and score")  # noqa: TRY003, TRY004
        item = cast(dict[str, object], item)
        if item.keys() != {"rank", "score"}:
            raise ValueError("reranker score entries must contain exactly rank and score")  # noqa: TRY003
        rank = item["rank"]
        score = item["score"]
        if not isinstance(rank, int) or isinstance(rank, bool) or rank not in expected_ranks or rank in observed_ranks:
            raise ValueError("reranker scores must uniquely cover the requested batch ranks")  # noqa: TRY003
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError("reranker scores must be finite numbers")  # noqa: TRY003, TRY004
        if isinstance(score, float) and not math.isfinite(score):
            raise ValueError("reranker scores must be finite numbers")  # noqa: TRY003
        observed_ranks.add(rank)
        validated.append((rank, score))

    if observed_ranks != expected_ranks:
        raise ValueError("reranker scores must uniquely cover the requested batch ranks")  # noqa: TRY003
    return validated


__all__ = ["HTTPMemoryReranker"]
