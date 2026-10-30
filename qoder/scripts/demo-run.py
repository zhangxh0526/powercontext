#!/usr/bin/env python3
"""端到端演示：真实 HTTP 走完「创建实验 → 保存进展 → 召回 → 重启进程 → 再次召回」。

用 curl 等价的真实 HTTP 请求驱动正在运行的应用（默认 http://127.0.0.1:8710），
把每一步的真实请求与响应写入 evidence/demo-run-<时间戳>/。
脚本只使用命令行与 HTTP，不启动任何浏览器或桌面自动化。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = os.getenv("ASSISTANT_BASE", "http://127.0.0.1:8710")
TITLE = "演示实验 RDMA 尾延迟"
PROGRESS = '完成基础测量；备注 <b>原样文本</b> 和 "控制组A"'
NEXT = "核对说明；下一步：固定线程数后重新测量 P99 延迟"
QUESTION = "上次进行到哪里，下一步是什么？"

steps: list[dict] = []


def call(label: str, method: str, path: str, body: dict | None = None) -> dict:
    url = BASE + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    payload = json.loads(raw.decode("utf-8"))
    steps.append(
        {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "label": label,
            "request": {"method": method, "url": url, "body": body},
            "http_status": status,
            "response": payload,
        }
    )
    print(f"$ {method} {url}")
    print(json.dumps(payload, ensure_ascii=False, indent=2)[:1400])
    print()
    return payload


def restart_server() -> None:
    print("$ scripts/dev-server.sh restart")
    subprocess.run([str(ROOT / "scripts" / "dev-server.sh"), "restart"], cwd=str(ROOT), check=True)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=3) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.3)
    raise SystemExit("重启后应用未在 30 秒内恢复响应")


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence = ROOT / "evidence" / f"demo-run-{stamp}"
    evidence.mkdir(parents=True, exist_ok=True)

    health = call("检查服务状态", "GET", "/api/health")
    if health.get("state") not in ("ready", "degraded"):
        raise SystemExit(f"PowerContext 不可用：{json.dumps(health, ensure_ascii=False)[:200]}")

    created = call("创建实验（自动绑定独立 Scope）", "POST", "/api/experiments", {"title": TITLE})
    experiment = created["experiment"]
    experiment_id = experiment["id"]

    saved = call(
        "保存进展（含中文分号、字段名、引号与 HTML 样式文本）",
        "POST",
        f"/api/experiments/{experiment_id}/save",
        {"progress": PROGRESS, "next_step": NEXT},
    )
    citation = (saved.get("entry") or {}).get("citation") or {}

    recalled = call(
        "召回上次进展",
        "POST",
        f"/api/experiments/{experiment_id}/recall",
        {"question": QUESTION},
    )
    latest = recalled.get("latest") or {}
    parsed = latest.get("parsed") or {}

    restart_server()
    recalled_after_restart = call(
        "重启进程后再次召回（同一数据目录、同一 Scope）",
        "POST",
        f"/api/experiments/{experiment_id}/recall",
        {"question": QUESTION},
    )
    after = recalled_after_restart.get("latest") or {}
    after_parsed = after.get("parsed") or {}

    results = {
        "experiment_id": experiment_id,
        "scope_id": experiment["scope_id"],
        "saved_entry_id": citation.get("entry_id"),
        "saved_entry_version_id": citation.get("entry_version_id"),
        "recalled_progress": parsed.get("progress"),
        "recalled_next_step": parsed.get("next_step"),
        "codec": parsed.get("codec"),
        "after_restart_entry_id": after.get("entry_id"),
        "after_restart_progress": after_parsed.get("progress"),
        "after_restart_next_step": after_parsed.get("next_step"),
        "exact_roundtrip": parsed.get("progress") == PROGRESS and parsed.get("next_step") == NEXT,
        "exact_roundtrip_after_restart": after.get("entry_id") == citation.get("entry_id")
        and after_parsed.get("progress") == PROGRESS
        and after_parsed.get("next_step") == NEXT,
    }
    document = {
        "verdict": "PASS" if results["exact_roundtrip"] and results["exact_roundtrip_after_restart"] else "FAIL",
        "assistant_base": BASE,
        "sent": {"title": TITLE, "progress": PROGRESS, "next_step": NEXT, "question": QUESTION},
        "results": results,
        "steps": steps,
    }
    path = evidence / "demo-run.json"
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines: list[str] = []
    for step in steps:
        lines.append(f"$ {step['request']['method']} {step['request']['url']}")
        lines.append(json.dumps(step["request"]["body"], ensure_ascii=False) if step["request"]["body"] else "(无请求体)")
        lines.append(f"HTTP {step['http_status']}")
        lines.append(json.dumps(step["response"], ensure_ascii=False, indent=2))
        lines.append("")
    (evidence / "demo-run.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"演示记录：{path}")
    return 0 if document["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
