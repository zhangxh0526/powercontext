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

"""Captured physical-page Sources and deterministic offline retrieval for PDF reports."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, field_validator

from powercontext.artifacts import MemoryCitation
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import MemoryHit

PDF_PAGE_SOURCE_NAME = "pdf-page"
_TOKEN_PATTERN = re.compile(r"[a-z]+|\d+")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_MAX_SEARCH_CANDIDATES = 300

_STOP_WORDS = frozenset({
    "a",
    "an",
    "analyst",
    "and",
    "answer",
    "any",
    "are",
    "as",
    "assuming",
    "at",
    "be",
    "been",
    "being",
    "by",
    "calculate",
    "calculated",
    "calculating",
    "calculation",
    "can",
    "company",
    "could",
    "define",
    "defined",
    "details",
    "did",
    "directly",
    "do",
    "does",
    "explicitly",
    "for",
    "from",
    "give",
    "given",
    "had",
    "has",
    "have",
    "having",
    "how",
    "i",
    "if",
    "important",
    "in",
    "investment",
    "is",
    "it",
    "its",
    "of",
    "on",
    "only",
    "or",
    "our",
    "outlined",
    "perspective",
    "please",
    "primarily",
    "quantity",
    "question",
    "rely",
    "relying",
    "report",
    "reported",
    "respond",
    "response",
    "should",
    "shown",
    "state",
    "that",
    "the",
    "their",
    "then",
    "these",
    "this",
    "those",
    "to",
    "use",
    "using",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
    "within",
    "would",
    "you",
    "your",
    "address",
    "based",
    "clearly",
    "data",
    "decimal",
    "definition",
    "line",
    "items",
    "information",
    "place",
    "places",
    "round",
    "rounded",
    "units",
    "utilizing",
})
_TERM_SYNONYMS = {
    "amortisation": ("amortization",),
    "amortization": ("amortisation",),
    "battle": ("dispute", "litigation", "proceeding"),
    "battles": ("disputes", "litigation", "proceedings"),
    "cost": ("charge", "expense"),
    "costs": ("charges", "expenses"),
    "earnings": ("income",),
    "expense": ("charge", "cost"),
    "expenses": ("charges", "costs"),
    "fy": ("fiscal",),
    "geographic": ("geographical", "regional"),
    "geographical": ("geographic", "regional"),
    "geographies": ("geographic", "geographical", "regions"),
    "geography": ("geographic", "geographical", "region"),
    "income": ("earnings",),
    "legal": ("litigation",),
    "ongoing": ("pending",),
    "operate": ("operating", "operations"),
    "operates": ("operating", "operations"),
    "operations": ("operating",),
    "regions": ("geographies", "geographic"),
    "revenue": ("sales",),
    "revenues": ("sales",),
    "sales": ("revenue", "revenues"),
}
_PHRASE_SYNONYMS: dict[tuple[str, ...], tuple[tuple[str, ...], ...]] = {
    ("capex",): (
        ("capital", "expenditures"),
        ("capital", "spending"),
        ("purchases", "of", "property", "plant", "and", "equipment"),
    ),
    ("capital", "expenditure"): (("capital", "spending"),),
    ("capital", "expenditures"): (("capital", "spending"),),
    ("capital", "intensive"): (
        ("net", "sales"),
        ("consolidated", "statement", "of", "income"),
        ("consolidated", "balance", "sheet"),
        ("statement", "of", "cash", "flows"),
        ("capital", "expenditures"),
        ("fixed", "assets"),
        ("total", "assets"),
    ),
    ("capital", "intensity"): (("capital", "expenditures"), ("fixed", "assets"), ("net", "sales")),
    ("cash", "flow", "statement"): (
        ("statement", "of", "cash", "flows"),
        ("statements", "of", "cash", "flows"),
    ),
    ("cash", "flow", "statements"): (
        ("statement", "of", "cash", "flows"),
        ("statements", "of", "cash", "flows"),
    ),
    ("statement", "of", "cash", "flows"): (("cash", "flow", "statement"),),
    ("statements", "of", "cash", "flows"): (("cash", "flow", "statements"),),
    ("customer", "concentration"): (
        ("customer", "accounted"),
        ("major", "customer"),
        ("significant", "customer"),
        ("customer", "revenue"),
    ),
    ("dpo",): (("accounts", "payable"), ("cost", "of", "sales"), ("inventories",), ("days", "payable", "outstanding")),
    ("ebit",): (("earnings", "before", "interest", "and", "taxes"),),
    ("ebitda",): (("earnings", "before", "interest", "taxes", "depreciation", "and", "amortization"),),
    ("effective", "tax", "rate"): (
        ("income", "tax", "expense"),
        ("provision", "for", "income", "taxes"),
        ("income", "before", "income", "taxes"),
        ("statements", "of", "operations"),
    ),
    ("geographies",): (
        ("africa", "middle", "east", "and", "south", "asia"),
        ("asia", "pacific", "australia", "and", "new", "zealand", "and", "china", "region"),
        ("reportable", "segments"),
        ("our", "operations"),
        ("company", "overview"),
        ("countries", "and", "territories"),
    ),
    ("geography",): (("reportable", "segments"), ("our", "operations"), ("company", "overview")),
    ("gross", "margin"): (
        ("cost", "of", "products"),
        ("cost", "of", "services"),
        ("sales", "of", "products"),
        ("sales", "of", "services"),
    ),
    ("income", "statement"): (
        ("statement", "of", "income"),
        ("statements", "of", "income"),
        ("statement", "of", "operations"),
    ),
    ("income", "statements"): (
        ("statement", "of", "income"),
        ("statements", "of", "income"),
        ("statements", "of", "operations"),
    ),
    ("legal", "battle"): (("legal", "proceedings"), ("litigation", "proceedings")),
    ("legal", "battles"): (("legal", "proceedings"), ("litigation", "proceedings")),
    ("net", "income"): (("net", "earnings"),),
    ("operating", "income"): (("operating", "profit"),),
    ("primary", "customers"): (
        ("commercial", "airlines"),
        ("u", "s", "government"),
        ("government", "contracts"),
        ("limited", "number"),
    ),
    ("product", "categories"): (
        ("summary", "of", "business", "segment", "data"),
        ("revenues", "commercial", "airplanes"),
        ("defense", "space", "and", "security"),
        ("global", "services"),
        ("commercial", "airplanes"),
        ("defense", "space", "security"),
        ("business", "segment", "data"),
    ),
    ("quick", "ratio"): (
        ("current", "assets"),
        ("current", "liabilities"),
        ("cash", "and", "cash", "equivalents"),
        ("short", "term", "investments"),
    ),
    ("restructuring", "cost"): (("restructuring", "charges"), ("restructuring", "expenses")),
    ("restructuring", "costs"): (("restructuring", "charges"), ("restructuring", "expenses")),
    ("revenue", "change"): (
        ("net", "sales"),
        ("total", "net", "sales"),
        ("net", "product", "sales"),
        ("net", "service", "sales"),
    ),
    ("change", "in", "revenue"): (
        ("net", "sales"),
        ("total", "net", "sales"),
        ("consolidated", "statements", "of", "operations"),
        ("net", "product", "sales"),
    ),
    ("year", "over", "year"): (
        ("net", "sales"),
        ("total", "net", "sales"),
        ("consolidated", "statements", "of", "operations"),
        ("increase", "in", "net", "sales"),
    ),
    ("dragged", "down"): (
        ("organic", "sales"),
        ("total", "sales", "change"),
        ("acquisitions", "divestitures"),
        ("business", "segment"),
    ),
    ("overall", "growth"): (
        ("organic", "sales"),
        ("total", "sales", "change"),
        ("acquisitions", "divestitures"),
        ("business", "segment"),
    ),
    ("segment", "growth"): (
        ("organic", "sales"),
        ("acquisitions", "divestitures"),
        ("total", "sales", "change"),
        ("business", "segment"),
    ),
    ("service", "categories"): (
        ("summary", "of", "business", "segment", "data"),
        ("revenues", "commercial", "airplanes"),
        ("defense", "space", "and", "security"),
        ("global", "services"),
        ("commercial", "airplanes"),
        ("defense", "space", "security"),
        ("business", "segment", "data"),
    ),
}


def _validate_document_id(value: str) -> str:
    if not value.strip():
        raise ValueError("document_id must not be empty or blank")  # noqa: TRY003
    return value


class PDFDocumentInput(BaseModel):
    document_id: str
    content: bytes

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        return _validate_document_id(value)


class PDFPageInput(BaseModel):
    document_id: str
    document_sha256: str
    page_number: int
    text: str

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        return _validate_document_id(value)

    @field_validator("document_sha256")
    @classmethod
    def validate_document_sha256(cls, value: str) -> str:
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("document_sha256 must be 64 lowercase hexadecimal characters")  # noqa: TRY003
        return value

    @field_validator("page_number")
    @classmethod
    def validate_page_number(cls, value: int) -> int:
        if value < 1:
            raise ValueError("page_number must start from 1")  # noqa: TRY003
        return value


class PDFPageSource(Source):
    document_id: str
    document_sha256: str
    page_number: int
    text: str


def _pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    identity = json.dumps(
        [document_id, document_sha256, page_number],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return f"pdf_page_{hashlib.sha256(identity).hexdigest()}"


def _decrypt_pdf(reader, /) -> None:
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("PDF requires a non-empty password")  # noqa: TRY003


def _extract_pdf_page_text(page, /) -> str:
    text = page.extract_text()
    if text is None:
        return ""
    if not isinstance(text, str):
        raise TypeError("PDF page extraction must return text")  # noqa: TRY003
    return text


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Extract every physical page, failing without returning a partial document."""

    _validate_document_id(value.document_id)
    if not value.content:
        raise ValueError("PDF content must not be empty")  # noqa: TRY003
    document_sha256 = hashlib.sha256(value.content).hexdigest()
    try:
        pypdf = importlib.import_module("pypdf")
    except ImportError as error:
        raise ImportError("parse_pdf_document requires the shared pypdf package") from error  # noqa: TRY003

    try:
        reader = pypdf.PdfReader(io.BytesIO(value.content))
        _decrypt_pdf(reader)
        pages = []
        for page_number, page in enumerate(reader.pages, start=1):
            pages.append(
                PDFPageInput(
                    document_id=value.document_id,
                    document_sha256=document_sha256,
                    page_number=page_number,
                    text=_extract_pdf_page_text(page),
                )
            )
    except Exception as error:
        raise ValueError(  # noqa: TRY003
            "PDF content is invalid, encrypted, damaged, or could not be parsed completely"
        ) from error
    return tuple(pages)


