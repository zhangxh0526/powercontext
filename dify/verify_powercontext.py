import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen


BASE_URL = os.getenv("POWERCONTEXT_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.getenv("POWERCONTEXT_TOKEN")


def post(path, payload):
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    request = Request(
        f"{BASE_URL}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            return json.load(response)
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"PowerContext {path} failed: HTTP {error.code}: {detail}") from error


def main():
    scope = post(
        "/v1/scopes",
        {
            "title": "Dify memory NPC demo",
            "summary": "Two-turn verification of player-choice memory",
            "idempotency_key": "dify-memory-npc-demo",
        },
    )
    scope_id = scope["scope_id"]
    remembered = post(
        "/v1/memory/remember",
        {
            "scope_id": scope_id,
            "kind": "decision",
            "text": "玩家选择：前往森林并带上地图",
            "reason": "Dify NPC verification",
        },
    )
    prepared = post(
        "/v1/context/prepare",
        {
            "scope_id": scope_id,
            "query": "玩家选择 玩家之前去了哪里，带了什么？",
            "max_bytes": 4000,
        },
    )
    content = prepared.get("content") or ""
    assert prepared.get("status") == "ready", prepared
    assert "森林" in content and "地图" in content, prepared
    print(f"POWERCONTEXT_SCOPE={scope_id}")
    print(json.dumps({"remembered": remembered, "prepared": prepared}, ensure_ascii=False, indent=2))
    print("PASS: the next query recalls both forest and map")


if __name__ == "__main__":
    main()
