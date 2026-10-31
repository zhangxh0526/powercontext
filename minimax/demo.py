import json
import os
from pathlib import Path

from minimax.context_manager import SeekdbContextStore


ROOT = Path(__file__).resolve().parent


def main():
    store = SeekdbContextStore(
        host=os.getenv("SEEKDB_HOST", "127.0.0.1"),
        port=int(os.getenv("SEEKDB_PORT", "12881")),
        user=os.getenv("SEEKDB_USER", "root"),
        password=os.getenv("SEEKDB_PASSWORD", ""),
        database=os.getenv("SEEKDB_DATABASE", "minimax_sdd"),
    )
    store.reset()
    for name in ("spec_v1", "design", "code", "tests"):
        store.import_json(ROOT / "samples" / f"{name}.json")
    first = store.prepare("实现 normalize_status 的状态管理", terms=["normalize_status", "状态", "test"])
    print("FIRST_CONTEXT")
    print(json.dumps(first, ensure_ascii=False, indent=2))
    store.close()

    store = SeekdbContextStore(
        host=os.getenv("SEEKDB_HOST", "127.0.0.1"),
        port=int(os.getenv("SEEKDB_PORT", "12881")),
        user=os.getenv("SEEKDB_USER", "root"),
        password=os.getenv("SEEKDB_PASSWORD", ""),
        database=os.getenv("SEEKDB_DATABASE", "minimax_sdd"),
    )
    restored = store.last_task()
    print("RESTORED_CONTEXT")
    print(json.dumps(restored, ensure_ascii=False, indent=2))
    store.import_json(ROOT / "samples" / "spec_v2.json")
    updated = store.prepare(restored["task"], terms=["normalize_status", "状态", "test"])
    print("UPDATED_CONTEXT")
    print(json.dumps(updated, ensure_ascii=False, indent=2))
    store.close()


if __name__ == "__main__":
    main()
