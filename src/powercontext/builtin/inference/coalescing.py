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

"""Coalesce concurrent embedding requests and cache successful vectors."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel


@dataclass(eq=False, slots=True)
class _Batch:
    texts: list[str]
    accepting: bool = True
    task: asyncio.Task[EmbeddingResult | Exception] = field(init=False)


class CoalescingEmbeddingModel:
    """Share in-flight text embeddings and keep a bounded per-instance LRU cache."""

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if max_batch_size < 1:
            raise ValueError("max_batch_size must be positive")  # noqa: TRY003
        if coalesce_ms < 0:
            raise ValueError("coalesce_ms must not be negative")  # noqa: TRY003
        if cache_size < 0:
            raise ValueError("cache_size must not be negative")  # noqa: TRY003
        self.profile = model.profile
        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_delay = coalesce_ms / 1000
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._inflight: dict[str, _Batch] = {}
        self._pending_batch: _Batch | None = None

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Embed texts in their original order, including duplicate positions."""

        texts = tuple(texts)
        if not texts:
            return EmbeddingResult(vectors=())

        vectors: dict[str, EmbeddingVector] = {}
        batches: dict[_Batch, None] = {}
        for text in texts:
            cached = self._cache.get(text)
            if cached is not None:
                self._cache.move_to_end(text)
                vectors[text] = cached
                continue
            batch = self._inflight.get(text)
            if batch is None:
                batch = self._add_text(text)
            batches.setdefault(batch, None)

        if not batches:
            return EmbeddingResult(vectors=tuple(vectors[text] for text in texts))

        outcomes = await asyncio.gather(
            *(asyncio.shield(batch.task) for batch in batches),
            return_exceptions=True,
        )
        usages: list[InferenceUsage] = []
        for batch, outcome in zip(batches, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                raise outcome
            usages.append(outcome.usage)
            for text, vector in zip(batch.texts, outcome.vectors, strict=True):
                vectors[text] = vector

        return EmbeddingResult(
            vectors=tuple(vectors[text] for text in texts),
            usage=InferenceUsage(
                requests=sum(usage.requests for usage in usages),
                input_tokens=_token_total([usage.input_tokens for usage in usages]),
                output_tokens=_token_total([usage.output_tokens for usage in usages]),
            ),
        )

    def _add_text(self, text: str) -> _Batch:
        batch = self._pending_batch
        if batch is None or not batch.accepting or len(batch.texts) >= self._max_batch_size:
            batch = _Batch(texts=[text])
            self._pending_batch = batch
            batch.task = asyncio.create_task(self._run_batch(batch))
        else:
            batch.texts.append(text)
        self._inflight[text] = batch
        return batch

    async def _run_batch(self, batch: _Batch) -> EmbeddingResult | Exception:
        try:
            await asyncio.sleep(self._coalesce_delay)
            batch.accepting = False
            if self._pending_batch is batch:
                self._pending_batch = None
            texts = tuple(batch.texts)
            result = await self._model.embed(texts)
            self._cache_result(texts, result)
        except Exception as error:
            return error
        else:
            return result
        finally:
            batch.accepting = False
            if self._pending_batch is batch:
                self._pending_batch = None
            for text in batch.texts:
                if self._inflight.get(text) is batch:
                    del self._inflight[text]

    def _cache_result(self, texts: tuple[str, ...], result: EmbeddingResult) -> None:
        if len(result.vectors) != len(texts):
            raise ValueError("embedding model returned the wrong vector count")  # noqa: TRY003
        if self._cache_size > 0:
            for text, vector in zip(texts, result.vectors, strict=True):
                self._cache[text] = vector
                self._cache.move_to_end(text)
                if len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)


def _token_total(values: list[int | None]) -> int | None:
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


__all__ = ["CoalescingEmbeddingModel"]
