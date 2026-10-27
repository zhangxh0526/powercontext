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

import pytest

from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel

PROFILE = EmbeddingProfile(profile_id="coalescing-tests", model="offline", dimension=1)


def _result(texts: tuple[str, ...], usage: InferenceUsage | None = None) -> EmbeddingResult:
    return EmbeddingResult(
        vectors=tuple((float(len(text)),) for text in texts),
        usage=usage or InferenceUsage(requests=1, input_tokens=len(texts), output_tokens=0),
    )


class RecordingEmbedding:
    profile = PROFILE

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: tuple[str, ...]) -> EmbeddingResult:
        texts = tuple(texts)
        self.calls.append(texts)
        return _result(texts)


class BlockedEmbedding(RecordingEmbedding):
    def __init__(self, usage: InferenceUsage | None = None) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.usage = usage

    async def embed(self, texts: tuple[str, ...]) -> EmbeddingResult:
        texts = tuple(texts)
        self.calls.append(texts)
        self.started.set()
        await self.release.wait()
        return _result(texts, self.usage)


def test_zero_delay_budget_for_six_concurrent_identical_batches() -> None:
    async def scenario() -> None:
        base = RecordingEmbedding()
        model = CoalescingEmbeddingModel(base, max_batch_size=6, coalesce_ms=0, cache_size=48)
        texts = tuple(f"needle-{index:02d} fixture" for index in range(24))

        results = await asyncio.gather(*(model.embed(texts) for _ in range(6)))

        assert len(base.calls) == 4
        assert all(len(call) == 6 for call in base.calls)
        assert sorted(text for call in base.calls for text in call) == sorted(texts)
        for result in results:
            assert result.vectors == _result(texts).vectors
            assert result.usage.requests == 4
            assert result.usage.input_tokens == 24
            assert result.usage.output_tokens == 0

        cached = await asyncio.gather(*(model.embed((texts[index],)) for index in range(6)))
        assert len(base.calls) == 4
        for result in cached:
            assert result.usage.requests == 0
            assert result.usage.input_tokens is None
            assert result.usage.output_tokens is None

    asyncio.run(scenario())


def test_late_caller_joins_running_batch_and_restores_duplicate_order() -> None:
    async def scenario() -> None:
        base = BlockedEmbedding()
        model = CoalescingEmbeddingModel(base, max_batch_size=2, coalesce_ms=0)
        first = asyncio.create_task(model.embed(("a", "bb")))
        await asyncio.wait_for(base.started.wait(), timeout=1)

        late = asyncio.create_task(model.embed(("bb", "a", "bb")))
        await asyncio.sleep(0)
        assert base.calls == [("a", "bb")]

        base.release.set()
        first_result, late_result = await asyncio.gather(first, late)
        assert first_result.vectors == ((1.0,), (2.0,))
        assert late_result.vectors == ((2.0,), (1.0,), (2.0,))
        assert first_result.usage.requests == 1
        assert late_result.usage.requests == 1
        assert late_result.usage.input_tokens == 2
        assert base.calls == [("a", "bb")]

    asyncio.run(scenario())


def test_cache_disabled_still_shares_inflight_work() -> None:
    async def scenario() -> None:
        base = BlockedEmbedding()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0, cache_size=0)
        first = asyncio.create_task(model.embed(("shared",)))
        await asyncio.wait_for(base.started.wait(), timeout=1)
        second = asyncio.create_task(model.embed(("shared", "shared")))
        await asyncio.sleep(0)
        assert len(base.calls) == 1

        base.release.set()
        first_result, second_result = await asyncio.gather(first, second)
        assert first_result.vectors == ((6.0,),)
        assert second_result.vectors == ((6.0,), (6.0,))
        await model.embed(("shared",))
        assert base.calls == [("shared",), ("shared",)]

    asyncio.run(scenario())


def test_partial_failure_preserves_independent_success_and_allows_retry() -> None:
    class FlakyEmbedding(RecordingEmbedding):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        async def embed(self, texts: tuple[str, ...]) -> EmbeddingResult:
            texts = tuple(texts)
            self.calls.append(texts)
            if texts == ("bad",) and not self.failed:
                self.failed = True
                raise RuntimeError("temporary")
            return _result(texts)

    async def scenario() -> None:
        base = FlakyEmbedding()
        model = CoalescingEmbeddingModel(base, max_batch_size=1, coalesce_ms=0)
        failing = asyncio.create_task(model.embed(("bad", "good")))
        survivor = asyncio.create_task(model.embed(("good", "good")))

        with pytest.raises(RuntimeError, match="temporary"):
            await failing
        survived = await survivor
        assert survived.vectors == ((4.0,), (4.0,))
        assert survived.usage.requests == 1
        assert len(base.calls) == 2
        assert sum(call.count("good") for call in base.calls) == 1

        assert (await model.embed(("bad",))).vectors == ((3.0,),)
        cached = await model.embed(("good",))
        assert cached.usage.requests == 0
        assert len(base.calls) == 3
        assert base.calls[-1] == ("bad",)

    asyncio.run(scenario())


