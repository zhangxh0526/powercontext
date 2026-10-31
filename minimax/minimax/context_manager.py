from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pymysql


@dataclass(frozen=True)
class Document:
    doc_id: str
    kind: str
    revision: int
    title: str
    content: str
    source_path: str
    keywords: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "Document":
        required = ("doc_id", "kind", "revision", "title", "content", "source_path")
        missing = [name for name in required if name not in value]
        if missing:
            raise ValueError(f"missing document fields: {', '.join(missing)}")
        if value["kind"] not in {"spec", "design", "code", "test"}:
            raise ValueError(f"unsupported document kind: {value['kind']}")
        if not isinstance(value["revision"], int) or value["revision"] <= 0:
            raise ValueError("revision must be a positive integer")
        for name in ("doc_id", "title", "content", "source_path"):
            if not isinstance(value[name], str) or not value[name].strip():
                raise ValueError(f"{name} must be a non-empty string")
        keywords = value.get("keywords", [])
        if not isinstance(keywords, list) or not all(isinstance(item, str) for item in keywords):
            raise ValueError("keywords must be a list of strings")
        return cls(
            doc_id=value["doc_id"],
            kind=value["kind"],
            revision=value["revision"],
            title=value["title"],
            content=value["content"],
            source_path=value["source_path"],
            keywords=tuple(keywords),
        )

    @classmethod
    def from_json(cls, path: str | Path) -> "Document":
        with Path(path).open(encoding="utf-8") as stream:
            return cls.from_mapping(json.load(stream))

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "kind": self.kind,
            "revision": self.revision,
            "title": self.title,
            "content": self.content,
            "source_path": self.source_path,
            "keywords": list(self.keywords),
        }


class SeekdbContextStore:
    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 12881,
        user: str = "root",
        password: str = "",
        database: str = "minimax_sdd",
        connect_timeout: int = 5,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_]+", database):
            raise ValueError("database must contain only letters, digits and underscores")
        self.database = database
        self.connection = pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=connect_timeout,
        )
        with self.connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{database}`")
        self.connection.select_db(database)
        self._init_schema()

    def _init_schema(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS sdd_documents (
                  doc_id VARCHAR(128) PRIMARY KEY,
                  kind VARCHAR(32) NOT NULL,
                  revision BIGINT NOT NULL,
                  title VARCHAR(255) NOT NULL,
                  content MEDIUMTEXT NOT NULL,
                  source_path VARCHAR(512) NOT NULL,
                  keywords TEXT NOT NULL,
                  active TINYINT NOT NULL DEFAULT 1
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS sdd_task_records (
                  id BIGINT AUTO_INCREMENT PRIMARY KEY,
                  task_text TEXT NOT NULL,
                  context_json MEDIUMTEXT NOT NULL,
                  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def reset(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS sdd_task_records")
            cursor.execute("DROP TABLE IF EXISTS sdd_documents")
        self._init_schema()

    def import_document(self, document: Document) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                REPLACE INTO sdd_documents
                  (doc_id, kind, revision, title, content, source_path, keywords, active)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 1)
                """,
                (
                    document.doc_id,
                    document.kind,
                    document.revision,
                    document.title,
                    document.content,
                    document.source_path,
                    json.dumps(document.keywords, ensure_ascii=False),
                ),
            )

    def import_json(self, path: str | Path) -> Document:
        document = Document.from_json(path)
        self.import_document(document)
        return document

    @staticmethod
    def _like_term(term: str) -> str:
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    def prepare(self, task: str, terms: Iterable[str] = ()) -> dict[str, Any]:
        task = task.strip()
        if not task:
            raise ValueError("task must not be empty")
        search_terms = [term.strip() for term in terms if term.strip()] or [task]
        clauses = []
        params: list[str] = []
        for term in search_terms:
            clauses.append("(keywords LIKE %s ESCAPE '\\\\' OR title LIKE %s ESCAPE '\\\\' OR content LIKE %s ESCAPE '\\\\')")
            params.extend([self._like_term(term)] * 3)
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT doc_id, kind, revision, title, content, source_path, keywords
                FROM sdd_documents
                WHERE active = 1 AND ({' OR '.join(clauses)})
                ORDER BY doc_id
                """,
                params,
            )
            rows = cursor.fetchall()
        documents = [
            {
                "doc_id": row[0],
                "kind": row[1],
                "revision": row[2],
                "title": row[3],
                "content": row[4],
                "source_path": row[5],
                "keywords": json.loads(row[6]),
            }
            for row in rows
        ]
        context: dict[str, Any] = {"task": task, "documents": documents, "record_id": None}
        with self.connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO sdd_task_records (task_text, context_json) VALUES (%s, %s)",
                (task, json.dumps(context, ensure_ascii=False)),
            )
            record_id = cursor.lastrowid
        context["record_id"] = record_id
        with self.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE sdd_task_records SET context_json = %s WHERE id = %s",
                (json.dumps(context, ensure_ascii=False), record_id),
            )
        return context

    def last_task(self) -> dict[str, Any] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, task_text, context_json FROM sdd_task_records ORDER BY id DESC LIMIT 1"
            )
            row = cursor.fetchone()
        if row is None:
            return None
        context = json.loads(row[2])
        context["record_id"] = row[0]
        context["task"] = row[1]
        return context

    def close(self) -> None:
        self.connection.close()
