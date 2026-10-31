import importlib
import os
import unittest


status_module = importlib.import_module(os.getenv("SDD_STATUS_MODULE", "src.status"))


class NormalizeStatusTest(unittest.TestCase):
    def test_ready_is_case_insensitive(self):
        self.assertEqual("ready", status_module.normalize_status("READY"))

    def test_spaces_become_underscores(self):
        self.assertEqual("in_progress", status_module.normalize_status("In Progress"))

    def test_hyphens_become_underscores(self):
        self.assertEqual("in_progress", status_module.normalize_status("in-progress"))

    def test_done(self):
        self.assertEqual("done", status_module.normalize_status("done"))

    def test_unknown_status_is_rejected(self):
        with self.assertRaises(ValueError):
            status_module.normalize_status("unknown")

    def test_empty_status_is_rejected(self):
        with self.assertRaises(ValueError):
            status_module.normalize_status("  ")


if __name__ == "__main__":
    unittest.main()
