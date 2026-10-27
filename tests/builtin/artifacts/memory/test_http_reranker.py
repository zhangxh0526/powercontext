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

"""Protocol and error regressions for HTTP Memory reranking."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.artifacts.memory.models import MemoryHit


def _hit(rank: int) -> MemoryHit:
    return MemoryHit(
        memory_ref=ArtifactRef(family="memory", artifact_id="memory", revision=1),
        entry_id=f"entry-{rank}",
        entry_version_id=f"entry-{rank}-v1",
        text=f"candidate {rank}",
        score=1 / rank,
        matched_by=("fts",),
    )


def test_sends_exact_batches_and_globally_sorts_out_of_order_scores() -> None:
    requests: list[dict[str, object]] = []
    scores = {1: 0.1, 2: 0.8, 3: 0.8, 4: 0.2, 5: 0.8}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "scores": [
                    {"rank": item["rank"], "score": scores[item["rank"]]}
                    for item in reversed(body["candidates"])
                ]
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert HTTPMemoryReranker("https://invalid", client=client).policy_id == "http-cross-encoder-v1"
            reranker = HTTPMemoryReranker(
                "https://invalid",
                client=client,
                batch_size=2,
                policy_id="custom-policy",
            )
            decision = await reranker.rerank("query", tuple(_hit(rank) for rank in range(1, 6)), 5)
            assert reranker.policy_id == "custom-policy"

        assert decision.selected_ranks == (2, 3, 5, 4, 1)
        assert decision.usage.requests == 3
        assert decision.discarded_rank_count == 0
        assert decision.used_fallback is False

    asyncio.run(scenario())
    assert requests == [
        {
            "query": "query",
            "candidates": [
                {"rank": 1, "text": "candidate 1"},
                {"rank": 2, "text": "candidate 2"},
            ],
        },
        {
            "query": "query",
            "candidates": [
                {"rank": 3, "text": "candidate 3"},
                {"rank": 4, "text": "candidate 4"},
            ],
        },
        {
            "query": "query",
            "candidates": [{"rank": 5, "text": "candidate 5"}],
        },
    ]


@pytest.mark.parametrize("batch_size", [0, -1, True, 1.5, "2", None])
def test_rejects_invalid_batch_size(batch_size) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError):
                HTTPMemoryReranker("https://invalid", client=client, batch_size=batch_size)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("candidate_count", "limit"),
    [
        (0, 1),
        (1, 0),
        (1, -1),
        (1, 2),
        (1, True),
        (1, 1.0),
        (1, "1"),
        (1, None),
    ],
)
def test_rejects_invalid_rerank_bounds_before_http(candidate_count, limit) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid bounds must not issue an HTTP request")

    async def scenario() -> None:
        candidates = tuple(_hit(rank) for rank in range(1, candidate_count + 1))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker("https://invalid", client=client)
            with pytest.raises(ValueError):
                await reranker.rerank("query", candidates, limit)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"scores": None},
        {"scores": {}},
        {"scores": []},
        {"scores": [None]},
        {"scores": [{"score": 0.5}]},
        {"scores": [{"rank": 1}]},
        {"scores": [{"rank": True, "score": 0.5}]},
        {"scores": [{"rank": 1.0, "score": 0.5}]},
        {"scores": [{"rank": "1", "score": 0.5}]},
        {"scores": [{"rank": 1, "score": True}]},
        {"scores": [{"rank": 1, "score": "0.5"}]},
        {"scores": [{"rank": 1, "score": None}]},
        {"scores": [{"rank": 1, "score": float("-inf")}]},
    ],
)
def test_rejects_malformed_response_schema_and_non_numeric_scores(payload) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=json.dumps(payload).encode()))

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            reranker = HTTPMemoryReranker("https://invalid", client=client)
            with pytest.raises(ValueError):
                await reranker.rerank("query", (_hit(1),), 1)

    asyncio.run(scenario())


def test_rejects_a_rank_from_another_batch() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={"scores": [{"rank": 2, "score": 0.5}]})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker("https://invalid", client=client, batch_size=1)
            with pytest.raises(ValueError):
                await reranker.rerank("query", (_hit(1), _hit(2)), 1)

    asyncio.run(scenario())
    assert len(requests) == 1


def test_rejects_invalid_json() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, content=b'{"scores":[', headers={"Content-Type": "application/json"})
    )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            reranker = HTTPMemoryReranker("https://invalid", client=client)
            with pytest.raises(ValueError):
                await reranker.rerank("query", (_hit(1),), 1)

    asyncio.run(scenario())


def test_raise_for_status_precedes_json_validation() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(503, content=b"not JSON"))

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            reranker = HTTPMemoryReranker("https://invalid", client=client)
            with pytest.raises(httpx.HTTPStatusError) as caught:
                await reranker.rerank("query", (_hit(1),), 1)

        assert caught.value.response.status_code == 503
        assert caught.value.request.url == httpx.URL("https://invalid")

    asyncio.run(scenario())


def test_network_error_is_propagated_without_wrapping() -> None:
    endpoint = "https://reranker.invalid/score"
    error = httpx.ConnectError("offline", request=httpx.Request("POST", endpoint))

    def handler(_request: httpx.Request) -> httpx.Response:
        raise error

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker(endpoint, client=client)
            with pytest.raises(httpx.ConnectError) as caught:
                await reranker.rerank("query", (_hit(1),), 1)

        assert caught.value is error

    asyncio.run(scenario())
