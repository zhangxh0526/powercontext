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

"""Captured row-level Sources for strictly validated offline CSV documents."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from urllib.parse import quote

from pydantic import BaseModel

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization

CSV_ROW_SOURCE_NAME = "csv-row"


class CSVDocumentInput(BaseModel):
    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


class CSVRowSource(Source):
    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


def _validate_document_id(document_id: str) -> None:
    if not document_id or document_id != document_id.strip():
        raise ValueError("document_id must be non-empty and free of surrounding whitespace")  # noqa: TRY003


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    _validate_document_id(document_id)
    if not primary_key_value.strip():
        raise ValueError("primary_key_value must not be empty or blank")  # noqa: TRY003
    source_id = f"csv:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"CSV row source_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return source_id


def _read_columns(records: Iterator[list[str]], primary_key: str) -> tuple[str, ...]:
    header = next(records, None)
    if not header:
        raise ValueError("CSV document must contain a non-empty header")  # noqa: TRY003
    columns = tuple(header)
    if any(not column.strip() for column in columns):
        raise ValueError("CSV header fields must not be empty or blank")  # noqa: TRY003
    if len(set(columns)) != len(columns):
        raise ValueError("CSV header fields must be unique")  # noqa: TRY003
    if primary_key not in columns:
        raise ValueError("primary_key must exactly match a CSV header field")  # noqa: TRY003
    return columns


def _row_input(
    value: CSVDocumentInput,
    columns: tuple[str, ...],
    primary_key_index: int,
    record: list[str],
) -> CSVRowInput | None:
    if not record:
        return None
    if len(record) != len(columns):
        raise ValueError("CSV data records must match the header width")  # noqa: TRY003
    primary_key_value = record[primary_key_index]
    if not primary_key_value.strip():
        raise ValueError("CSV primary key values must not be empty or blank")  # noqa: TRY003
    csv_row_source_id(value.document_id, primary_key_value)
    return CSVRowInput(
        document_id=value.document_id,
        primary_key=value.primary_key,
        primary_key_value=primary_key_value,
        columns=columns,
        values=tuple(record),
    )


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    _validate_document_id(value.document_id)
    if not value.primary_key:
        raise ValueError("primary_key must not be empty")  # noqa: TRY003
    try:
        text = value.content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("CSV content must be valid UTF-8") from error  # noqa: TRY003

    records = csv.reader(io.StringIO(text, newline=""), strict=True)
    rows: list[CSVRowInput] = []
    primary_key_values: set[str] = set()
    try:
        columns = _read_columns(records, value.primary_key)
        primary_key_index = columns.index(value.primary_key)
        for record in records:
            row = _row_input(value, columns, primary_key_index, record)
            if row is None:
                continue
            if row.primary_key_value in primary_key_values:
                raise ValueError("CSV primary key values must be unique within a document")  # noqa: TRY003
            primary_key_values.add(row.primary_key_value)
            rows.append(row)
    except csv.Error as error:
        raise ValueError("CSV content is malformed") from error  # noqa: TRY003
    return tuple(rows)


class CSVRowSourceAdapter:
    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        source_id = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class CSVRowTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


CSV_ROW_SOURCE_ADAPTER = CSVRowSourceAdapter()
CSV_ROW_SOURCE_DEFINITION = AdapterSourceDefinition(
    CSV_ROW_SOURCE_ADAPTER,
    projections=(CSVRowTextEvidenceProjection(),),
)

__all__ = [
    "CSV_ROW_SOURCE_DEFINITION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "csv_row_source_id",
    "parse_csv_document",
]