def test_invalid_vector_count_is_not_cached_and_can_be_retried() -> None:
    class InvalidOnceEmbedding(RecordingEmbedding):
        async def embed(self, texts: tuple[str, ...]) -> EmbeddingResult:
            texts = tuple(texts)
            self.calls.append(texts)
            if len(self.calls) == 1:
                return EmbeddingResult(vectors=())
            return _result(texts)

    async def scenario() -> None:
        base = InvalidOnceEmbedding()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)
        with pytest.raises(ValueError):
            await model.embed(("retry",))

        assert (await model.embed(("retry",))).vectors == ((5.0,),)
        assert (await model.embed(("retry",))).usage.requests == 0
        assert base.calls == [("retry",), ("retry",)]

    asyncio.run(scenario())


def test_cancellation_isolated_from_shared_work_and_usage() -> None:
    async def scenario() -> None:
        usage = InferenceUsage(requests=2, input_tokens=9, output_tokens=4)
        base = BlockedEmbedding(usage)
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)
        cancelled = asyncio.create_task(model.embed(("shared",)))
        survivor = asyncio.create_task(model.embed(("shared", "shared")))
        await asyncio.wait_for(base.started.wait(), timeout=1)

        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        late = asyncio.create_task(model.embed(("shared",)))
        await asyncio.sleep(0)
        assert len(base.calls) == 1

        base.release.set()
        survived, late_result = await asyncio.gather(survivor, late)
        for result in (survived, late_result):
            assert result.usage.requests == 2
            assert result.usage.input_tokens == 9
            assert result.usage.output_tokens == 4
        assert survived.vectors == ((6.0,), (6.0,))
        assert late_result.vectors == ((6.0,),)

        cached = await model.embed(("shared",))
        assert cached.usage.requests == 0
        assert cached.usage.input_tokens is None
        assert cached.usage.output_tokens is None
        assert len(base.calls) == 1

    asyncio.run(scenario())


def test_usage_counts_distinct_batches_once_and_preserves_unknown_tokens() -> None:
    class MeteredEmbedding(RecordingEmbedding):
        async def embed(self, texts: tuple[str, ...]) -> EmbeddingResult:
            texts = tuple(texts)
            self.calls.append(texts)
            usage = (
                InferenceUsage(requests=2, input_tokens=7, output_tokens=1)
                if "a" in texts
                else InferenceUsage(requests=1, input_tokens=None, output_tokens=3)
            )
            return _result(texts, usage)

    async def scenario() -> None:
        base = MeteredEmbedding()
        model = CoalescingEmbeddingModel(base, max_batch_size=2, coalesce_ms=0)
        first, second = await asyncio.gather(
            model.embed(("a", "bb", "a", "ccc")),
            model.embed(("ccc", "a")),
        )

        assert len(base.calls) == 2
        for result in (first, second):
            assert result.usage.requests == 3
            assert result.usage.input_tokens is None
            assert result.usage.output_tokens == 4
        assert first.vectors == ((1.0,), (2.0,), (1.0,), (3.0,))
        assert second.vectors == ((3.0,), (1.0,))

        cached = await model.embed(("a", "bb", "ccc"))
        assert cached.usage.requests == 0
        assert cached.usage.input_tokens is None
        assert cached.usage.output_tokens is None

    asyncio.run(scenario())


def test_lru_refresh_empty_input_and_profile() -> None:
    async def scenario() -> None:
        base = RecordingEmbedding()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0, cache_size=2)
        assert model.profile is base.profile

        empty = await model.embed(())
        assert empty.vectors == ()
        assert empty.usage.requests == 0
        assert empty.usage.input_tokens is None
        assert base.calls == []

        await model.embed(("alpha",))
        await model.embed(("beta",))
        await model.embed(("alpha",))
        await model.embed(("gamma",))
        await model.embed(("alpha",))
        await model.embed(("beta",))
        assert base.calls == [("alpha",), ("beta",), ("gamma",), ("beta",)]

    asyncio.run(scenario())


def test_instances_do_not_share_cached_vectors() -> None:
    async def scenario() -> None:
        base = RecordingEmbedding()
        first = CoalescingEmbeddingModel(base, coalesce_ms=0)
        second = CoalescingEmbeddingModel(base, coalesce_ms=0)

        await first.embed(("same",))
        await second.embed(("same",))
        await first.embed(("same",))

        assert base.calls == [("same",), ("same",)]

    asyncio.run(scenario())
