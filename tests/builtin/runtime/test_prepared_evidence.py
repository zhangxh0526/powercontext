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

import json

import pytest
from pydantic import ValidationError

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryHit
from powercontext.builtin.runtime import PrepareContextRequest
from powercontext.builtin.runtime.prepared_context import (
    PreparedContextBuild,
    PreparedContextBuilder,
    PreparedMemoryCandidates,
)

MEMORY_REF = ArtifactRef(family="memory", artifact_id="memory", revision=3)


def _hit(entry_id: str, text: str, *, memory_ref: ArtifactRef = MEMORY_REF) -> MemoryHit:
    return MemoryHit(
        memory_ref=memory_ref,
        entry_id=entry_id,
        entry_version_id=f"{entry_id}-v1",
        text=text,
        score=1.0,
        matched_by=("fts",),
    )


def _candidates(scope_id: str, *hits: MemoryHit, memory_ref: ArtifactRef = MEMORY_REF) -> PreparedMemoryCandidates:
    return PreparedMemoryCandidates(scope_id=scope_id, memory_ref=memory_ref, hits=hits)


def _assert_aligned(build: PreparedContextBuild, texts: dict[tuple[str, str], str], max_bytes: int) -> None:
    assert build.context.status == "ready"
    content = build.context.content
    assert content is not None
    assert build.context.content_bytes == len(content.encode("utf-8")) <= max_bytes
    assert "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1" in content
    assert "END_POWERCONTEXT_PREPARED_CONTEXT_V1" in content
    envelope = json.loads(content.splitlines()[-2])
    assert envelope["trust"] == "untrusted_history"
    items = envelope["items"]
    assert len(items) == len(build.fragments) == len(build.origins)
    for item, fragment, origin in zip(items, build.fragments, build.origins, strict=True):
        address = fragment.address
        assert origin == address
        assert type(fragment.start) is int and type(fragment.end) is int
        assert 0 <= fragment.start < fragment.end <= len(texts[address.memory.scope_id, address.entry_id])
        assert fragment.text == texts[address.memory.scope_id, address.entry_id][fragment.start : fragment.end]
        assert set(item) == {"citation", "content", "start", "end"}
        assert item["content"] == fragment.text
        assert item["start"] == fragment.start and item["end"] == fragment.end
        assert item["citation"] == {
            "scope_id": address.memory.scope_id,
            "memory_ref": address.memory.artifact.model_dump(mode="json"),
            "entry_id": address.entry_id,
            "entry_version_id": address.entry_version_id,
        }


def test_multiple_late_fragments_share_one_exact_entry_and_probe_agrees() -> None:
    first = "The cobalt sensor calibration is stable at 37 degrees."
    second = "Independent validation confirms cobalt sensor reproducibility in every trial."
    filler = "Administrative notes describe unrelated operational procedures. " * 6
    text = f"Opening notes. {first} {filler}{second} Closing notes."
    candidates = (_candidates("scope", _hit("entry", text)),)
    request = PrepareContextRequest(
        query="cobalt sensor calibration validation",
        max_bytes=1200,
        evidence_selection=True,
    )
    builder = PreparedContextBuilder()

    build = builder.build_scopes_result(request=request, current_scope_id="scope", memory_candidates=candidates)
    view = builder.probe_budget(request=request, current_scope_id="scope", memory_candidates=candidates)

    assert len(build.fragments) >= 2
    assert all(fragment.address == build.fragments[0].address for fragment in build.fragments)
    assert any("37 degrees" in fragment.text for fragment in build.fragments)
    assert any("Independent validation" in fragment.text for fragment in build.fragments)
    _assert_aligned(build, {("scope", "entry"): text}, request.max_bytes)
    assert view.delivered_items == len(build.fragments)
    assert view.truncated_items == build.omissions.truncated_items == 0
    assert view.dropped_items == build.omissions.dropped_items
    assert view.unused_bytes == request.max_bytes - build.context.content_bytes


