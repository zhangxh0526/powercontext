#!/usr/bin/env python3
"""科研实验续作助手：真实应用验收。

脚本自行启动应用（独立端口与数据目录），通过真实 HTTP 请求依次验证：
新建空实验、写入 PowerContext 记忆、按 Scope 召回、不同实验隔离、
应用重启后使用同一 Scope 恢复、以及 PowerContext 服务错误时的显示状态。
每一步记录真实请求与响应，写入 evidence/ 目录；任一断言失败以非零状态退出。
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_DIR = ROOT / "evidence"
MARKER_A = "P99-9f3a"
MARKER_B = "IOPS-7c11"
PROGRESS_A = f"已完成 4KB 与 64KB 消息对比，尾延迟使用 {MARKER_A} 标记，中位数稳定但尾部抖动明显。"
NEXT_A = f"固定线程数后重新测量 {MARKER_A} 并保存脚本参数。"
PROGRESS_B = f"冷启动写入首轮完成，用 {MARKER_B} 标记写入放大 3.2 倍。"
NEXT_B = f"调整 page size 到 16KB 后复测 {MARKER_B}。"
QUESTION = "上次进行到哪里，下一步是什么？"


class Check:
    def __init__(self, name: str, condition: bool, detail: str) -> None:
        self.name = name
        self.ok = condition
        self.detail = detail

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


class Failed(Exception):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def http(method: str, url: str, payload: dict | None = None, timeout: float = 20.0) -> tuple[int, dict]:
    parts = urllib.parse.urlsplit(url)
    url = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, urllib.parse.quote(parts.path, safe="/!$&'()*+,;=:@-._~"), parts.query, parts.fragment)
    )
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read()
        status = error.code
    except urllib.error.URLError as error:
        return 0, {"transport_error": f"{type(error.reason).__name__}: {error.reason}"}
    try:
        return status, json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return status, {"raw": raw.decode("utf-8", errors="replace")[:2000]}


class AppProcess:
    def __init__(self, port: int, data_dir: Path, record: list) -> None:
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self.data_dir = data_dir
        self.record = record
        self.process: subprocess.Popen | None = None
        self.log_handle = None
        self.launches: list[dict] = []

    def start(self, powercontext_url: str, label: str) -> None:
        env = dict(os.environ)
        env.update(
            POWERCONTEXT_URL=powercontext_url,
            ASSISTANT_DATA_DIR=str(self.data_dir),
            ASSISTANT_HOST="127.0.0.1",
            ASSISTANT_PORT=str(self.port),
            PYTHONPATH=str(ROOT),
            PYTHONUNBUFFERED="1",
        )
        env.pop("POWERCONTEXT_TOKEN", None)
        env.pop("POWERCONTEXT_SCOPE", None)
        os.makedirs(self.data_dir, exist_ok=True)
        log_path = self.data_dir / f"app-{label}.log"
        self.log_handle = open(log_path, "w", encoding="utf-8")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "backend.server"],
            cwd=str(ROOT),
            env=env,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.launches.append({"label": label, "pid": self.process.pid, "log": str(log_path), "started_at": utc()})
        self.record.append({
            "event": "app_start",
            "label": label,
            "pid": self.process.pid,
            "powercontext_url": powercontext_url,
            "data_dir": str(self.data_dir),
            "log": str(log_path),
        })
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise Failed(f"应用启动失败（退出码 {self.process.returncode}），日志见 {log_path}")
            status, body = http("GET", f"{self.base_url}/api/health", timeout=5)
            if status == 200:
                self.record.append({"event": "app_ready", "label": label, "health": body, "at": utc()})
                return
            time.sleep(0.4)
        raise Failed(f"应用在 25 秒内没有响应 {self.base_url}/api/health，日志见 {log_path}")

    def stop(self, label: str) -> None:
        if self.process is None:
            return
        pid = self.process.pid
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            self.process.wait(timeout=10)
        self.record.append({"event": "app_stop", "label": label, "pid": pid, "returncode": self.process.returncode, "at": utc()})
        log(f"已停止应用进程 pid={pid}（退出码 {self.process.returncode}）")
        self.process = None
        if self.log_handle is not None:
            self.log_handle.close()
            self.log_handle = None

    def api(self, method: str, path: str, payload: dict | None = None, note: str = "") -> tuple[int, dict]:
        url = f"{self.base_url}{path}"
        status, body = http(method, url, payload)
        self.record.append({
            "note": note,
            "method": method,
            "url": url,
            "request": payload,
            "http_status": status,
            "response": body,
            "at": utc(),
        })
        return status, body


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check(checks: list[Check], name: str, condition: bool, detail: str) -> None:
    checks.append(Check(name, condition, detail))
    log(f"{'PASS' if condition else 'FAIL'}  {name}：{detail}")
    if not condition:
        raise Failed(f"{name}：{detail}")


def create_experiment(app: AppProcess, title: str) -> dict:
    status, body = app.api("POST", "/api/experiments", {"title": title}, note=f"创建实验 {title}")
    if status != 201 or not body.get("ok"):
        raise Failed(f"创建实验 {title} 失败：HTTP {status} {json.dumps(body, ensure_ascii=False)[:400]}")
    return body["experiment"]


def main() -> int:
    powercontext_url = os.getenv("POWERCONTEXT_URL", "http://127.0.0.1:8000")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_dir = EVIDENCE_DIR / f"app-{stamp}"
    data_dir = evidence_dir / "runtime-data"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    record: list[dict] = []
    checks: list[Check] = []

    app = AppProcess(free_port(), data_dir, record)
    atexit.register(lambda: app.stop("cleanup"))
    try:
        app.start(powercontext_url, "initial")

        status, health = app.api("GET", "/api/health", note="检查 PowerContext 可达性")
        check(
            checks,
            "应用启动且 PowerContext 就绪",
            status == 200 and health.get("ok") and health.get("powercontext", {}).get("status") in ("ready", "degraded"),
            f"HTTP {status}, powercontext={json.dumps(health.get('powercontext'), ensure_ascii=False)[:120]}",
        )

        experiment_a = create_experiment(app, "验收实验A RDMA 尾延迟")
        status, empty = app.api(
            "POST", f"/api/experiments/{experiment_a['id']}/recall", {"question": QUESTION}, note="新空实验首次召回"
        )
        check(
            checks,
            "新实验首次召回显示无记忆",
            status == 200 and empty.get("state") == "no_memory" and empty.get("entries") == [],
            f"HTTP {status}, state={empty.get('state')}, prepared_status={empty.get('prepared_status')}",
        )

        status, saved = app.api(
            "POST",
            f"/api/experiments/{experiment_a['id']}/save",
            {"progress": PROGRESS_A, "next_step": NEXT_A},
            note="保存实验A进展（真实 /v1/memory/remember 写入）",
        )
        entry = saved.get("entry") or {}
        citation = entry.get("citation") or {}
        check(
            checks,
            "写入 PowerContext 返回 decision Memory 条目",
            status == 200 and saved.get("state") == "saved" and citation.get("entry_id", "").startswith("mem_ent_"),
            f"HTTP {status}, entry={citation.get('entry_id')}, version={citation.get('entry_version_id')}",
        )

        status, recalled = app.api(
            "POST", f"/api/experiments/{experiment_a['id']}/recall", {"question": QUESTION}, note="实验A召回"
        )
        latest = recalled.get("latest") or {}
        parsed = latest.get("parsed") or {}
        check(
            checks,
            "召回返回实验名称、进展与下一步",
            status == 200
            and recalled.get("state") == "recalled"
            and parsed.get("title") == experiment_a["title"]
            and MARKER_A in parsed.get("progress", "")
            and MARKER_A in parsed.get("next_step", "")
            and latest.get("entry_id") == citation.get("entry_id"),
            f"HTTP {status}, state={recalled.get('state')}, parsed={json.dumps(parsed, ensure_ascii=False)[:220]}",
        )

        experiment_b = create_experiment(app, "验收实验B 冷启动写入")
        check(
            checks,
            "不同实验绑定不同 Scope",
            experiment_b["scope_id"] != experiment_a["scope_id"],
            f"A={experiment_a['scope_id']}, B={experiment_b['scope_id']}",
        )
        app.api(
            "POST",
            f"/api/experiments/{experiment_b['id']}/save",
            {"progress": PROGRESS_B, "next_step": NEXT_B},
            note="保存实验B进展",
        )
        status, recall_a_after_b = app.api(
            "POST", f"/api/experiments/{experiment_a['id']}/recall", {"question": QUESTION}, note="实验B写入后召回实验A"
        )
        texts_a = " ".join(item["text"] for item in recall_a_after_b.get("entries", []))
        check(
            checks,
            "实验隔离：A 的召回不含 B 的记忆",
            status == 200 and recall_a_after_b.get("state") == "recalled" and MARKER_B not in texts_a and MARKER_A in texts_a,
            f"HTTP {status}, A 召回条目 {len(recall_a_after_b.get('entries', []))} 条，含 B 标记={MARKER_B in texts_a}",
        )
        status, recall_b = app.api(
            "POST", f"/api/experiments/{experiment_b['id']}/recall", {"question": QUESTION}, note="召回实验B"
        )
        texts_b = " ".join(item["text"] for item in recall_b.get("entries", []))
        check(
            checks,
            "实验隔离：B 只召回自己的进展",
            status == 200 and MARKER_B in texts_b and MARKER_A not in texts_b,
            f"HTTP {status}, 含 A 标记={MARKER_A in texts_b}",
        )

        app.stop("before-restart")
        app.start(powercontext_url, "restarted")
        status, listed = app.api("GET", "/api/experiments", note="重启后读取实验与 Scope 映射")
        ids = {item["id"]: item["scope_id"] for item in listed.get("experiments", [])}
        check(
            checks,
            "重启后实验与 Scope 映射仍然存在",
            experiment_a["id"] in ids and ids[experiment_a["id"]] == experiment_a["scope_id"],
            f"HTTP {status}, 已登记实验 {len(ids)} 个",
        )
        status, recovered = app.api(
            "POST", f"/api/experiments/{experiment_a['id']}/recall", {"question": QUESTION}, note="重启后召回实验A"
        )
        recovered_latest = recovered.get("latest") or {}
        recovered_parsed = recovered_latest.get("parsed") or {}
        check(
            checks,
            "重启后同一 Scope 恢复已保存进展与下一步",
            status == 200
            and recovered.get("state") == "recalled"
            and recovered.get("scope_id") == experiment_a["scope_id"]
            and MARKER_A in recovered_parsed.get("progress", "")
            and MARKER_A in recovered_parsed.get("next_step", "")
            and recovered_latest.get("entry_id") == citation.get("entry_id"),
            f"HTTP {status}, entry={recovered_latest.get('entry_id')}, parsed={json.dumps(recovered_parsed, ensure_ascii=False)[:200]}",
        )

        status, invalid = app.api(
            "POST",
            f"/api/experiments/{experiment_b['id']}/save",
            {"progress": "只有进展", "next_step": "   "},
            note="字段为空时的校验请求",
        )
        check(
            checks,
            "空的进展或下一步被校验拒绝且不写入",
            status == 400 and invalid.get("state") == "invalid_request",
            f"HTTP {status}, state={invalid.get('state')}, message={json.dumps(invalid.get('error'), ensure_ascii=False)[:160]}",
        )
        status, oversized = app.api(
            "POST",
            f"/api/experiments/{experiment_b['id']}/save",
            {"progress": "超" * 8300, "next_step": "超出服务单条记忆字节上限的进展"},
            note="超出 8192 UTF-8 字节时的保存请求",
        )
        check(
            checks,
            "超出 8192 字节的记忆被拒绝而非截断写入",
            status == 502 and oversized.get("state") == "api_error" and oversized.get("error", {}).get("code") == "text_too_long",
            f"HTTP {status}, code={oversized.get('error', {}).get('code')}, message={str(oversized.get('error', {}).get('message'))[:120]}",
        )
        status, recalled_b = app.api(
            "POST", f"/api/experiments/{experiment_b['id']}/recall", {"question": QUESTION}, note="拒绝写入后召回实验B"
        )
        check(
            checks,
            "两次被拒绝的请求没有产生新的记忆条目",
            status == 200 and len(recalled_b.get("entries") or []) == 1
            and MARKER_B in ((recalled_b.get("latest") or {}).get("parsed") or {}).get("progress", ""),
            f"HTTP {status}, 条目 {len(recalled_b.get('entries') or [])} 条",
        )

        with open(data_dir / "experiments.json", encoding="utf-8") as handle:
            mapping = json.load(handle)
        tampered = dict(mapping["experiments"][experiment_a["id"]])
        tampered["scope_id"] = "scp_qoder_acceptance_missing_scope"
        mapping["experiments"][experiment_a["id"]] = tampered
        with open(data_dir / "experiments.json", "w", encoding="utf-8") as handle:
            json.dump(mapping, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        status, api_error = app.api(
            "POST", f"/api/experiments/{experiment_a['id']}/recall", {"question": QUESTION}, note="接口错误显示（不存在的 Scope）"
        )
        check(
            checks,
            "接口错误显示为 api_error 而非成功召回",
            status == 502 and api_error.get("state") == "api_error" and api_error.get("error", {}).get("status") == 404,
            f"HTTP {status}, state={api_error.get('state')}, error={json.dumps(api_error.get('error'), ensure_ascii=False)[:200]}",
        )
        mapping["experiments"][experiment_a["id"]] = dict(experiment_a)
        with open(data_dir / "experiments.json", "w", encoding="utf-8") as handle:
            json.dump(mapping, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

        app.stop("before-unreachable")
        dead_port = free_port()
        app.start(f"http://127.0.0.1:{dead_port}", "unreachable")
        status, down_health = app.api("GET", "/api/health", note="PowerContext 不可用时的健康检查")
        check(
            checks,
            "服务不可用显示 service_unavailable",
            status == 200 and down_health.get("ok") is False and down_health.get("state") == "service_unavailable",
            f"HTTP {status}, state={down_health.get('state')}",
        )
        status, down_save = app.api(
            "POST",
            f"/api/experiments/{experiment_b['id']}/save",
            {"progress": PROGRESS_B, "next_step": NEXT_B},
            note="PowerContext 不可用时的保存请求",
        )
        check(
            checks,
            "服务不可用时保存请求不伪装成功",
            status == 503 and down_save.get("ok") is False and down_save.get("state") == "service_unavailable",
            f"HTTP {status}, state={down_save.get('state')}, message={json.dumps(down_save.get('error'), ensure_ascii=False)[:180]}",
        )
        status, down_recall = app.api(
            "POST", f"/api/experiments/{experiment_b['id']}/recall", {"question": QUESTION}, note="PowerContext 不可用时的召回"
        )
        check(
            checks,
            "服务不可用时召回请求不伪装成功",
            status == 503 and down_recall.get("state") == "service_unavailable",
            f"HTTP {status}, state={down_recall.get('state')}",
        )
        app.stop("final")

        if app.launches and data_dir.exists():
            shutil.copytree(data_dir, evidence_dir / "final-runtime-data", dirs_exist_ok=True)
        return write_evidence(evidence_dir, checks, record, "PASS", powercontext_url)
    except Failed as failure:
        log(f"\n验收失败：{failure}")
        app.stop("failure")
        return write_evidence(evidence_dir, checks, record, f"FAIL: {failure}", powercontext_url)
    finally:
        app.stop("atexit-guard")


def write_evidence(evidence_dir: Path, checks: list[Check], record: list[dict], verdict: str, powercontext_url: str) -> int:
    summary = {
        "verdict": verdict,
        "started_at": next((item["at"] for item in record if "at" in item), utc()),
        "finished_at": utc(),
        "powercontext_url": powercontext_url,
        "checks": [item.to_dict() for item in checks],
        "interactions": record,
    }
    path = evidence_dir / "app-acceptance.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"\n真实请求与响应记录：{path}")
    log(f"验收结论：{verdict}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
