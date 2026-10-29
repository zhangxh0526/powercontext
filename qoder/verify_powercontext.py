import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen


BASE_URL = os.getenv("POWERCONTEXT_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.getenv("POWERCONTEXT_TOKEN")


def request(path, payload=None):
    headers = {}
    data = None
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(BASE_URL + path, data=data, headers=headers), timeout=15) as response:
            return json.load(response)
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{path}: HTTP {error.code}: {detail}") from error


def main():
    health = request("/health/ready")
    assert health["status"] in ("ready", "degraded"), health
    scope = request(
        "/v1/scopes",
        {
            "title": "RDMA experiment continuation",
            "summary": "Qoder research progress verification",
            "idempotency_key": "qoder-research-continuation-v1",
        },
    )
    scope_id = scope["scope_id"]
    text = "实验进展：已比较 4KB 与 64KB 消息；下一步固定线程数并重新测量 P99 延迟。"
    remembered = request(
        "/v1/memory/remember",
        {"scope_id": scope_id, "kind": "decision", "text": text, "reason": "Qoder verification"},
    )
    prepared = request(
        "/v1/context/prepare",
        {
            "scope_id": scope_id,
            "query": "实验进展 上次进行到哪里，下一步是什么？",
            "max_bytes": 4000,
            "assembly": {"sections": [{"family": "memory", "limit": 6}]},
        },
    )
    content = prepared.get("content") or ""
    assert prepared.get("status") == "ready", prepared
    assert all(value in content for value in ("4KB", "64KB", "P99")), prepared
    print(f"POWERCONTEXT_SCOPE={scope_id}")
    print(json.dumps({"remembered": remembered, "prepared": prepared}, ensure_ascii=False, indent=2))
    print("PASS: PowerContext returns the saved experiment progress and next step")
    print("Restart the application with this scope and repeat its own recall check before submission.")


if __name__ == "__main__":
    main()