def test_same_text_in_different_scopes_keeps_distinct_citations_and_origins() -> None:
    text = "Shared cobalt sensor evidence is calibrated at 37 degrees."
    candidates = (
        _candidates("scope-a", _hit("shared-entry", text)),
        _candidates("scope-b", _hit("shared-entry", text)),
    )

    build = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="cobalt sensor", max_bytes=1200, evidence_selection=True),
        current_scope_id="scope-a",
        memory_candidates=candidates,
    )

    assert len(build.fragments) == 2
    assert {fragment.address.memory.scope_id for fragment in build.fragments} == {"scope-a", "scope-b"}
    assert all(fragment.text == text for fragment in build.fragments)
    _assert_aligned(
        build,
        {("scope-a", "shared-entry"): text, ("scope-b", "shared-entry"): text},
        max_bytes=1200,
    )


def test_unicode_emoji_and_combining_characters_preserve_literal_coordinates() -> None:
    text = "Intro notes. 🙂 The cafe\u0301 station records 海洋观测 at 37 degrees. Closing notes."

    build = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="café 海洋观测", max_bytes=1000, evidence_selection=True),
        current_scope_id="scope",
        memory_candidates=(_candidates("scope", _hit("unicode", text)),),
    )

    selected = next(fragment for fragment in build.fragments if "station" in fragment.text)
    assert "🙂" in selected.text
    assert "cafe\u0301" in selected.text and "café" not in selected.text
    _assert_aligned(build, {("scope", "unicode"): text}, max_bytes=1000)


def test_final_renderer_owns_budget_boundary_and_legal_empty_result() -> None:
    text = "Cobalt sensor evidence."
    reference = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="cobalt", max_bytes=1000, evidence_selection=True),
        current_scope_id="s",
        memory_candidates=(
            _candidates(
                "s",
                _hit("e", text, memory_ref=ArtifactRef(family="memory", artifact_id="m", revision=1)),
                memory_ref=ArtifactRef(family="memory", artifact_id="m", revision=1),
            ),
        ),
    )
    assert reference.context.status == "ready"

    boundary = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(
            query="cobalt",
            max_bytes=reference.context.content_bytes,
            evidence_selection=True,
        ),
        current_scope_id="s",
        memory_candidates=(
            _candidates(
                "s",
                _hit("e", text, memory_ref=ArtifactRef(family="memory", artifact_id="m", revision=1)),
                memory_ref=ArtifactRef(family="memory", artifact_id="m", revision=1),
            ),
        ),
    )
    assert boundary.context == reference.context
    assert boundary.fragments == reference.fragments

    long_ref = ArtifactRef(family="memory", artifact_id="m" * 128, revision=1)
    empty = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="cobalt", max_bytes=512, evidence_selection=True),
        current_scope_id="s" * 128,
        memory_candidates=(_candidates("s" * 128, _hit("e" * 128, text, memory_ref=long_ref), memory_ref=long_ref),),
    )
    assert empty.context.status == "empty"
    assert empty.context.content is None and empty.context.content_bytes == 0
    assert empty.fragments == () and empty.origins == ()


def test_default_false_is_byte_compatible_and_fragments_default_empty() -> None:
    candidates = (_candidates("scope", _hit("entry", "A cobalt sensor records temperature.")),)
    implicit_request = PrepareContextRequest(query="cobalt")
    explicit_request = PrepareContextRequest(query="cobalt", evidence_selection=False)
    builder = PreparedContextBuilder()

    implicit = builder.build_scopes_result(
        request=implicit_request, current_scope_id="scope", memory_candidates=candidates
    )
    explicit = builder.build_scopes_result(
        request=explicit_request, current_scope_id="scope", memory_candidates=candidates
    )

    assert implicit_request.evidence_selection is False
    assert implicit == explicit
    assert implicit.fragments == ()
    assert implicit.context.content is not None
    item = json.loads(implicit.context.content.splitlines()[-2])["items"][0]
    assert item["content"] == "A cobalt sensor records temperature."
    assert "start" not in item and "end" not in item
    assert PreparedContextBuild(context=builder.empty(), origins=()).fragments == ()


@pytest.mark.parametrize("value", ["true", 1, None])
def test_evidence_selection_requires_a_strict_boolean(value: object) -> None:
    with pytest.raises(ValidationError):
        PrepareContextRequest.model_validate({"query": "cobalt", "evidence_selection": value})
