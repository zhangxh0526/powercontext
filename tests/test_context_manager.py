import os
import unittest
from pathlib import Path

from stepfun.context_manager import SeekdbContextStore


ROOT = Path(__file__).resolve().parents[1]


class SeekdbContextStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = SeekdbContextStore(
            host=os.getenv("SEEKDB_HOST", "127.0.0.1"),
            port=int(os.getenv("SEEKDB_PORT", "12881")),
            user=os.getenv("SEEKDB_USER", "root"),
            password=os.getenv("SEEKDB_PASSWORD", ""),
            database=os.getenv("SEEKDB_DATABASE", "stepfun_sdd"),
        )
        self.store.reset()
        for name in ("spec_v1", "design", "code", "tests"):
            self.store.import_json(ROOT / "samples" / f"{name}.json")

    def tearDown(self):
        self.store.reset()
        self.store.close()

    def test_context_update_and_task_state_restart(self):
        context = self.store.prepare(
            "实现 normalize_status 的状态管理",
            terms=["normalize_status", "状态", "test"],
        )
        self.assertGreater(context["record_id"], 0)
        self.assertEqual(
            {"spec-status", "design-status", "code-status", "test-status"},
            {document["doc_id"] for document in context["documents"]},
        )
        self.assertEqual(1, self._document(context, "spec-status")["revision"])
        self.store.close()

        self.store = SeekdbContextStore(
            host=os.getenv("SEEKDB_HOST", "127.0.0.1"),
            port=int(os.getenv("SEEKDB_PORT", "12881")),
            user=os.getenv("SEEKDB_USER", "root"),
            password=os.getenv("SEEKDB_PASSWORD", ""),
            database=os.getenv("SEEKDB_DATABASE", "stepfun_sdd"),
        )
        restored = self.store.last_task()
        self.assertIsNotNone(restored)
        self.assertEqual(context["record_id"], restored["record_id"])
        self.assertEqual(context["task"], restored["task"])
        continued = self.store.prepare(restored["task"], terms=["normalize_status", "状态", "test"])
        self.assertIn("spec-status", {document["doc_id"] for document in continued["documents"]})

        self.store.import_json(ROOT / "samples" / "spec_v2.json")
        updated = self.store.prepare("更新 normalize_status 规格", terms=["normalize_status"])
        self.assertEqual(2, self._document(updated, "spec-status")["revision"])
        self.assertIn("空字符串", self._document(updated, "spec-status")["content"])

    @staticmethod
    def _document(context, doc_id):
        return next(document for document in context["documents"] if document["doc_id"] == doc_id)


if __name__ == "__main__":
    unittest.main()
