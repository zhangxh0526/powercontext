#!/usr/bin/env python3
"""回归验证：用户输入的中文分号、字段名、引号、HTML 样式文本与换行必须完整往返。

对应复核报告 qoder-records/save-order-and-text-review.json 中的 text_roundtrip：
保存 progress=「完成基础测量」、next_step=「核对说明；下一步：重新测量」时，
旧的“；”分隔正则把用户文本当成字段边界，召回变成 progress=「完成基础测量；下一步：核对说明」。
本脚本通过真实 HTTP（应用后端 + 真实 PowerContext）断言保存、召回、重启后逐字一致，
并断言带锚点但无法解析的历史记录不会被说成“没有记忆”。
只用命令行与 HTTP，不启动任何浏览器或桌面自动化。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

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
ANCHOR = "实验进展"

# 复核报告里真实复现的失败输入。
REVIEW_PROGRESS = "完成基础测量"
REVIEW_NEXT = "核对说明；下一步：重新测量"

# 其他容易踩到字段边界与转义的输入。
TRICKY_TITLE = f"文本往返验收 {ANCHOR}；进展：<b>标签</b> \"{uuid.uuid4().hex[:6]}\""
TRICKY_PROGRESS = '包含 "双引号"、\'单引号\'、中文分号；以及 HTML：<img src=x onerror=alert(1)>\n第二行保留换行'
TRICKY_NEXT = '下一步里也有 进展：与 ；分隔符，还有 {"json": "样式"} 和 \\ 反斜杠'


def write_raw_memory(powercontext_url: str, scope_id: str, text: str) -> dict:
    """直接向真实 PowerContext 写入一条记忆，用于构造历史（旧编码）记录。"""
    url = powercontext_url.rstrip("/") + "/v1/memory/remember"
    body = json.dumps({"scope_id": scope_id, "kind": "decision", "text": text, "reason": "构造历史记录"}, ensure_ascii=False)
    request = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    log(f"已直接向真实服务写入历史记录（scope={scope_id}，条目长度 {len(text)} 字符）")
    return payload


def recall(app: AppProcess, experiment: dict, note: str) -> dict:
    status, body = app.api(
        "POST", f"/api/experiments/{experiment['id']}/recall", {"question": QUESTION}, note=note
    )
    if status != 200:
        raise Failed(f"召回失败：HTTP {status} {json.dumps(body, ensure_ascii=False)[:300]}")
    return body


def save(app: AppProcess, experiment: dict, progress: str, next_step: str, note: str) -> dict:
    status, body = app.api(
        "POST",
        f"/api/experiments/{experiment['id']}/save",
        {"progress": progress, "next_step": next_step},
        note=note,
    )
    if status != 200 or not body.get("ok"):
        raise Failed(f"保存失败：HTTP {status} {json.dumps(body, ensure_ascii=False)[:300]}")
    citation = (body.get("entry") or {}).get("citation") or {}
    return {"response": body, "citation": citation}


def fields(body: dict) -> dict:
    parsed = ((body.get("latest") or {}).get("parsed") or {})
    return {
        "state": body.get("state"),
        "title": parsed.get("title"),
        "progress": parsed.get("progress"),
        "next_step": parsed.get("next_step"),
        "ordinal": parsed.get("ordinal"),
        "saved_at": parsed.get("saved_at"),
        "codec": parsed.get("codec"),
        "decodable": parsed.get("decodable", True),
        "entry_id": (body.get("latest") or {}).get("entry_id"),
        "warning": body.get("warning"),
        "recall_sources": body.get("recall_sources"),
    }


def roundtrip_case(app: AppProcess, checks: list[Check], powercontext_url: str) -> dict:
    """复核报告的原始失败输入：字段内含“；”和字段名。"""
    experiment = create_experiment(app, "文本往返验收 中文分号")
    marker = uuid.uuid4().hex[:8]
    progress = REVIEW_PROGRESS + f"（{marker}）"
    next_step = REVIEW_NEXT + f"（{marker}）"
    saved = save(app, experiment, progress, next_step, "保存含中文分号与字段名的进展")
    citation = saved["citation"]
    check(
        checks,
        "保存响应在 entry.citation 中返回真实 entry/version ID",
        citation.get("entry_id", "").startswith("mem_ent_") and citation.get("entry_version_id", "").startswith("mem_ver_"),
        f"entry={citation.get('entry_id')}, version={citation.get('entry_version_id')}",
    )
    body = recall(app, experiment, "召回含中文分号与字段名的进展")
    got = fields(body)
    check(
        checks,
        "中文分号与字段名不再拆错字段（progress 逐字一致）",
        got["state"] == "recalled" and got["progress"] == progress,
        f"期望 {json.dumps(progress, ensure_ascii=False)}，实际 {json.dumps(got['progress'], ensure_ascii=False)}",
    )
    check(
        checks,
        "中文分号与字段名不再拆错字段（next_step 逐字一致）",
        got["next_step"] == next_step,
        f"期望 {json.dumps(next_step, ensure_ascii=False)}，实际 {json.dumps(got['next_step'], ensure_ascii=False)}",
    )
    check(
        checks,
        "召回条目使用结构化编码且可解析",
        got["decodable"] is True and str(got["codec"]).startswith("json-v"),
        f"codec={got['codec']}, decodable={got['decodable']}",
    )
    check(
        checks,
        "召回使用保存响应返回的同一 entry 身份",
        got["entry_id"] == citation["entry_id"],
        f"召回 entry={got['entry_id']}，保存响应 entry={citation['entry_id']}",
    )
    return {"experiment": experiment, "sent": {"progress": progress, "next_step": next_step}, "recalled": got}


def tricky_case(app: AppProcess, checks: list[Check]) -> dict:
    """引号、HTML 样式文本、反斜杠、换行与实验名称中的字段名。"""
    experiment = create_experiment(app, TRICKY_TITLE)
    marker = uuid.uuid4().hex[:8]
    progress = TRICKY_PROGRESS + f"（{marker}）"
    next_step = TRICKY_NEXT + f"（{marker}）"
    save(app, experiment, progress, next_step, "保存引号/HTML/换行/反斜杠文本")
    body = recall(app, experiment, "召回引号/HTML/换行/反斜杠文本")
    got = fields(body)
    check(
        checks,
        "实验名称中的锚点与字段名完整保留",
        got["title"] == TRICKY_TITLE,
        f"期望 {json.dumps(TRICKY_TITLE, ensure_ascii=False)[:160]}，实际 {json.dumps(got['title'], ensure_ascii=False)[:160]}",
    )
    check(
        checks,
        "引号、HTML 样式文本与换行在 progress 中逐字保留",
        got["progress"] == progress,
        f"期望 {json.dumps(progress, ensure_ascii=False)[:220]}，实际 {json.dumps(got['progress'], ensure_ascii=False)[:220]}",
    )
    check(
        checks,
        "JSON 样式与反斜杠在 next_step 中逐字保留",
        got["next_step"] == next_step,
        f"期望 {json.dumps(next_step, ensure_ascii=False)[:220]}，实际 {json.dumps(got['next_step'], ensure_ascii=False)[:220]}",
    )
    raw_text = ((body.get("latest") or {}).get("text") or "")
    check(
        checks,
        "PowerContext 原文仍带可检索的实验进展锚点",
        raw_text.startswith(f"{ANCHOR}："),
        json.dumps(raw_text[:80], ensure_ascii=False),
    )
    return {"experiment": experiment, "sent": {"title": TRICKY_TITLE, "progress": progress, "next_step": next_step}, "recalled": got}


def legacy_case(app: AppProcess, checks: list[Check], powercontext_url: str) -> dict:
    """旧分隔符记录与新编码共存：必须仍能解析，且序号不能倒退。"""
    experiment = create_experiment(app, "历史编码兼容验收")
    marker = uuid.uuid4().hex[:8]
    saved_at = "2026-10-29T00:00:00Z"
    legacy_text = (
        f"{ANCHOR}：历史编码兼容验收；第 3 次保存；保存于 {saved_at}；"
        f"进展：旧格式记录 A（{marker}）；下一步：旧格式记录 B（{marker}）"
    )
    written = write_raw_memory(powercontext_url, experiment["scope_id"], legacy_text)
    legacy_entry = ((written.get("entry") or {}).get("citation") or {}).get("entry_id")
    body = recall(app, experiment, "仅存在旧分隔符记录时的召回")
    got = fields(body)
    check(
        checks,
        "旧分隔符记录仍被识别为已召回而非无记忆",
        got["state"] == "recalled" and got["entry_id"] == legacy_entry,
        f"state={got['state']}, entry={got['entry_id']}, 写入返回 entry={legacy_entry}",
    )
    check(
        checks,
        "旧分隔符记录的进展与下一步仍按旧格式解析",
        got["codec"] == "legacy-delimiter"
        and got["progress"] == f"旧格式记录 A（{marker}）"
        and got["next_step"] == f"旧格式记录 B（{marker}）"
        and got["ordinal"] == "3",
        json.dumps(got, ensure_ascii=False)[:260],
    )
    progress = f"新格式覆盖旧记录（{marker}）；下一步：仍保持原样"
    next_step = f"新格式下一步（{marker}）"
    save(app, experiment, progress, next_step, "在旧记录之后保存新格式")
    body = recall(app, experiment, "旧记录之后召回最新保存")
    got = fields(body)
    check(
        checks,
        "新保存的序号在旧记录之后且成为最新条目",
        got["ordinal"] == "4" and got["progress"] == progress and got["next_step"] == next_step,
        json.dumps(got, ensure_ascii=False)[:260],
    )
    return {"legacy_text": legacy_text, "after_legacy": got}


def undecodable_case(app: AppProcess, checks: list[Check], powercontext_url: str) -> dict:
    """带锚点但无法结构化的记录：展示原文，不能当作没有记忆。"""
    experiment = create_experiment(app, "不可解析锚点记录验收")
    marker = uuid.uuid4().hex[:8]
    raw = f"{ANCHOR}：这条记录没有字段边界，只是一段自由文本（{marker}）"
    write_raw_memory(powercontext_url, experiment["scope_id"], raw)
    body = recall(app, experiment, "只有带锚点但不可解析记录时的召回")
    got = fields(body)
    check(
        checks,
        "带锚点但无法解析的记录不被当作无记忆",
        got["state"] == "recalled",
        f"state={got['state']}, 原文条目数={len(body.get('entries') or [])}",
    )
    check(
        checks,
        "不可解析记录标记为 undecodable 并给出原文警告",
        got["decodable"] is False and got["codec"] == "undecodable" and isinstance(got["warning"], str) and got["warning"],
        json.dumps({k: got[k] for k in ("codec", "decodable", "warning")}, ensure_ascii=False)[:260],
    )
    check(
        checks,
        "不可解析记录按 PowerContext 原文完整保留",
        got["progress"] == raw[len(f"{ANCHOR}：") :],
        f"期望 {json.dumps(raw, ensure_ascii=False)[:200]}，实际 {json.dumps(got['progress'], ensure_ascii=False)[:200]}",
    )
    # 不可解析条目存在时，最新可解析保存仍必须胜出，不能被当作无记忆。
    progress = f"在不可解析记录之后保存（{marker}）"
    next_step = f"确认最新保存优先（{marker}）"
    save(app, experiment, progress, next_step, "在不可解析记录之后保存")
    body = recall(app, experiment, "不可解析记录之后的召回")
    got_after = fields(body)
    check(
        checks,
        "不可解析记录不影响最新成功保存的召回",
        got_after["state"] == "recalled"
        and got_after["decodable"] is True
        and got_after["progress"] == progress
        and got_after["next_step"] == next_step,
        json.dumps(got_after, ensure_ascii=False)[:260],
    )
    return {"raw": raw, "immediate": got, "after_save": got_after}


def restart_case(app: AppProcess, checks: list[Check], cases: dict) -> dict:
    experiment = cases["roundtrip"]["experiment"]
    expected = cases["roundtrip"]["sent"]
    body = recall(app, experiment, "重启后召回中文分号记录")
    got = fields(body)
    check(
        checks,
        "重启后含中文分号的进展与下一步仍逐字一致",
        got["state"] == "recalled" and got["progress"] == expected["progress"] and got["next_step"] == expected["next_step"],
        json.dumps(got, ensure_ascii=False)[:280],
    )
    tricky = cases["tricky"]["experiment"]
    body = recall(app, tricky, "重启后召回引号/HTML 记录")
    got_tricky = fields(body)
    expected_tricky = cases["tricky"]["sent"]
    check(
        checks,
        "重启后引号、HTML 样式文本与换行仍逐字一致",
        got_tricky["progress"] == expected_tricky["progress"] and got_tricky["next_step"] == expected_tricky["next_step"],
        json.dumps(got_tricky, ensure_ascii=False)[:280],
    )
    return {"roundtrip": got, "tricky": got_tricky}


def main() -> int:
    powercontext_url = os.getenv("POWERCONTEXT_URL", "http://127.0.0.1:8000")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence = ROOT / "evidence" / f"text-roundtrip-{stamp}"
    evidence.mkdir(parents=True, exist_ok=True)
    record: list[dict] = []
    checks: list[Check] = []
    cases: dict[str, dict] = {}
    verdict = "PASS"
    app = AppProcess(free_port(), evidence / "runtime-data", record)
    try:
        app.start(powercontext_url, "text-roundtrip")
        cases["roundtrip"] = roundtrip_case(app, checks, powercontext_url)
        cases["tricky"] = tricky_case(app, checks)
        cases["legacy"] = legacy_case(app, checks, powercontext_url)
        cases["undecodable"] = undecodable_case(app, checks, powercontext_url)
        app.stop("restart")
        app.start(powercontext_url, "restarted")
        cases["after_restart"] = restart_case(app, checks, cases)
    except Failed as failure:
        log(f"\n文本往返回归失败：{failure}")
        verdict = f"FAIL: {failure}"
    except Exception as failure:  # noqa: BLE001
        log(f"\n文本往返回归异常：{type(failure).__name__}: {failure}")
        verdict = f"FAIL: {failure}"
    finally:
        app.stop("finally")
    payload = {
        "verdict": verdict,
        "finished_at": utc(),
        "powercontext_url": powercontext_url,
        "previous_failure_report": str(ROOT.parent / "qoder-records" / "save-order-and-text-review.json"),
        "cases": cases,
        "checks": [item.to_dict() for item in checks],
        "interactions": record,
    }
    path = evidence / "text-roundtrip.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"文本往返回归记录：{path}")
    log(f"文本往返回归结论：{verdict}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
