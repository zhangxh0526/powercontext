"""Minimal PowerContext client built on the Python standard library.

Only the endpoints required by the experiment continuation flow are used:
``/health/ready``, ``/v1/scopes``, ``/v1/memory/remember`` and
``/v1/context/prepare`` (request/response shapes follow reference/powercontext.yaml).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import Config

MAX_UTF8_BYTES = 8192


class PowerContextError(Exception):
    """Base class for every PowerContext failure surfaced to the caller."""

    kind = "error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": self.message}


class ServiceUnavailable(PowerContextError):
    kind = "service_unavailable"


class ApiError(PowerContextError):
    kind = "api_error"

    def __init__(self, message: str, status: int, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": self.message, "status": self.status, "code": self.code}


@dataclass(frozen=True)
class MemoryEntry:
    entry_id: str
    entry_version_id: str
    kind: str
    text: str
    score: float | None = None


@dataclass(frozen=True)
class PreparedContext:
    status: str
    content: str
    content_bytes: int
    entries: tuple[MemoryEntry, ...]
    truncated: bool


ENTRY_HEADING = re.compile(r"^#{2,4}\s*Memory\b.*$", re.MULTILINE)
ENTRY_ID = re.compile(r'Entry:\s*id="(?P<entry_id>[^"]*)"')
ENTRY_VERSION = re.compile(r'version="(?P<entry_version_id>[^"]*)"')
KIND_LINE = re.compile(r'Kind:\s*"(?P<kind>[^"]*)"')
BLOCK_TEXT = re.compile(r"^(?: {0,8})>\s?(?P<line>.*)$", re.MULTILINE)
BODY_START = "BEGIN_POWERCONTEXT_PREPARED_TEXT_V1"
BODY_END = "END_POWERCONTEXT_PREPARED_TEXT_V1"


def parse_prepared_content(content: str) -> tuple[tuple[MemoryEntry, ...], bool]:
    """Extract Memory entries from the assembled Markdown context block."""
    body = content
    if BODY_START in body:
        body = body.split(BODY_START, 1)[1]
    if BODY_END in body:
        body = body.split(BODY_END, 1)[0]

    headings = list(ENTRY_HEADING.finditer(body))
    if not headings:
        return (), False
    truncated = "Truncated: yes" in body
    entries: list[MemoryEntry] = []
    for index, heading in enumerate(headings):
        start = heading.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        block = body[start:end]
        entry_id = ENTRY_ID.search(block)
        entry_version = ENTRY_VERSION.search(block)
        if not entry_id or not entry_version:
            continue
        kind = KIND_LINE.search(block)
        lines = [match.group("line").strip() for match in BLOCK_TEXT.finditer(block)]
        text = " ".join(line for line in lines if line).strip()
        entries.append(
            MemoryEntry(
                entry_id=entry_id.group("entry_id"),
                entry_version_id=entry_version.group("entry_version_id"),
                kind=kind.group("kind") if kind else "",
                text=text,
            )
        )
    return tuple(entries), truncated


class PowerContextClient:
    def __init__(self, config: Config, timeout: float = 15.0) -> None:
        self.config = config
        self.timeout = timeout

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if self.config.powercontext_token:
            headers["Authorization"] = f"Bearer {self.config.powercontext_token}"
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.config.base_url}{path}", data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            code = None
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                detail = parsed.get("error") or parsed.get("detail")
                if isinstance(detail, dict):
                    code = detail.get("code")
                    message = detail.get("message") or json.dumps(detail, ensure_ascii=False)
                elif detail:
                    code = detail if isinstance(detail, str) else None
                    message = str(detail)
                else:
                    message = json.dumps(parsed, ensure_ascii=False)
            else:
                message = raw.strip() or f"HTTP {error.code}"
            raise ApiError(f"PowerContext {path} 返回 HTTP {error.code}: {message}", error.code, code) from error
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", error)
            raise ServiceUnavailable(
                f"无法连接 PowerContext {self.config.base_url}（{type(reason).__name__}: {reason}）"
            ) from error
        except TimeoutError as error:
            raise ServiceUnavailable(f"连接 PowerContext {self.config.base_url} 超时") from error
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise ApiError(f"PowerContext {path} 返回了无法解析的 JSON", 502, "invalid_json") from error

    def readiness(self) -> dict[str, Any]:
        return self._request("/health/ready")

    def ensure_scope_readable(self, scope_id: str) -> dict[str, Any]:
        return self._request(f"/v1/scopes/{urllib.parse.quote(scope_id, safe='')}")

    def create_scope(self, title: str, summary: str, idempotency_key: str) -> dict[str, Any]:
        return self._request(
            "/v1/scopes",
            {"title": title, "summary": summary, "idempotency_key": idempotency_key},
        )

    def remember_decision(self, scope_id: str, text: str, reason: str) -> dict[str, Any]:
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_UTF8_BYTES:
            raise ApiError(
                f"记忆文本超出 PowerContext 上限 {MAX_UTF8_BYTES} UTF-8 字节（当前 {len(encoded)} 字节）",
                422,
                "text_too_long",
            )
        return self._request(
            "/v1/memory/remember",
            {"scope_id": scope_id, "kind": "decision", "text": text, "reason": reason},
        )

    def prepare(self, scope_id: str, query: str, max_bytes: int = 32768) -> PreparedContext:
        result = self._request(
            "/v1/context/prepare",
            {
                "scope_id": scope_id,
                "query": query,
                "max_bytes": max_bytes,
                "assembly": {"sections": [{"family": "memory", "limit": 8}]},
            },
        )
        content = (result or {}).get("content") or ""
        status = (result or {}).get("status") or "empty"
        entries, truncated = parse_prepared_content(content)
        return PreparedContext(
            status=status,
            content=content,
            content_bytes=int((result or {}).get("content_bytes") or 0),
            entries=entries,
            truncated=truncated,
        )

    def entries(self, scope_id: str) -> tuple[MemoryEntry, ...]:
        """Full active Memory inventory of one Scope (no relevance truncation)."""
        result = self._request("/v1/memory/entries/list", {"scope_id": scope_id}) or {}
        entries: list[MemoryEntry] = []
        for item in result.get("entries") or []:
            citation = item.get("citation") or {}
            entries.append(
                MemoryEntry(
                    entry_id=citation.get("entry_id", ""),
                    entry_version_id=citation.get("entry_version_id", ""),
                    kind=item.get("kind", ""),
                    text=item.get("text", ""),
                )
            )
        return tuple(entries)

    def search(self, scope_id: str, query: str, limit: int = 6) -> dict[str, Any]:
        return self._request(
            "/v1/memory/search",
            {"scope_id": scope_id, "query": query, "limit": limit, "mode": "fts"},
        )


import urllib.parse  # noqa: E402  (kept next to the helper that needs it)