class PDFPageSourceAdapter:
    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        return PDFPageSource(
            name=_pdf_page_source_id(value.document_id, value.document_sha256, value.page_number),
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.name,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(
    PDFPageSourceAdapter(),
    version="1",
    projections=(PDFPageTextEvidenceProjection(),),
)


@dataclass(frozen=True, slots=True)
class _QuestionPlan:
    terms: tuple[str, ...]
    phrases: tuple[tuple[str, ...], ...]
    queries: tuple[str, ...]


@dataclass(slots=True)
class _Candidate:
    hit: MemoryHit
    ranks: dict[int, int]
    tokens: frozenset[str]
    token_text: str


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(_TOKEN_PATTERN.findall(value.casefold()))


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _stems(term: str) -> tuple[str, ...]:
    stems = []
    if len(term) > 4 and term.endswith("ies"):
        stems.append(f"{term[:-3]}y")
    if len(term) > 4 and term.endswith("s") and not term.endswith("ss"):
        stems.append(term[:-1])
    if len(term) > 5 and term.endswith("ing"):
        stems.extend((term[:-3], f"{term[:-3]}e"))
    if len(term) > 4 and term.endswith("ed"):
        stems.extend((term[:-2], f"{term[:-2]}e"))
    return _unique(stems)


def _term_forms(term: str) -> frozenset[str]:
    forms = [term]
    for alternative in (term, *_TERM_SYNONYMS.get(term, ())):
        forms.append(alternative)
        forms.extend(_stems(alternative))
    return frozenset(forms)


def _contains_phrase(token_text: str, phrase: tuple[str, ...]) -> bool:
    return f" {' '.join(phrase)} " in token_text


def _contains_sequence(tokens: tuple[str, ...], sequence: tuple[str, ...]) -> bool:
    width = len(sequence)
    return any(tokens[start : start + width] == sequence for start in range(len(tokens) - width + 1))


def _question_tokens_and_acronyms(question: str) -> tuple[list[str], list[tuple[str, ...]]]:
    raw_tokens = list(_tokens(question))
    acronym_phrases = []
    if re.search(r"\bd\s*&\s*a\b", question, flags=re.IGNORECASE):
        acronym_phrases.append(("depreciation", "and", "amortization"))
        raw_tokens.extend(("depreciation", "amortization"))
    if re.search(r"\bp\s*&\s*l\b", question, flags=re.IGNORECASE):
        acronym_phrases.append(("income", "statement"))
        raw_tokens.extend(("income", "statement"))
    return raw_tokens, acronym_phrases


def _question_synonym_phrases(
    raw_tokens: tuple[str, ...], acronym_phrases: list[tuple[str, ...]]
) -> list[tuple[str, ...]]:
    synonym_phrases = list(acronym_phrases)
    for trigger, alternatives in _PHRASE_SYNONYMS.items():
        if _contains_sequence(raw_tokens, trigger):
            synonym_phrases.extend(alternatives)
    return synonym_phrases


def _question_phrases(
    raw_tokens: tuple[str, ...], terms: tuple[str, ...], synonym_phrases: list[tuple[str, ...]]
) -> tuple[tuple[str, ...], ...]:
    phrase_candidates = list(synonym_phrases)
    term_set = set(terms)
    for width in range(2, 6):
        for start in range(len(raw_tokens) - width + 1):
            phrase = raw_tokens[start : start + width]
            if phrase[0] in _STOP_WORDS or phrase[-1] in _STOP_WORDS:
                continue
            if len(term_set.intersection(phrase)) >= 2:
                phrase_candidates.append(phrase)
    return tuple(dict.fromkeys(phrase_candidates))


def _question_plan(question: str) -> _QuestionPlan:
    raw_tokens, acronym_phrases = _question_tokens_and_acronyms(question)
    terms = _unique([term for term in raw_tokens if term not in _STOP_WORDS and (len(term) > 1 or term.isdigit())])
    if not terms:
        raise ValueError("question must contain searchable financial terms or numbers")  # noqa: TRY003

    raw_tuple = tuple(raw_tokens)
    synonym_phrases = _question_synonym_phrases(raw_tuple, acronym_phrases)
    expanded_terms = list(terms)
    for term in terms:
        expanded_terms.extend(_term_forms(term))
    expanded = _unique(expanded_terms)
    phrases = _question_phrases(raw_tuple, terms, synonym_phrases)

    query_candidates = [question, " ".join(terms[:6]), " ".join(expanded[:8])]
    for phrase in synonym_phrases[:4]:
        query_candidates.append(" ".join(phrase))
        query_candidates.append(" ".join(_unique([*terms[:3], *phrase])))
    for phrase in phrases[len(synonym_phrases) :][:4]:
        query_candidates.append(" ".join(phrase))
    queries = tuple(dict.fromkeys(query_candidates))
    return _QuestionPlan(terms=terms, phrases=phrases, queries=queries)


def _candidate_key(hit: MemoryHit) -> tuple[str, str, int, str, str]:
    return (
        hit.memory_ref.family,
        hit.memory_ref.artifact_id,
        hit.memory_ref.revision,
        hit.entry_id,
        hit.entry_version_id,
    )


def _new_candidate(hit: MemoryHit, query_index: int, rank: int) -> _Candidate:
    tokens = _tokens(hit.text)
    return _Candidate(
        hit=hit,
        ranks={query_index: rank},
        tokens=frozenset(tokens),
        token_text=f" {' '.join(tokens)} ",
    )


def _term_matches(term: str, candidate: _Candidate) -> bool:
    if not candidate.tokens.isdisjoint(_term_forms(term)):
        return True
    return any(
        _contains_phrase(candidate.token_text, phrase)
        for trigger, phrases in _PHRASE_SYNONYMS.items()
        if trigger == (term,)
        for phrase in phrases
    )


def _rank_candidates(candidates: dict[tuple[str, str, int, str, str], _Candidate], plan: _QuestionPlan):
    matched_terms = {
        key: frozenset(term for term in plan.terms if _term_matches(term, candidate))
        for key, candidate in candidates.items()
    }
    document_frequency = Counter(term for matches in matched_terms.values() for term in matches)
    population = len(candidates)
    inverse_frequency = {term: math.log((population + 1) / (document_frequency[term] + 1)) + 1 for term in plan.terms}
    term_weights = {
        term: (1.5 if len(term) == 4 and term.isdigit() else 1.2 if term.isdigit() else 1.0) * inverse_frequency[term]
        for term in plan.terms
    }
    total_weight = sum(term_weights.values())

    scores = {}
    for key, candidate in candidates.items():
        matches = matched_terms[key]
        coverage = len(matches) / len(plan.terms)
        rarity = sum(term_weights[term] for term in matches) / total_weight
        matched_phrases = [phrase for phrase in plan.phrases if _contains_phrase(candidate.token_text, phrase)]
        longest_phrase = max((len(phrase) for phrase in matched_phrases), default=1)
        phrase_score = min(1.0, (longest_phrase - 1) / 3)
        fts_score = max(
            (1.0 if query_index == 0 else 0.9) / math.log2(rank + 1) for query_index, rank in candidate.ranks.items()
        )
        scores[key] = 0.30 * coverage + 0.34 * rarity + 0.24 * phrase_score + 0.12 * fts_score

    return sorted(
        candidates.values(),
        key=lambda candidate: (
            -scores[_candidate_key(candidate.hit)],
            min(candidate.ranks.values()),
            _candidate_key(candidate.hit),
        ),
    )


async def _collect_pdf_candidates(
    memory, plan: _QuestionPlan, candidate_limit: int
) -> dict[tuple[str, str, int, str, str], _Candidate]:
    from powercontext.builtin.runtime.models import SearchMemoryRequest

    candidates = {}
    for query_index, query in enumerate(plan.queries):
        page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=candidate_limit))
        for rank, hit in enumerate(page.hits, start=1):
            key = _candidate_key(hit)
            if key not in candidates:
                candidates[key] = _new_candidate(hit, query_index, rank)
            else:
                previous = candidates[key].ranks.get(query_index)
                if previous is None or rank < previous:
                    candidates[key].ranks[query_index] = rank
    return candidates


