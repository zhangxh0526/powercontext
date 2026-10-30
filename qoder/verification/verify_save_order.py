#!/usr/bin/env python3
"""回归验证：快速连续保存后必须召回最后一次成功保存。

对应复核报告 qoder-records/save-order-review.json（8 组中 4 组返回 FIRST）。
本脚本不修改那份失败报告，只把当前结果写入 evidence/。
覆盖三类场景：
1. 同一秒内连续两次保存（原失败场景，8 组重复）；
2. 单个实验超过 6 次保存（相关度召回候选集会被截断）；
3. 应用重启后仍恢复最后一次成功保存。
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "verification"))

from verify_app import (  # noqa: E402
    AppProcess,
    Check,
    Failed,
    check,
    create_experiment,
    free_port,
    log,
    utc,
)

QUESTION = "上次进行到哪里，下一步是什么？"
GROUPS = 8


def recall_progress(app: AppProcess, experiment: dict) -> tuple[str | None, dict]:
    status, body = app.api(
        "POST", f"/api/experiments/{experiment['id']}/recall", {"question": QUESTION}, note="召回最新保存"
    )
    if status != 200:
        raise Failed(f"召回请求失败：HTTP {status} {json.dumps(body, ensure_ascii=False)[:300]}")
    latest = body.get("latest") or {}
    return (latest.get("parsed") or {}).get("progress"), body


def same_second_case(app: AppProcess, checks: list[Check]) -> dict:
    failures = []
    for number in range(GROUPS):
        experiment = create_experiment(app, f"保存顺序回归{uuid.uuid4().hex[:8]}")
        path = f"/api/experiments/{experiment['id']}"
        status, first = app.api("POST", path + "/save", {"progress": "FIRST", "next_step": "STEP-A"}, note=f"第{number}组第一次保存")
        status, second = app.api("POST", path + "/save", {"progress": "SECOND", "next_step": "STEP-B"}, note=f"第{number}组第二次保存")
        if status != 200 or not second.get("ok"):
            raise Failed(f"第{number}组连续保存失败：HTTP {status} {json.dumps(second, ensure_ascii=False)[:300]}")
        actual, body = recall_progress(app, experiment)
        entry = body.get("latest") or {}
        failures.append(
            {
                "expected": "SECOND",
                "actual": actual,
                "first_saved_at": first["experiment"]["saved_at"],
                "second_saved_at": second["experiment"]["saved_at"],
                "first_entry": first["experiment"]["entry_id"],
                "second_entry": second["experiment"]["entry_id"],
                "recalled_entry": entry.get("entry_id"),
                "ordinal": (entry.get("parsed") or {}).get("ordinal"),
                "recall_sources": body.get("recall_sources"),
            }
        )
    wrong = [item for item in failures if item["actual"] != "SECOND"]
    check(
        checks,
        f"同一秒连续保存 {GROUPS} 组全部召回 SECOND",
        not wrong,
        f"失败 {len(wrong)} 组；示例 {json.dumps(failures[0], ensure_ascii=False)[:260]}",
    )
    return {"groups": failures, "failures": len(wrong)}


def many_saves_case(app: AppProcess, checks: list[Check]) -> dict:
    experiment = create_experiment(app, f"多次保存回归{uuid.uuid4().hex[:8]}")
    path = f"/api/experiments/{experiment['id']}"
    last = None
    for number in range(1, 13):
        last = f"SECOND-{number}"
        status, body = app.api(
            "POST",
            path + "/save",
            {"progress": f"SAVE-{number:02d}", "next_step": f"NEXT-{number:02d}"},
            note=f"第 {number} 次保存",
        )
        if status != 200 or not body.get("ok"):
            raise Failed(f"第 {number} 次保存失败：HTTP {status} {json.dumps(body, ensure_ascii=False)[:300]}")
    actual, body = recall_progress(app, experiment)
    parsed = (body.get("latest") or {}).get("parsed") or {}
    candidates = [item for item in body.get("candidates", []) if item.get("parsed")]
    check(
        checks,
        "单个实验 12 次保存后召回最新一次（候选集截断不影响结果）",
        actual == "SAVE-12" and parsed.get("next_step") == "NEXT-12" and parsed.get("ordinal") == "12",
        f"召回 progress={actual}, ordinal={parsed.get('ordinal')}, 召回候选 {len(candidates)} 条 / 清单 {body.get('inventory_count')} 条, "
        f"来源={json.dumps(body.get('recall_sources'), ensure_ascii=False)}",
    )
    return {
        "saves": 12,
        "recalled_progress": actual,
        "recalled_ordinal": parsed.get("ordinal"),
        "prepare_candidates": len(candidates),
        "inventory_count": body.get("inventory_count"),
        "recall_sources": body.get("recall_sources"),
        "latest_entry_id": (body.get("latest") or {}).get("entry_id"),
    }


def truncated_candidate_case(app: AppProcess, checks: list[Check]) -> dict:
    """一直保存到相关度召回不再返回最新条目，验证清单补全真的接管。"""
    experiment = create_experiment(app, f"候选截断回归{uuid.uuid4().hex[:8]}")
    path = f"/api/experiments/{experiment['id']}"
    trigger = None
    saves = 0
    steps: list[dict[str, Any]] = []
    for number in range(1, 25):
        saves = number
        status, body = app.api(
            "POST",
            path + "/save",
            {"progress": f"TRUNC-{number:02d}", "next_step": f"NEXT-{number:02d}"},
            note=f"截断用例第 {number} 次保存",
        )
        if status != 200:
            raise Failed(f"截断用例第 {number} 次保存失败：HTTP {status} {json.dumps(body, ensure_ascii=False)[:300]}")
        _, recalled = app.api("POST", path + "/recall", {"question": QUESTION}, note=f"截断用例第 {number} 次召回")
        sources = recalled.get("recall_sources") or {}
        parsed = ((recalled.get("latest") or {}).get("parsed") or {})
        steps.append({"save": number, "prepare_returned_latest": sources.get("prepare_returned_latest"), "recalled": parsed.get("progress")})
        if parsed.get("progress") != f"TRUNC-{number:02d}":
            raise Failed(
                f"第 {number} 次保存后召回到旧进展：{json.dumps(parsed, ensure_ascii=False)[:200]}，来源={json.dumps(sources, ensure_ascii=False)}"
            )
        if not sources.get("prepare_returned_latest"):
            trigger = {
                "save_count": number,
                "expected_progress": f"TRUNC-{number:02d}",
                "recalled_progress": parsed.get("progress"),
                "recalled_ordinal": parsed.get("ordinal"),
                "recall_sources": sources,
                "latest_entry_id": (recalled.get("latest") or {}).get("entry_id"),
            }
            break
    check(
        checks,
        "连续 24 次保存内每次召回都恢复最新一次成功保存",
        all(step["recalled"] == f"TRUNC-{step['save']:02d}" for step in steps),
        f"共 {len(steps)} 步，最后一步 {steps[-1] if steps else None}",
    )
    check(
        checks,
        "相关度召回漏掉最新保存时由 Scope 记忆清单补全恢复",
        trigger is not None and trigger["recalled_progress"] == trigger["expected_progress"],
        json.dumps(trigger or {"steps": steps, "note": "未出现候选截断"}, ensure_ascii=False)[:320],
    )
    return {"trigger": trigger, "saves": saves, "steps": steps}


def main() -> int:
    powercontext_url = os.getenv("POWERCONTEXT_URL", "http://127.0.0.1:8000")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence = ROOT / "evidence" / f"save-order-{stamp}"
    evidence.mkdir(parents=True, exist_ok=True)
    record: list[dict] = []
    checks: list[Check] = []
    app = AppProcess(free_port(), evidence / "runtime-data", record)
    verdict = "PASS"
    cases: dict[str, dict] = {}
    try:
        app.start(powercontext_url, "save-order")
        cases["same_second"] = same_second_case(app, checks)
        cases["many_saves"] = many_saves_case(app, checks)
        cases["truncated_candidates"] = truncated_candidate_case(app, checks)

        app.stop("restart")
        app.start(powercontext_url, "restarted")
        status, listed = app.api("GET", "/api/experiments", note="重启后实验清单")
        target = next(
            item for item in listed.get("experiments", []) if item["title"].startswith("多次保存回归") and item.get("ordinal") == 12
        )
        actual, body = recall_progress(app, target)
        parsed = (body.get("latest") or {}).get("parsed") or {}
        check(
            checks,
            "应用重启后仍恢复最后一次成功保存",
            actual == "SAVE-12" and parsed.get("ordinal") == "12" and body.get("scope_id") == target["scope_id"],
            f"HTTP 召回 progress={actual}, ordinal={parsed.get('ordinal')}, scope={body.get('scope_id')}",
        )
        cases["after_restart"] = {
            "recalled_progress": actual,
            "recalled_ordinal": parsed.get("ordinal"),
            "entry_id": (body.get("latest") or {}).get("entry_id"),
            "recall_sources": body.get("recall_sources"),
        }
    except Failed as failure:
        log(f"\n连续保存回归失败：{failure}")
        verdict = f"FAIL: {failure}"
    except Exception as failure:  # noqa: BLE001
        log(f"\n连续保存回归异常：{type(failure).__name__}: {failure}")
        verdict = f"FAIL: {failure}"
    finally:
        app.stop("finally")
    payload = {
        "verdict": verdict,
        "finished_at": utc(),
        "powercontext_url": powercontext_url,
        "previous_failure_report": str(ROOT.parent / "qoder-records" / "save-order-review.json"),
        "cases": cases,
        "checks": [item.to_dict() for item in checks],
        "interactions": record,
    }
    path = evidence / "save-order-regression.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"连续保存回归记录：{path}")
    log(f"连续保存回归结论：{verdict}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
