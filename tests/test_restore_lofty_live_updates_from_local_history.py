from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.restore_lofty_live_updates_from_local_history import (
    baseline_allows_approved_pending_date,
    build_payloads,
)


class BaselinePendingDateTest(unittest.TestCase):
    def test_accepts_only_missing_entry_for_approved_pending_date(self) -> None:
        row = {
            "containment_ok": False,
            "full_history_containment": {
                "status": "missing_history_entries",
                "missing_entry_count": 1,
                "missing_entries": [{"date": "2026-07-31", "sha256": "a" * 64}],
            },
        }

        self.assertTrue(baseline_allows_approved_pending_date(row, "2026-07-31"))

    def test_rejects_missing_historical_date(self) -> None:
        row = {
            "containment_ok": False,
            "full_history_containment": {
                "status": "missing_history_entries",
                "missing_entry_count": 2,
                "missing_entries": [
                    {"date": "2026-07-31"},
                    {"date": "2026-07-14"},
                ],
            },
        }

        self.assertFalse(baseline_allows_approved_pending_date(row, "2026-07-31"))

    def test_live_authoritative_mode_accepts_local_only_historical_dates(self) -> None:
        row = {
            "containment_ok": False,
            "full_history_containment": {
                "status": "missing_history_entries",
                "missing_entry_count": 2,
                "missing_entries": [
                    {"date": "2026-07-31"},
                    {"date": "2026-07-14"},
                ],
            },
        }

        self.assertTrue(
            baseline_allows_approved_pending_date(
                row,
                "2026-07-31",
                allow_discard_local_only_history=True,
            )
        )

    def test_live_authoritative_mode_accepts_bounded_missing_entry_samples(self) -> None:
        row = {
            "containment_ok": False,
            "full_history_containment": {
                "status": "missing_history_entries",
                "missing_entry_count": 20,
                "missing_entries": [{"date": "2026-07-15"}] * 10,
            },
        }

        self.assertTrue(
            baseline_allows_approved_pending_date(
                row,
                "2026-07-31",
                allow_discard_local_only_history=True,
            )
        )

    def test_accepts_clean_containment(self) -> None:
        self.assertTrue(baseline_allows_approved_pending_date({"containment_ok": True}, "2026-07-31"))


class RecoveryCandidateSelectionTest(unittest.TestCase):
    def recovery_args(
        self,
        root: Path,
        *,
        updates_md: Path,
        snapshot: Path,
        approved_entry: Path | None,
    ) -> argparse.Namespace:
        baseline_report = root / "baseline.json"
        baseline_report.write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "lofty_property_id": "property-1",
                            "property_name": "1278 E 187th St",
                            "live_snapshot_path": str(snapshot),
                            "containment_ok": True,
                            "live_marker_count": 1,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        update_record = {
            "status": "no_manifest_approved_update_candidate",
            "approval_manifest_required": True,
        }
        if approved_entry is not None:
            update_record = {
                "status": "approved_entry_read_error",
                "approved_entry": str(approved_entry),
            }
        guarded_report = root / "guarded.json"
        guarded_report.write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "updates_md": str(updates_md),
                            "updates": update_record,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return argparse.Namespace(
            runtime_map=[],
            baseline_containment_report=baseline_report,
            guarded_apply_report=guarded_report,
            manager_properties=None,
            manual_excluded_property=[],
            property_id=[],
            allow_discard_local_only_history=False,
        )

    @staticmethod
    def containment_module(updates_md: Path) -> SimpleNamespace:
        return SimpleNamespace(
            merge_runtime_map_records=lambda _: [
                {
                    "lofty_property_id": "property-1",
                    "property_name": "1278 E 187th St",
                    "updates_md": str(updates_md),
                }
            ],
            DEFAULT_MANUAL_EXCLUDED_PROPERTIES=[],
            property_excluded=lambda *_: False,
            resolve_updates_md=lambda _: (updates_md, "input"),
        )

    def test_skips_baseline_property_without_manifest_approved_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updates_md = root / "UPDATES.md"
            updates_md.write_text("- Property Update (07/31/2026): Current.\n", encoding="utf-8")
            snapshot = root / "live.txt"
            snapshot.write_text("- Property Update (07/31/2026): Current.\n", encoding="utf-8")
            args = self.recovery_args(
                root,
                updates_md=updates_md,
                snapshot=snapshot,
                approved_entry=None,
            )

            with patch(
                "scripts.restore_lofty_live_updates_from_local_history.load_module",
                side_effect=[self.containment_module(updates_md), SimpleNamespace()],
            ):
                self.assertEqual(build_payloads(args), [])

    def test_missing_manifest_approved_candidate_still_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updates_md = root / "UPDATES.md"
            updates_md.write_text("- Property Update (07/31/2026): Current.\n", encoding="utf-8")
            snapshot = root / "live.txt"
            snapshot.write_text("- Property Update (07/31/2026): Current.\n", encoding="utf-8")
            missing_approved_entry = root / "approved.md"
            args = self.recovery_args(
                root,
                updates_md=updates_md,
                snapshot=snapshot,
                approved_entry=missing_approved_entry,
            )

            with patch(
                "scripts.restore_lofty_live_updates_from_local_history.load_module",
                side_effect=[self.containment_module(updates_md), SimpleNamespace()],
            ):
                with self.assertRaisesRegex(SystemExit, "approved update candidate is missing"):
                    build_payloads(args)


if __name__ == "__main__":
    unittest.main()
