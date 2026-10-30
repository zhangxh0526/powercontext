"""Stdlib HTTP server: static UI plus the PowerContext-backed assistant API."""

from __future__ import annotations

import json
import os
import re
import secrets
import urllib.parse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import DEFAULT_RECALL_QUERY, Config, load_config
from .powercontext import ApiError, PowerContextClient, ServiceUnavailable
from .store import (
    Experiment,
    ScopeStore,
    build_progress_text,
    entry_to_dict,
    parse_progress_text,
    progress_sort_key,
    recall_query,
    slugify,
    utc_now,
)

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/styles.css": "styles.css",
}
EXPERIMENT_ID = re.compile(r"^[\w\u4e00-\u9fff.-]{1,64}$")
ERROR_STATE = {
    "service_unavailable": "service_unavailable",
    "api_error": "api_error",
    "internal_error": "api_error",
}


def ok_payload(**fields: Any) -> dict[str, Any]:
    payload = {"ok": True}
    payload.update(fields)
    return payload


def fail_payload(status: int, error: Exception) -> dict[str, Any]:
    detail = error.as_dict() if isinstance(error, (ServiceUnavailable, ApiError)) else {
        "kind": "internal_error",
        "message": f"{type(error).__name__}: {error}",
    }
    return {
        "ok": False,
        "state": ERROR_STATE.get(detail.get("kind", ""), "api_error"),
        "http_status": status,
        "error": detail,
    }


def status_for(error: Exception) -> int:
    if isinstance(error, ServiceUnavailable):
        return HTTPStatus.SERVICE_UNAVAILABLE
    if isinstance(error, ApiError):
        return HTTPStatus.BAD_GATEWAY
    return HTTPStatus.INTERNAL_SERVER_ERROR


def public_config(config: Config) -> dict[str, Any]:
    return {
        "powercontext_url": config.base_url,
        "powercontext_token_configured": bool(config.powercontext_token),
        "powercontext_scope_configured": config.scope_is_configured,
    }


