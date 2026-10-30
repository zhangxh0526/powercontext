"""Experiment <-> PowerContext Scope mapping and progress text codec.

The store keeps Scope mappings and saved form metadata. Recall always reads
PowerContext Memory; these local fields never substitute for a Memory read.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

PROGRESS_PREFIX = "实验进展："
PAYLOAD_VERSION = 1
DEFAULT_EXPERIMENT_ID = "default"
ID_SAFE = re.compile(r"[^0-9A-Za-z]+")


@dataclass
class Experiment:
    id: str
    title: str
    scope_id: str
    created_at: str
    scope_origin: str = "created"
    progress: str = ""
    next_step: str = ""
    saved_at: str = ""
    ordinal: int = 0
    entry_id: str = ""
    entry_version_id: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def slugify(title: str) -> str:
    slug = ID_SAFE.sub("-", title.strip()).strip("-").lower()
    return slug[:24] or "experiment"


def new_experiment_id(title: str) -> str:
    return f"{slugify(title)}-{secrets.token_hex(3)}"


def build_progress_text(title: str, progress: str, next_step: str, saved_at: str, ordinal: int) -> str:
    """Compose the retrievable Memory text.

    ``实验进展`` stays at the very front because an FTS-only PowerContext can only match
    that anchor against a free-form Chinese question. Everything after it is one JSON
    document, so user text containing ``；`` or ``进展：`` cannot be mistaken for a field
    boundary the way a delimiter-joined line could.
    """
    payload = {
        "v": PAYLOAD_VERSION,
        "ordinal": ordinal,
        "saved_at": saved_at,
        "title": title,
        "progress": progress,
        "next_step": next_step,
    }
    return PROGRESS_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


LEGACY_PATTERNS = (
    re.compile(
        rf"^{re.escape(PROGRESS_PREFIX)}(?P<title>.*?)；第 (?P<ordinal>\d+) 次保存；保存于 (?P<saved_at>[^；]+)；"
        r"进展：(?P<progress>.*)；下一步：(?P<next_step>.*)$"
    ),
    re.compile(
        rf"^{re.escape(PROGRESS_PREFIX)}(?P<title>.*?)；保存于 (?P<saved_at>[^；]+)；"
        r"进展：(?P<progress>.*)；下一步：(?P<next_step>.*)$"
    ),
)
ORDINAL_HINT = re.compile(r"第\s*(?P<ordinal>\d+)\s*次保存")
SAVED_AT_HINT = re.compile(r"保存于\s*(?P<saved_at>\S+?)[；;]?\s")


def _decoded(payload: dict[str, Any], codec: str) -> dict[str, Any]:
    return {
        "title": str(payload.get("title", "")),
        "ordinal": str(payload.get("ordinal", "")),
        "saved_at": str(payload.get("saved_at", "")),
        "progress": str(payload.get("progress", "")),
        "next_step": str(payload.get("next_step", "")),
        "codec": codec,
        "decodable": True,
    }


def parse_progress_text(text: str) -> dict[str, Any] | None:
    """Decode one progress Memory entry.

    Returns ``None`` only for text this application never wrote. Anything that carries
    the ``实验进展`` anchor but cannot be decoded comes back with ``decodable: False`` so
    the caller can still show it instead of reporting "no memory".
    """
    if not isinstance(text, str) or not text.strip():
        return None
    body = text.strip()
    if not body.startswith(PROGRESS_PREFIX):
        return None
    remainder = body[len(PROGRESS_PREFIX) :]
    if remainder.startswith("{"):
        try:
            payload = json.loads(remainder)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and {"progress", "next_step"} <= payload.keys():
            return _decoded(payload, f"json-v{payload.get('v', '?')}")
    for pattern in LEGACY_PATTERNS:
        match = pattern.search(body)
        if match:
            return _decoded(match.groupdict(), "legacy-delimiter")
    ordinal = ORDINAL_HINT.search(body)
    saved_at = SAVED_AT_HINT.search(body)
    return {
        "title": "",
        "ordinal": ordinal.group("ordinal") if ordinal else "",
        "saved_at": saved_at.group("saved_at").rstrip("；;") if saved_at else "",
        "progress": remainder,
        "next_step": "",
        "codec": "undecodable",
        "decodable": False,
    }


def progress_sort_key(entry: dict[str, Any]) -> tuple[int, str, str]:
    """Order recalled progress snapshots: newest save wins, entry id breaks ties."""
    parsed = entry.get("parsed") or {}
    try:
        ordinal = int(parsed.get("ordinal") or 0)
    except (TypeError, ValueError):
        ordinal = 0
    return ordinal, str(parsed.get("saved_at") or ""), str(entry.get("entry_id") or "")


def recall_query(question: str, title: str = "") -> str:
    """Combine the retrievable anchor with the user's own question."""
    parts = [PROGRESS_PREFIX.rstrip("：")]
    if title:
        parts.append(title)
    cleaned = re.sub(r"\s+", " ", question or "").strip()
    if cleaned:
        parts.append(cleaned)
    return " ".join(parts)


