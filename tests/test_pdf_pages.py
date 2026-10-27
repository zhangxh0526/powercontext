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
import hashlib
import io
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.search import analyze_fts_query, analyze_text
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    SourceDefinitionRegistry,
    SourceMaterialization,
    SourceRef,
    TextEvidence,
)
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    PDFPageInput,
    PDFPageSource,
    parse_pdf_document,
    search_pdf_pages,
)

pypdf = pytest.importorskip("pypdf")
_OWNER_FIXTURE = "owner"
_USER_FIXTURE = "secret"


def _blank_pdf(page_count: int = 1, *, user_password: str | None = None) -> bytes:
    writer = pypdf.PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=100, height=200)
    if user_password is not None:
        writer.encrypt(user_password=user_password, owner_password=_OWNER_FIXTURE)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_parse_hashes_complete_original_bytes_and_preserves_blank_physical_pages() -> None:
    content = _blank_pdf(3)

    pages = parse_pdf_document(PDFDocumentInput(document_id="report", content=content))

    assert [(page.page_number, page.text) for page in pages] == [(1, ""), (2, ""), (3, "")]
    assert {page.document_sha256 for page in pages} == {hashlib.sha256(content).hexdigest()}


@pytest.mark.parametrize("document_id", ["", " ", "\t\n"])
def test_document_and_page_inputs_reject_blank_document_ids(document_id: str) -> None:
    with pytest.raises(ValueError):
        PDFDocumentInput(document_id=document_id, content=_blank_pdf())
    with pytest.raises(ValueError):
        PDFPageInput(document_id=document_id, document_sha256="a" * 64, page_number=1, text="")


def test_nonempty_password_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="secret", content=_blank_pdf(user_password=_USER_FIXTURE)))


def test_failure_on_any_page_is_atomic(monkeypatch: pytest.MonkeyPatch) -> None:
    class Page:
        def __init__(self, text: str) -> None:
            self.text = text

        def extract_text(self) -> str:
            if self.text == "broken":
                raise RuntimeError(self.text)
            return self.text

    class Reader:
        is_encrypted = False

        def __init__(self) -> None:
            self.pages = [Page("first"), Page("broken"), Page("last")]

    monkeypatch.setattr(pypdf, "PdfReader", lambda _stream: Reader())

    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="broken", content=b"complete original bytes"))


def test_identity_is_stable_unique_bounded_and_revision_aware() -> None:
    original = PDFPageInput(document_id="document:a", document_sha256="a" * 64, page_number=7, text="old")
    other_page = original.model_copy(update={"page_number": 8})
    other_document = original.model_copy(update={"document_id": "document:b"})
    revision = original.model_copy(update={"document_sha256": "b" * 64, "text": "new"})
    long_document = original.model_copy(update={"document_id": "文档:" * 200})
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))

    async def scenario() -> None:
        sources = [
            await registry.resolve(page) for page in (original, other_page, other_document, revision, long_document)
        ]
        assert len({source.name for source in sources}) == len(sources)
        assert all(isinstance(source, PDFPageSource) for source in sources)
        assert all(source.materialization is SourceMaterialization.CAPTURED for source in sources)
        assert all(len(source.name) <= 256 for source in sources)
        assert (await registry.resolve(original)).name == sources[0].name
        assert sources[0].name != sources[3].name
        assert await registry.read(sources[0]) == original
        assert await registry.read(sources[3]) == revision

        evidence = TextEvidence.model_validate(registry.project(sources[0], TEXT_EVIDENCE_PROJECTION_KEY))
        assert evidence == TextEvidence(
            source_type="pdf-page",
            source_id=sources[0].name,
            content="document:a\nold",
            metadata={"document_id": "document:a", "document_sha256": "a" * 64, "page_number": 7},
        )
        assert set(evidence.metadata) == {"document_id", "document_sha256", "page_number"}

    asyncio.run(scenario())


@dataclass(frozen=True)
class _FakeEntry:
    citation: MemoryCitation
    text: str
    sources: tuple[SourceRef, ...]


class _FakeScopedMemory:
    def __init__(self, entries: tuple[_FakeEntry, ...]) -> None:
        self.entries = entries
        self.search_requests = []
        self.get_requests = []

    async def search(self, request) -> SimpleNamespace:
        self.search_requests.append(request)
        query_terms = set(analyze_fts_query(request.query).split())
        hits = []
        for entry in self.entries:
            if query_terms.isdisjoint(analyze_text(entry.text).split()):
                continue
            hits.append(
                MemoryHit(
                    memory_ref=entry.citation.memory_ref,
                    entry_id=entry.citation.entry_id,
                    entry_version_id=entry.citation.entry_version_id,
                    text=entry.text,
                    score=1.0,
                    matched_by=("fts",),
                )
            )
        return SimpleNamespace(hits=tuple(hits[: request.limit]))

    async def get(self, request) -> SimpleNamespace:
        self.get_requests.append(request)
        entry = next(item for item in self.entries if item.citation == request.citation)
        return SimpleNamespace(entry=SimpleNamespace(sources=entry.sources))


def _fake_entry(index: int, text: str, source_id: str, *, source_type: str = "pdf-page") -> _FakeEntry:
    memory_ref = ArtifactRef(family="memory", artifact_id="report-memory", revision=1)
    return _FakeEntry(
        citation=MemoryCitation(
            memory_ref=memory_ref,
            entry_id=f"entry-{index}",
            entry_version_id=f"version-{index}",
        ),
        text=text,
        sources=(SourceRef(source_type=source_type, source_id=source_id),),
    )


def _retrieval_entries() -> tuple[_FakeEntry, ...]:
    target_text = "ACME\n2022 restructuring charges of 12 and depreciation and amortization of 30"
    return (
        _fake_entry(0, "ACME\n2022 annual report revenue and operating income", "generic-page"),
        _fake_entry(1, target_text, "target-page"),
        _fake_entry(2, target_text, "target-page"),
        _fake_entry(3, "ACME\nRestructuring costs were 12 in 2022", "second-page"),
        _fake_entry(4, target_text, "not-a-pdf-page", source_type="csv-row"),
    )


def test_search_reranks_offline_and_returns_exact_distinct_pdf_citations() -> None:
    memory = _FakeScopedMemory(_retrieval_entries())
    entries = memory.entries
    question = "What were ACME restructuring costs and D&A in FY2022?"

    citations = asyncio.run(search_pdf_pages(memory, question, limit=2))

    assert citations == (entries[1].citation, entries[3].citation)
    assert memory.search_requests[0].query == question
    assert all(request.mode == "fts" for request in memory.search_requests)
    assert any(
        request.query != question
        and all(term in request.query for term in ("2022", "restructuring", "depreciation", "amortization"))
        for request in memory.search_requests
    )
    assert any("charges" in request.query for request in memory.search_requests[1:])
    retrieved = [request.citation for request in memory.get_requests]
    assert all(citation in retrieved for citation in citations)
    assert entries[2].citation in retrieved
    assert all(request.citation.memory_ref == entries[0].citation.memory_ref for request in memory.get_requests)


def test_search_honors_limit_without_calling_memory_for_zero() -> None:
    memory = _FakeScopedMemory(_retrieval_entries())
    question = "What were ACME restructuring costs and D&A in FY2022?"

    assert asyncio.run(search_pdf_pages(memory, question, limit=1)) == (memory.entries[1].citation,)
    request_count = len(memory.search_requests)
    assert asyncio.run(search_pdf_pages(memory, question, limit=0)) == ()
    assert len(memory.search_requests) == request_count
    with pytest.raises(ValueError):
        asyncio.run(search_pdf_pages(memory, question, limit=-1))