async def _get_validated_pdf_page_citation(memory, hit: MemoryHit) -> tuple[MemoryCitation, str] | None:
    from powercontext.builtin.runtime.models import GetMemoryEntryRequest

    citation = MemoryCitation(
        memory_ref=hit.memory_ref,
        entry_id=hit.entry_id,
        entry_version_id=hit.entry_version_id,
    )
    record = await memory.get(GetMemoryEntryRequest(citation=citation))
    pdf_source_ids = {
        reference.source_id for reference in record.entry.sources if reference.source_type == PDF_PAGE_SOURCE_NAME
    }
    if len(pdf_source_ids) != 1:
        return None
    return citation, next(iter(pdf_source_ids))


async def search_pdf_pages(memory, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Retrieve and exactly validate distinct captured PDF pages through scoped Memory."""

    if limit < 0:
        raise ValueError("limit must not be negative")  # noqa: TRY003
    if limit == 0:
        return ()
    plan = _question_plan(question)
    candidate_limit = max(limit, min(_MAX_SEARCH_CANDIDATES, max(64, limit * 8)))
    candidates = await _collect_pdf_candidates(memory, plan, candidate_limit)

    citations = []
    seen_source_ids = set()
    for candidate in _rank_candidates(candidates, plan):
        validated = await _get_validated_pdf_page_citation(memory, candidate.hit)
        if validated is None:
            continue
        citation, source_id = validated
        if source_id in seen_source_ids:
            continue
        seen_source_ids.add(source_id)
        citations.append(citation)
        if len(citations) == limit:
            break
    return tuple(citations)


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDF_PAGE_SOURCE_NAME",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "PDFPageSourceAdapter",
    "PDFPageTextEvidenceProjection",
    "parse_pdf_document",
    "search_pdf_pages",
]