class ScopeStore:
    """JSON file that binds each experiment to its own PowerContext Scope."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "experiments.json")
        self._lock = threading.Lock()
        os.makedirs(data_dir, exist_ok=True)

    def _load(self) -> dict[str, Any]:
        if not os.path.exists(self.path):
            return {"version": 1, "experiments": {}}
        with open(self.path, encoding="utf-8") as handle:
            try:
                payload = json.load(handle)
            except json.JSONDecodeError:
                payload = {}
        if not isinstance(payload, dict) or not isinstance(payload.get("experiments"), dict):
            return {"version": 1, "experiments": {}}
        return payload

    def _save(self, payload: dict[str, Any]) -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False)
        try:
            with handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.path)
        finally:
            if os.path.exists(handle.name):
                os.unlink(handle.name)

    def all(self) -> list[Experiment]:
        with self._lock:
            payload = self._load()
        items = [Experiment(**value) for value in payload["experiments"].values()]
        return sorted(items, key=lambda item: (item.created_at, item.id))

    def get(self, experiment_id: str) -> Experiment | None:
        with self._lock:
            payload = self._load()
        raw = payload["experiments"].get(experiment_id)
        return Experiment(**raw) if raw else None

    def find_by_scope(self, scope_id: str) -> Experiment | None:
        for experiment in self.all():
            if experiment.scope_id == scope_id:
                return experiment
        return None

    def create(self, title: str, scope_id: str, scope_origin: str = "created") -> Experiment:
        experiment = Experiment(
            id=new_experiment_id(title),
            title=title.strip(),
            scope_id=scope_id,
            created_at=utc_now(),
            scope_origin=scope_origin,
        )
        with self._lock:
            payload = self._load()
            payload["experiments"][experiment.id] = asdict(experiment)
            self._save(payload)
        return experiment

    def adopt_default(self, title: str, scope_id: str) -> Experiment:
        with self._lock:
            payload = self._load()
            existing = payload["experiments"].get(DEFAULT_EXPERIMENT_ID)
            if existing:
                return Experiment(**existing)
            experiment = Experiment(
                id=DEFAULT_EXPERIMENT_ID,
                title=title,
                scope_id=scope_id,
                created_at=utc_now(),
                scope_origin="POWERCONTEXT_SCOPE",
            )
            payload["experiments"][experiment.id] = asdict(experiment)
            self._save(payload)
        return experiment

    def record_save(self, experiment_id: str, entry: Experiment) -> Experiment:
        with self._lock:
            payload = self._load()
            payload["experiments"][experiment_id] = asdict(entry)
            self._save(payload)
        return entry


def entry_to_dict(experiment: Experiment) -> dict[str, Any]:
    return asdict(experiment)
