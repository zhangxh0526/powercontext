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

"""Select query-relevant literal Memory evidence within a rendered byte budget."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.search import analyze_fts_query, analyze_text
from powercontext.builtin.runtime.prepared_text import TRUST_POLICY

_BEGIN_MARKER = "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1"
_END_MARKER = "END_POWERCONTEXT_PREPARED_CONTEXT_V1"
_MAX_WINDOW_SENTENCES = 3
_WINDOWS_PER_SIZE = 8
_MAX_CANDIDATES = 192
_MAX_BEAM_STATES = 24
_SENTENCE_CLOSERS = frozenset("\"'”’)]}»")  # noqa: RUF001
_CJK_SENTENCE_ENDS = frozenset("。！？")  # noqa: RUF001
_CJK_ANALYZER_TERM = re.compile(r"(?:u_[0-9a-f]+|b_[0-9a-f]+_[0-9a-f]+)\Z")
_ABBREVIATIONS = frozenset({
    "al",
    "approx",
    "ca",
    "cf",
    "dr",
    "eq",
    "fig",
    "figs",
    "mr",
    "mrs",
    "ms",
    "no",
    "prof",
    "ref",
    "refs",
    "st",
    "vs",
})


@dataclass(frozen=True)
class EvidenceSource:
    scope_id: str
    memory_ref: ArtifactRef
    entry_id: str
    entry_version_id: str
    text: str


@dataclass(frozen=True)
class EvidenceSpan:
    source_index: int
    start: int
    end: int


@dataclass(frozen=True)
class EvidenceSelection:
    content: str | None
    spans: tuple[EvidenceSpan, ...]
    dropped_items: int = 0


@dataclass(frozen=True)
class _Sentence:
    start: int
    end: int
    terms: tuple[str, ...]
    exact_terms: frozenset[str]
    stems: frozenset[str]


@dataclass
class _Document:
    source: EvidenceSource
    sentences: tuple[_Sentence, ...]
    term_frequencies: Counter[str]
    sentence_frequencies: Counter[str]
    priority: float = 0.0
    span_scores: dict[tuple[int, int], _SpanScore] = field(default_factory=dict)


@dataclass(frozen=True, order=True)
class _Span:
    source_index: int
    start: int
    end: int
    first_sentence: int
    last_sentence: int


@dataclass(frozen=True)
class _Candidate:
    span: _Span
    priority: float


@dataclass(frozen=True)
class _Query:
    terms: tuple[str, ...]
    stems: tuple[str, ...]
    weights: tuple[float, ...]
    rarity_weights: tuple[float, ...]
    phrases: tuple[tuple[str, ...], ...]
    literal: str
    total_weight: float
    total_rarity: float
    total_phrase_weight: float


@dataclass(frozen=True)
class _SpanScore:
    relevant: bool
    priority: float
    matches: tuple[float, ...]
    phrase_ids: frozenset[int]


@dataclass(frozen=True)
class _State:
    spans: tuple[_Span, ...]
    score: float
    content_bytes: int


def select_evidence(
    query: str,
    sources: Sequence[EvidenceSource],
    max_bytes: int,
    *,
    max_items: int,
) -> EvidenceSelection:
    documents = tuple(_make_document(source) for source in sources)
    if not documents or max_items < 1:
        return EvidenceSelection(content=None, spans=())
    analyzed_query = _make_query(query, documents)
    candidates = _make_candidates(documents, analyzed_query)
    if not candidates:
        return _fallback_selection(documents, max_bytes)
    best = _fit_candidates(documents, candidates, analyzed_query, max_bytes, max_items)
    if best is None:
        return EvidenceSelection(
            content=None,
            spans=(),
            dropped_items=len({candidate.span.source_index for candidate in candidates}),
        )
    dropped = _count_budget_drops(documents, candidates, best, max_bytes, max_items)
    return EvidenceSelection(
        content=_render(documents, best.spans),
        spans=tuple(EvidenceSpan(span.source_index, span.start, span.end) for span in best.spans),
        dropped_items=dropped,
    )


def _make_document(source: EvidenceSource) -> _Document:
    sentences = []
    term_frequencies: Counter[str] = Counter()
    sentence_frequencies: Counter[str] = Counter()
    for start, end in _sentence_spans(source.text):
        terms = tuple(analyze_text(source.text[start:end]).split())
        exact_terms = frozenset(terms)
        stems = frozenset(_stem(term) for term in terms)
        sentences.append(_Sentence(start, end, terms, exact_terms, stems))
        term_frequencies.update(_stem(term) for term in terms)
        sentence_frequencies.update(stems)
    return _Document(source, tuple(sentences), term_frequencies, sentence_frequencies)


def _make_query(value: str, documents: Sequence[_Document]) -> _Query:
    terms = tuple(dict.fromkeys(analyze_fts_query(value).split()))
    stems = tuple(_stem(term) for term in terms)
    document_count = len(documents)
    weights = []
    rarity_weights = []
    for term, stem in zip(terms, stems, strict=True):
        frequency = sum(
            any(term in sentence.exact_terms or stem in sentence.stems for sentence in document.sentences)
            for document in documents
        )
        inverse_frequency = math.log1p((document_count - frequency + 0.5) / (frequency + 0.5))
        weights.append(1.0 + inverse_frequency)
        rarity_weights.append(0.25 + inverse_frequency)

    raw_terms = tuple(analyze_text(value).split())
    query_terms = frozenset(terms)
    phrases = []
    for size in (2, 3):
        for index in range(len(raw_terms) - size + 1):
            phrase = raw_terms[index : index + size]
            if (
                all(term in query_terms and not _CJK_ANALYZER_TERM.fullmatch(term) for term in phrase)
                and phrase not in phrases
            ):
                phrases.append(phrase)
    return _Query(
        terms=terms,
        stems=stems,
        weights=tuple(weights),
        rarity_weights=tuple(rarity_weights),
        phrases=tuple(phrases),
        literal=unicodedata.normalize("NFC", value).casefold().strip(),
        total_weight=sum(weights),
        total_rarity=sum(rarity_weights),
        total_phrase_weight=sum(len(phrase) - 1 for phrase in phrases),
    )


def _make_candidates(documents: Sequence[_Document], query: _Query) -> tuple[_Candidate, ...]:
    candidates: list[_Candidate] = []
    for document_index, document in enumerate(documents):
        document.priority = _document_priority(document, query)
        for size in range(1, min(_MAX_WINDOW_SENTENCES, len(document.sentences)) + 1):
            window: list[_Candidate] = []
            for first in range(len(document.sentences) - size + 1):
                last = first + size - 1
                span = _span(document_index, document, first, last)
                score = _score_span(document, span, query)
                if score.relevant:
                    window.append(_Candidate(span, score.priority))
            window.sort(key=lambda candidate: (-candidate.priority, candidate.span))
            candidates.extend(window[:_WINDOWS_PER_SIZE])
    return _limit_candidates(candidates, len(documents))


def _limit_candidates(candidates: Sequence[_Candidate], document_count: int) -> tuple[_Candidate, ...]:
    if len(candidates) <= _MAX_CANDIDATES:
        return tuple(candidates)
    retained: list[_Candidate] = []
    for document_index in range(document_count):
        document_candidates = [candidate for candidate in candidates if candidate.span.source_index == document_index]
        document_candidates.sort(key=lambda candidate: (-candidate.priority, candidate.span))
        retained.extend(document_candidates[:2])
    retained_keys = {candidate.span for candidate in retained}
    for candidate in sorted(candidates, key=lambda candidate: (-candidate.priority, candidate.span)):
        if len(retained) >= _MAX_CANDIDATES:
            break
        if candidate.span not in retained_keys:
            retained.append(candidate)
            retained_keys.add(candidate.span)
    return tuple(retained)


def _document_priority(document: _Document, query: _Query) -> float:
    if not document.sentences:
        return 0.0
    if not query.terms:
        normalized = unicodedata.normalize("NFC", document.source.text).casefold()
        return float(bool(query.literal and query.literal in normalized))
    exact_terms: frozenset[str] = frozenset().union(*(sentence.exact_terms for sentence in document.sentences))
    stems: frozenset[str] = frozenset().union(*(sentence.stems for sentence in document.sentences))
    matches = _match_strengths(query, exact_terms, stems)
    terms = tuple(term for sentence in document.sentences for term in sentence.terms)
    phrase_score = _phrase_score(_phrase_matches(terms, query), query)
    return (
        _weighted_coverage(matches, query.weights, query.total_weight)
        + 0.25 * _weighted_coverage(matches, query.rarity_weights, query.total_rarity)
        + 0.5 * phrase_score
    )


def _score_span(document: _Document, span: _Span, query: _Query) -> _SpanScore:
    cache_key = (span.first_sentence, span.last_sentence)
    cached = document.span_scores.get(cache_key)
    if cached is not None:
        return cached
    sentences = document.sentences[span.first_sentence : span.last_sentence + 1]
    terms = tuple(term for sentence in sentences for term in sentence.terms)
    exact_terms: frozenset[str] = frozenset().union(*(sentence.exact_terms for sentence in sentences))
    stems: frozenset[str] = frozenset().union(*(sentence.stems for sentence in sentences))
    matches = _match_strengths(query, exact_terms, stems)
    coverage = _weighted_coverage(matches, query.weights, query.total_weight)
    rarity = _weighted_coverage(matches, query.rarity_weights, query.total_rarity)
    phrase_ids = _phrase_matches(terms, query)
    phrase_score = _phrase_score(phrase_ids, query)
    literal_match = False
    if not query.terms and query.literal:
        text = document.source.text[span.start : span.end]
        literal_match = query.literal in unicodedata.normalize("NFC", text).casefold()

    matched_weight = sum(weight * match for weight, match in zip(query.weights, matches, strict=True))
    consistency = (
        sum(
            query.weights[index] * match * min(1.0, document.term_frequencies[query.stems[index]] / 3)
            for index, match in enumerate(matches)
        )
        / matched_weight
        if matched_weight
        else 0.0
    )
    cohesion = _cohesion(document, span.first_sentence, span.last_sentence)
    density = 1 / (1 + max(0, len(terms) - 32) / 128)
    relevance = 4.5 * coverage + 1.5 * rarity + 2 * phrase_score + 4 * float(literal_match)
    priority = (
        relevance * (0.85 + 0.15 * density)
        + 0.5 * consistency
        + 0.35 * cohesion
        + 0.25 * document.priority
        + 0.12 * min(2, span.last_sentence - span.first_sentence) * (0.5 + 0.5 * cohesion)
    )
    result = _SpanScore(
        relevant=bool(any(matches) or literal_match),
        priority=priority,
        matches=matches,
        phrase_ids=phrase_ids,
    )
    document.span_scores[cache_key] = result
    return result


def _match_strengths(query: _Query, exact_terms: frozenset[str], stems: frozenset[str]) -> tuple[float, ...]:
    return tuple(
        1.0 if term in exact_terms else 0.72 if stem in stems else 0.0
        for term, stem in zip(query.terms, query.stems, strict=True)
    )


def _weighted_coverage(matches: Sequence[float], weights: Sequence[float], total: float) -> float:
    if not total:
        return 0.0
    return sum(weight * match for weight, match in zip(weights, matches, strict=True)) / total


def _phrase_matches(terms: tuple[str, ...], query: _Query) -> frozenset[int]:
    return frozenset(
        phrase_id
        for phrase_id, phrase in enumerate(query.phrases)
        if any(terms[index : index + len(phrase)] == phrase for index in range(len(terms) - len(phrase) + 1))
    )


def _phrase_score(phrase_ids: Collection[int], query: _Query) -> float:
    if not query.total_phrase_weight:
        return 0.0
    return sum(len(query.phrases[index]) - 1 for index in phrase_ids) / query.total_phrase_weight


def _cohesion(document: _Document, first: int, last: int) -> float:
    if first == last:
        return 0.0
    sentence_count = len(document.sentences)
    scores = []
    for index in range(first, last):
        shared = document.sentences[index].stems & document.sentences[index + 1].stems
        weight = 0.0
        for term in shared:
            frequency = document.sentence_frequencies[term]
            if len(term) >= 3 and frequency <= max(2, math.ceil(sentence_count * 0.6)):
                weight += 1 / frequency
        scores.append(min(1.0, weight / 2))
    return sum(scores) / len(scores)


def _fit_candidates(
    documents: Sequence[_Document],
    candidates: Sequence[_Candidate],
    query: _Query,
    max_bytes: int,
    max_items: int,
) -> _State | None:
    beam = [_State((), 0.0, 0)]
    seen: set[tuple[_Span, ...]] = {()}
    best: _State | None = None
    for _depth in range(max_items):
        generated: dict[tuple[_Span, ...], _State] = {}
        for state in beam:
            for candidate in candidates:
                spans = _merge_span(state.spans, candidate.span, documents)
                if spans == state.spans or spans in seen or len(spans) > max_items:
                    continue
                content_bytes = _rendered_bytes(documents, spans)
                if content_bytes > max_bytes:
                    continue
                generated[spans] = _State(spans, _selection_score(spans, documents, query), content_bytes)
        if not generated:
            break
        states = tuple(generated.values())
        for state in states:
            if _is_better(state, best):
                best = state
        seen.update(generated)
        beam = _diverse_beam(states, len(documents))
    return best


def _is_better(candidate: _State, current: _State | None) -> bool:
    if current is None or candidate.score > current.score + 1e-9:
        return True
    if abs(candidate.score - current.score) > 1e-9:
        return False
    return candidate.content_bytes < current.content_bytes or (
        candidate.content_bytes == current.content_bytes and candidate.spans < current.spans
    )


def _diverse_beam(states: Sequence[_State], document_count: int) -> list[_State]:
    by_score = sorted(states, key=lambda state: (-state.score, state.content_bytes, state.spans))
    by_efficiency = sorted(
        states,
        key=lambda state: (-state.score / max(1, state.content_bytes), state.content_bytes, state.spans),
    )
    beam = []
    selected = set()
    for state in (*by_score[:10], *by_efficiency[:4]):
        if state.spans not in selected:
            beam.append(state)
            selected.add(state.spans)
    for document_index in range(document_count):
        if len(beam) >= _MAX_BEAM_STATES:
            break
        state = next(
            (
                state
                for state in by_score
                if state.spans not in selected and any(span.source_index == document_index for span in state.spans)
            ),
            None,
        )
        if state is not None:
            beam.append(state)
            selected.add(state.spans)
    return beam


def _selection_score(spans: tuple[_Span, ...], documents: Sequence[_Document], query: _Query) -> float:
    matches = [0.0] * len(query.terms)
    phrase_ids: set[int] = set()
    priorities: dict[int, list[float]] = {}
    for span in spans:
        score = _score_span(documents[span.source_index], span, query)
        for index, match in enumerate(score.matches):
            matches[index] = max(matches[index], match)
        phrase_ids.update(score.phrase_ids)
        priorities.setdefault(span.source_index, []).append(score.priority)
    phrase_score = _phrase_score(phrase_ids, query)
    local_score = sum(max(values) + 0.2 * (sum(values) - max(values)) for values in priorities.values())
    return (
        5 * _weighted_coverage(matches, query.weights, query.total_weight)
        + 1.5 * _weighted_coverage(matches, query.rarity_weights, query.total_rarity)
        + 1.5 * phrase_score
        + local_score
    )


def _merge_span(spans: tuple[_Span, ...], candidate: _Span, documents: Sequence[_Document]) -> tuple[_Span, ...]:
    grouped: dict[int, list[_Span]] = {}
    for span in (*spans, candidate):
        grouped.setdefault(span.source_index, []).append(span)
    merged = []
    for source_index, source_spans in grouped.items():
        ordered = sorted(source_spans)
        current = ordered[0]
        for following in ordered[1:]:
            text = documents[source_index].source.text
            if following.start <= current.end or not text[current.end : following.start].strip():
                current = _Span(
                    source_index,
                    current.start,
                    max(current.end, following.end),
                    min(current.first_sentence, following.first_sentence),
                    max(current.last_sentence, following.last_sentence),
                )
            else:
                merged.append(current)
                current = following
        merged.append(current)
    return tuple(sorted(merged))


def _count_budget_drops(
    documents: Sequence[_Document],
    candidates: Sequence[_Candidate],
    best: _State,
    max_bytes: int,
    max_items: int,
) -> int:
    if len(best.spans) >= max_items:
        return 0
    represented = {span.source_index for span in best.spans}
    dropped = 0
    for source_index in range(len(documents)):
        if source_index in represented:
            continue
        source_candidates = [candidate for candidate in candidates if candidate.span.source_index == source_index]
        if not source_candidates:
            continue
        if not any(
            len(spans) <= max_items and _rendered_bytes(documents, spans) <= max_bytes
            for candidate in source_candidates
            for spans in (_merge_span(best.spans, candidate.span, documents),)
        ):
            dropped += 1
    return dropped


def _fallback_selection(documents: Sequence[_Document], max_bytes: int) -> EvidenceSelection:
    for source_index, document in enumerate(documents):
        if not document.sentences:
            continue
        span = _span(source_index, document, 0, 0)
        if _rendered_bytes(documents, (span,)) <= max_bytes:
            return EvidenceSelection(
                content=_render(documents, (span,)),
                spans=(EvidenceSpan(span.source_index, span.start, span.end),),
            )
    return EvidenceSelection(
        content=None,
        spans=(),
        dropped_items=sum(bool(document.sentences) for document in documents),
    )


def _span(source_index: int, document: _Document, first: int, last: int) -> _Span:
    return _Span(
        source_index,
        document.sentences[first].start,
        document.sentences[last].end,
        first,
        last,
    )


def _rendered_bytes(documents: Sequence[_Document], spans: tuple[_Span, ...]) -> int:
    return len(_render(documents, spans).encode("utf-8"))


def _render(documents: Sequence[_Document], spans: tuple[_Span, ...]) -> str:
    items = []
    for span in spans:
        source = documents[span.source_index].source
        items.append({
            "citation": {
                "scope_id": source.scope_id,
                "memory_ref": source.memory_ref.model_dump(mode="json"),
                "entry_id": source.entry_id,
                "entry_version_id": source.entry_version_id,
            },
            "content": source.text[span.start : span.end],
            "start": span.start,
            "end": span.end,
        })
    envelope = json.dumps({"trust": "untrusted_history", "items": items}, ensure_ascii=False, separators=(",", ":"))
    return "\n\n".join((TRUST_POLICY, f"{_BEGIN_MARKER}\n{envelope}\n{_END_MARKER}"))


def _sentence_spans(text: str) -> tuple[tuple[int, int], ...]:
    boundaries = []
    start = 0
    for index, character in enumerate(text):
        if character not in ".!?。！？" or not _is_sentence_end(text, index):  # noqa: RUF001
            continue
        end = index + 1
        while end < len(text) and text[end] in _SENTENCE_CLOSERS:
            end += 1
        sentence = _trim_span(text, start, end)
        if sentence is not None:
            boundaries.append(sentence)
        start = end
    final = _trim_span(text, start, len(text))
    if final is not None:
        boundaries.append(final)
    return tuple(boundaries)


def _is_sentence_end(text: str, index: int) -> bool:
    character = text[index]
    end = index + 1
    while end < len(text) and text[end] in _SENTENCE_CLOSERS:
        end += 1
    if character == ".":
        if end < len(text) and not text[end].isspace():
            return False
        word_start = index
        while word_start > 0 and text[word_start - 1].isalpha():
            word_start -= 1
        word = text[word_start:index].casefold()
        if len(word) == 1 or word in _ABBREVIATIONS:
            return False
    if character in _CJK_SENTENCE_ENDS:
        return True
    return end == len(text) or text[end].isspace()


def _trim_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None


def _stem(term: str) -> str:
    if not term.isascii() or not term.isalpha():
        return term
    if len(term) >= 6 and term.endswith("ies"):
        return term[:-3] + "y"
    if len(term) >= 6 and term.endswith("ing"):
        return term[:-3]
    if len(term) >= 5 and term.endswith("ed"):
        return term[:-2]
    if len(term) >= 8 and term.endswith("ion"):
        return term[:-3]
    if len(term) >= 8 and term.endswith("ive"):
        return term[:-3]
    if len(term) >= 8 and term.endswith("al"):
        return term[:-2]
    if len(term) >= 4 and term.endswith("s") and not term.endswith(("ss", "us", "is")):
        return term[:-1]
    return term


__all__ = ["EvidenceSelection", "EvidenceSource", "EvidenceSpan", "select_evidence"]
