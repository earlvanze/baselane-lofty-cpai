import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = str(ROOT / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import baselane_canonical_financial_artifact_cleanup as cleanup


def hard_delete(path: Path) -> str:
    path.unlink()
    return "test_unlink"


class CanonicalFinancialArtifactCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "Real Estate"
        self.global_root = Path(self.temp.name) / "assetrail"
        self.owner = self.root / "OH" / "Example Public" / "07 - P&L & Owner Statements"
        self.snapshot = self.root / "OH" / "Example Public" / "00 - README & Property Snapshot"
        self.owner.mkdir(parents=True)
        self.snapshot.mkdir(parents=True)
        self.global_root.mkdir()
        (self.global_root / "ECO Systems General Ledger.csv").write_bytes(b"canonical")

    def tearDown(self):
        self.temp.cleanup()

    def apply_plan(self, stale_report=None):
        plan, reviews = cleanup.build_plan(self.root, stale_report, self.global_root)
        results = [cleanup.execute_action(action, True, hard_delete) for action in plan]
        self.assertFalse(reviews)
        self.assertTrue(all(result.status == "applied" for result in results))
        return plan, results

    def test_keeps_canonical_financials_and_removes_current_alias_and_conflict(self):
        canonical = self.snapshot / "FINANCIALS.md"
        alias = self.owner.parent / "FINANCIALS-July-2026.md"
        conflict = self.owner.parent / "DETAILS (conflicted copy 2026-08-01).md"
        details = self.owner.parent / "DETAILS.md"
        for path, text in ((canonical, "canonical"), (alias, "alias"), (conflict, "conflict"), (details, "details")):
            path.write_text(text, encoding="utf-8")

        self.apply_plan()

        self.assertTrue(canonical.exists())
        self.assertTrue(details.exists())
        self.assertFalse(alias.exists())
        self.assertFalse(conflict.exists())

    def test_stale_report_paths_are_removed_but_pre_2026_history_and_transactions_remain(self):
        stale = self.owner / "P&L Statement - 2026-01 - Example.xlsx"
        historical = self.owner / "Cash Flow Statement - Example - 2025.xlsx"
        transactions = self.owner / "Transactions (July, 2026).csv"
        for path in (stale, historical, transactions):
            path.write_bytes(b"content")
        report = Path(self.temp.name) / "stale.json"
        report.write_text(json.dumps({"issues": [{"code": "stale", "detail": str(stale)}]}), encoding="utf-8")

        self.apply_plan(report)

        self.assertFalse(stale.exists())
        self.assertTrue(historical.exists())
        self.assertTrue(transactions.exists())

    def test_conflict_financial_artifacts_and_global_derivatives_are_removed(self):
        cf = self.owner / "Cash Flow Statement - Example (conflicted copy 2026-08-01).xlsx"
        gl = self.owner / "ECO Systems General Ledger - Example.bak.csv"
        canonical_global = self.global_root / "ECO Systems General Ledger.csv"
        derivative = self.global_root / "ECO Systems General Ledger.filtered.20260801.csv"
        historical = self.global_root / "ECO Systems General Ledger 2025.csv"
        for path in (cf, gl, canonical_global, derivative, historical):
            path.write_bytes(path.name.encode())

        self.apply_plan()

        self.assertFalse(cf.exists())
        self.assertFalse(gl.exists())
        self.assertTrue(canonical_global.exists())
        self.assertFalse(derivative.exists())
        self.assertTrue(historical.exists())

    def test_replacement_is_hash_verified_and_idempotent(self):
        source = self.owner / "source.xlsx"
        target = self.owner / "target.xlsx"
        source.write_bytes(b"new-current-content")
        target.write_bytes(b"stale-content")
        action = cleanup.PlannedAction("replace", "test", str(source), str(target))

        result = cleanup.execute_action(action, True, hard_delete)

        self.assertEqual(result.status, "applied")
        self.assertEqual(result.source_sha256_before, result.target_sha256_after)
        self.assertFalse(source.exists())
        self.assertEqual(target.read_bytes(), b"new-current-content")
        second = cleanup.execute_action(action, True, hard_delete)
        self.assertEqual(second.status, "skipped")
        self.assertEqual(second.method, "already_absent")

    def test_second_discovery_has_no_actions(self):
        alias = self.owner.parent / "Financial Summary - July 2026.md"
        alias.write_text("obsolete", encoding="utf-8")
        self.apply_plan()

        second_plan, reviews = cleanup.build_plan(self.root, None, self.global_root)

        self.assertEqual(second_plan, [])
        self.assertEqual(reviews, [])


if __name__ == "__main__":
    unittest.main()
