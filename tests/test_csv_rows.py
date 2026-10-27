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

from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    SourceDefinitionRegistry,
    SourceMaterialization,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.csv_rows import (
    CSV_ROW_SOURCE_DEFINITION,
    CSVDocumentInput,
    CSVRowInput,
    CSVRowSource,
    csv_row_source_id,
    parse_csv_document,
)


def test_csv_row_definition_preserves_exact_text_evidence_and_transport_observation() -> None:
    row = parse_csv_document(
        CSVDocumentInput(
            document_id="doc",
            primary_key="id",
            content="id,name,active,empty,note\r\n001,机场,false,, 001 \r\n".encode(),
        )
    )[0]
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))

    async def scenario() -> None:
        source = await registry.resolve(row)
        assert isinstance(source, CSVRowSource)
        assert source.name == source.source_id == "csv:doc:001"
        assert source.materialization is SourceMaterialization.CAPTURED
        assert await registry.read(source) == row

        evidence = TextEvidence.model_validate(registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY))
        assert evidence == TextEvidence(
            source_type="csv-row",
            source_id="csv:doc:001",
            content='{"id":"001","name":"机场","active":"false","empty":"","note":" 001 "}',
            metadata={
                "document_id": "doc",
                "primary_key": "id",
                "primary_key_value": "001",
                "columns": ["id", "name", "active", "empty", "note"],
                "row": {"id": "001", "name": "机场", "active": "false", "empty": "", "note": " 001 "},
            },
        )

        manifest = manifest_for_definition(CSV_ROW_SOURCE_DEFINITION)
        observation = project_source_for_transport(registry, source)
        assert manifest.name == "csv-row"
        assert manifest.version == "1"
        assert observation.source_type == "csv-row"
        assert observation.materialization is SourceMaterialization.CAPTURED
        assert TextEvidence.model_validate(observation.projection(TEXT_EVIDENCE_PROJECTION_KEY)) == evidence

    asyncio.run(scenario())
    assert CSV_ROW_SOURCE_DEFINITION.input_class is CSVRowInput
    assert CSV_ROW_SOURCE_DEFINITION.source_class is CSVRowSource


def test_only_empty_physical_records_after_the_header_are_ignored() -> None:
    rows = parse_csv_document(
        CSVDocumentInput(
            document_id="doc",
            primary_key="id",
            content=b"id,name\r\n\r\n1,one\r\n\r\n2,two\r\n\r\n",
        )
    )

    assert [row.values for row in rows] == [("1", "one"), ("2", "two")]


@pytest.mark.parametrize("content", [b'id\n""\n', b"id\n \t\n", b'id,name\n""\n'])
def test_quoted_empty_and_whitespace_records_are_not_empty_physical_records(content: bytes) -> None:
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="id", content=content))


@pytest.mark.parametrize("content", [b"\nid\n1\n", b"\xef\xbb\xbf", b"\xef\xbb\xbf\n"])
def test_empty_or_missing_headers_fail_before_any_data(content: bytes) -> None:
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="id", content=content))


def test_strict_quoting_failure_after_a_valid_record_is_atomic() -> None:
    document = CSVDocumentInput(
        document_id="doc",
        primary_key="id",
        content=b'id,name\n1,valid\n2,"closed"junk\n',
    )

    with pytest.raises(ValueError):
        parse_csv_document(document)


@pytest.mark.parametrize("document_id", ["", " ", "\tdoc", "doc\n"])
def test_document_identity_rejects_empty_and_surrounding_whitespace(document_id: str) -> None:
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id=document_id, primary_key="id", content=b"id\n1\n"))


def test_empty_primary_key_and_blank_primary_key_values_fail() -> None:
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="", content=b"id\n1\n"))
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="id", content=b"id\n \t\n"))


def test_identity_uses_utf8_percent_encoding_and_bounds_the_encoded_length() -> None:
    assert (
        csv_row_source_id("文档:α", "值 /%")  # noqa: RUF001
        == "csv:%E6%96%87%E6%A1%A3%3A%CE%B1:%E5%80%BC%20%2F%25"
    )

    longest = csv_row_source_id("d", "/" * 83 + "x")
    assert len(longest) == 256
    with pytest.raises(ValueError):
        csv_row_source_id("d", "/" * 83 + "xx")
