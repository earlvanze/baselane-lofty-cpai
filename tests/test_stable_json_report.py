import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).absolute().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "stable_json_report", ROOT / "scripts" / "stable_json_report.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class StableJsonReportTests(unittest.TestCase):
    def test_digest_ignores_generated_at(self):
        first = {"generated_at": "one", "status": "ok", "records": [{"a": 1}]}
        second = {"generated_at": "two", "status": "ok", "records": [{"a": 1}]}
        self.assertEqual(
            MODULE.stable_report_digest(first),
            MODULE.stable_report_digest(second),
        )

    def test_unchanged_report_preserves_timestamp_and_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            first = MODULE.write_json_report(
                path, {"generated_at": "one", "status": "ok"}
            )
            before = path.read_bytes()
            second = MODULE.write_json_report(
                path, {"generated_at": "two", "status": "ok"}
            )
            self.assertEqual(first, second)
            self.assertEqual(path.read_bytes(), before)