class Assistant:
    """Wires configuration, the PowerContext client and the local Scope mapping."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.client = PowerContextClient(config)
        self.store = ScopeStore(config.data_dir)
        self.default_experiment = (
            self.store.adopt_default("默认实验", config.powercontext_scope) if config.powercontext_scope else None
        )

    def health(self) -> tuple[int, dict[str, Any]]:
        try:
            readiness = self.client.readiness()
        except (ServiceUnavailable, ApiError) as error:
            return HTTPStatus.OK, fail_payload(status_for(error), error) | {"powercontext": {"status": "unreachable"}}
        ready = readiness.get("status") in ("ready", "degraded")
        payload = ok_payload(state="ready" if ready else "degraded", powercontext=readiness)
        payload["config"] = public_config(self.config)
        payload["experiments"] = len(self.store.all())
        return HTTPStatus.OK, payload

    def create_experiment(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        title = re.sub(r"\s+", " ", str(body.get("title", ""))).strip()
        if not title:
            return HTTPStatus.BAD_REQUEST, {
                "ok": False,
                "state": "invalid_request",
                "error": {"kind": "invalid_request", "message": "实验名称不能为空"},
            }
        if body.get("use_configured_scope"):
            if not self.config.powercontext_scope:
                return HTTPStatus.BAD_REQUEST, {
                    "ok": False,
                    "state": "invalid_request",
                    "error": {
                        "kind": "invalid_request",
                        "message": "未配置 POWERCONTEXT_SCOPE，无法复用默认实验 Scope；请为本实验创建独立 Scope",
                    },
                }
            existing = self.store.find_by_scope(self.config.powercontext_scope)
            experiment = existing or self.store.adopt_default(title, self.config.powercontext_scope)
            origin = "POWERCONTEXT_SCOPE"
        else:
            key = f"qoder-assistant-{slugify(title)}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{secrets.token_hex(3)}"
            scope = self.client.create_scope(
                title=f"实验：{title}",
                summary="科研实验续作助手为该实验创建的独立 Scope，用于隔离实验进展记忆。",
                idempotency_key=key,
            )
            experiment = self.store.create(title, scope["scope_id"], "created")
            origin = "created"
        return HTTPStatus.CREATED, ok_payload(state="created", scope_origin=origin, experiment=entry_to_dict(experiment))

    def progress_entries(self, scope_id: str) -> list[dict[str, Any]]:
        """Every experiment-progress Memory entry currently active in the Scope."""
        parsed: list[dict[str, Any]] = []
        for item in self.client.entries(scope_id):
            entry = parse_progress_text(item.text)
            if entry is None:
                continue
            parsed.append(
                {
                    "entry_id": item.entry_id,
                    "entry_version_id": item.entry_version_id,
                    "kind": item.kind,
                    "text": item.text,
                    "parsed": entry,
                }
            )
        return parsed

    def save(self, experiment: Experiment, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        progress = str(body.get("progress", "")).strip()
        next_step = str(body.get("next_step", "")).strip()
        if not progress or not next_step:
            return HTTPStatus.BAD_REQUEST, {
                "ok": False,
                "state": "invalid_request",
                "error": {"kind": "invalid_request", "message": "当前进展和下一步都不能为空"},
            }
        saved_at = utc_now()
        # 序号取自该 Scope 的真实记忆清单，重启或换机器都不会退化成时间戳并列值。
        ordinals = [int(entry["parsed"]["ordinal"]) for entry in self.progress_entries(experiment.scope_id) if str(entry["parsed"].get("ordinal", "")).isdigit()]
        ordinal = (max(ordinals) if ordinals else 0) + 1
        text = build_progress_text(experiment.title, progress, next_step, saved_at, ordinal)
        result = self.client.remember_decision(experiment.scope_id, text, reason="科研实验续作助手保存实验进展")
        entry = result.get("entry") or {}
        citation = entry.get("citation") or {}
        experiment.progress = progress
        experiment.next_step = next_step
        experiment.saved_at = saved_at
        experiment.ordinal = ordinal
        if citation:
            experiment.entry_id = citation.get("entry_id", "")
            experiment.entry_version_id = citation.get("entry_version_id", "")
        self.store.record_save(experiment.id, experiment)
        return HTTPStatus.OK, ok_payload(
            state="saved",
            scope_id=experiment.scope_id,
            ordinal=ordinal,
            text=text,
            memory=result.get("memory"),
            entry=entry or None,
            deduplicated=not entry,
            experiment=entry_to_dict(experiment),
        )

    def recall(self, experiment: Experiment, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        question = re.sub(r"\s+", " ", str(body.get("question", ""))).strip() or DEFAULT_RECALL_QUERY
        query = recall_query(question, experiment.title)
        prepared = self.client.prepare(experiment.scope_id, query)
        candidates = [
            {
                "entry_id": item.entry_id,
                "entry_version_id": item.entry_version_id,
                "kind": item.kind,
                "text": item.text,
                "parsed": parse_progress_text(item.text),
            }
            for item in prepared.entries
        ]
        inventory = self.progress_entries(experiment.scope_id)
        # 相关度召回是截断的候选集（真实服务在 8 条保存时只返回其中 6 条）。
        # 用同一 Scope 的记忆清单补全候选，保证“最后一次成功保存”一定参与比较。
        by_id = {item["entry_id"]: item for item in candidates if item["parsed"]}
        for item in inventory:
            by_id.setdefault(item["entry_id"], item)
        merged = sorted(by_id.values(), key=progress_sort_key)
        latest = merged[-1] if merged else None
        base = ok_payload(
            scope_id=experiment.scope_id,
            query=query,
            prepared_status=prepared.status,
            content_bytes=prepared.content_bytes,
            candidates=candidates,
            inventory_count=len(inventory),
        )
        if latest is None:
            base["state"] = "no_memory"
            base["latest"] = None
            base["entries"] = []
            base["message"] = "该实验的 Scope 中目前没有可召回的实验进展记忆。"
            return HTTPStatus.OK, base
        in_prepare = any(item["entry_id"] == latest["entry_id"] for item in candidates)
        undecodable = [item for item in merged if not item["parsed"].get("decodable", True)]
        base["state"] = "recalled"
        base["truncated"] = prepared.truncated
        base["content"] = prepared.content
        base["entries"] = merged
        base["latest"] = latest
        base["undecodable_count"] = len(undecodable)
        if not latest["parsed"].get("decodable", True):
            base["warning"] = "最新一条实验进展记忆无法解析为结构化字段，以下按 PowerContext 原文显示。"
        base["recall_sources"] = {
            "latest_from": "context_prepare" if in_prepare else "memory_entries_list",
            "prepare_candidate_count": len([item for item in candidates if item["parsed"]]),
            "inventory_entry_count": len(inventory),
            "prepare_returned_latest": in_prepare,
            "undecodable_entry_count": len(undecodable),
        }
        return HTTPStatus.OK, base


class Handler(SimpleHTTPRequestHandler):
    server_version = "ExperimentContinuationAssistant/1.0"

    def __init__(self, *args: Any, assistant: Assistant, **kwargs: Any) -> None:
        self.assistant = assistant
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def _send(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status if isinstance(status, int) else status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            parsed = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("请求体必须是 UTF-8 JSON") from error
        return parsed if isinstance(parsed, dict) else {}

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/health":
            status, payload = self.assistant.health()
            return self._json(status, payload)
        if path == "/api/experiments":
            payload = ok_payload(state="ready", experiments=[entry_to_dict(item) for item in self.assistant.store.all()])
            payload["config"] = public_config(self.assistant.config)
            return self._json(HTTPStatus.OK, payload)
        if path in STATIC_FILES:
            self.path = "/" + STATIC_FILES[path]
            return SimpleHTTPRequestHandler.do_GET(self)
        self._json(HTTPStatus.NOT_FOUND, {
            "ok": False,
            "state": "api_error",
            "error": {"kind": "not_found", "message": f"未知路径 {path}"},
        })

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        try:
            body = self._read_body()
        except ValueError as error:
            return self._json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "state": "invalid_request", "error": {"kind": "invalid_request", "message": str(error)}},
            )
        try:
            if path == "/api/experiments":
                status, payload = self.assistant.create_experiment(body)
            else:
                outcome = self._experiment_action(path, body)
                if outcome is None:
                    return
                status, payload = outcome
        except Exception as error:  # noqa: BLE001
            status, payload = status_for(error), fail_payload(status_for(error), error)
        self._json(status, payload)

    def _experiment_action(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
        parts = path[len("/api/experiments/") :].split("/")
        experiment_id = urllib.parse.unquote(parts[0])
        action = parts[1] if len(parts) > 1 else ""
        if not EXPERIMENT_ID.match(experiment_id):
            self._json(HTTPStatus.BAD_REQUEST, {
                "ok": False,
                "state": "invalid_request",
                "error": {"kind": "invalid_request", "message": "非法实验 id"},
            })
            return None
        experiment = self.assistant.store.get(experiment_id)
        if experiment is None:
            self._json(HTTPStatus.NOT_FOUND, {
                "ok": False,
                "state": "api_error",
                "error": {"kind": "not_found", "message": f"本地未登记实验 {experiment_id}，请先创建实验"},
            })
            return None
        if action == "save":
            return self.assistant.save(experiment, body)
        if action == "recall":
            return self.assistant.recall(experiment, body)
        self._json(HTTPStatus.NOT_FOUND, {
            "ok": False,
            "state": "api_error",
            "error": {"kind": "not_found", "message": f"实验不支持操作：{action or '(未提供)'}"},
        })
        return None


def build_server(config: Config) -> ThreadingHTTPServer:
    assistant = Assistant(config)

    def factory(*args: Any, **kwargs: Any) -> Handler:
        return Handler(*args, assistant=assistant, **kwargs)

    ThreadingHTTPServer.allow_reuse_address = True
    return ThreadingHTTPServer((config.host, config.port), factory)


def main() -> int:
    config = load_config()
    server = build_server(config)
    print(f"科研实验续作助手已启动：http://{config.host}:{config.port}")
    print(f"PowerContext：{config.base_url}（token {'已配置' if config.powercontext_token else '未配置'}）")
    print(f"实验 Scope 映射：{os.path.join(config.data_dir, 'experiments.json')}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
