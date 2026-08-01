from __future__ import annotations

import unittest

from scripts.restore_lofty_live_updates_from_local_history import (
    baseline_allows_approved_pending_date,
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


if __name__ == "__main__":
    unittest.main()
