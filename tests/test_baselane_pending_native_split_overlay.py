import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).absolute().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


plan = load_module("pending_native_split_plan", ROOT / "scripts" / "baselane_native_split_plan.py")
overlay = load_module(
    "pending_native_split_overlay",
    ROOT / "scripts" / "baselane_apply_native_split_ledger_overlay.py",
)


class PendingNativeSplitOverlayTests(unittest.TestCase):
    def test_pending_morgan_linen_is_deferred_and_overlayed_exactly_once(self):
        source = {
            "Account": "Heron-88 Madison Operations",
            "Date": "July 30, 2026",
            "ISODate": "2026-07-30",
            "Merchant": "MORGAN LINEN SERVICES",
            "Description": "",
            "Amount": "-80.26",
            "Type": "Operating Expenses",
            "Category": "Linens, Soaps, & Other Consumables",
            "Property": "88 Madison Ave",
            "Notes": "",
            "BaselaneId": "322955176",
            "Pending": "true",
        }
        properties = {
            "84 Madison Ave": "60548",
            "86 Madison Ave": "63162",
            "88 Madison Ave": "31499",
            "90 Madison Ave": "31525",
        }
        record = plan.build_record(
            source,
            properties,
            {"Cleaning & Maintenance": "6"},
            [
                {
                    "id": "madison_morgan_linen_4_5_6_5",
                    "category": "Cleaning & Maintenance",
                    "match": {
                        "contains_any": ["morgan linen services"],
                        "source_property": "88 Madison Ave",
                    },
                    "targets": [
                        {"property": "84 Madison Ave", "weight": 4},
                        {"property": "86 Madison Ave", "weight": 5},
                        {"property": "88 Madison Ave", "weight": 6},
                        {"property": "90 Madison Ave", "weight": 5},
                    ],
                }
            ],
        )

        self.assertTrue(record["pending"])
        self.assertEqual(
            [split["amount"] for split in record["splits"]],
            ["-16.05", "-20.07", "-24.08", "-20.06"],
        )

        report = {
            "status": "review",
            "records": [record],
            "source_errors": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            apply_report = Path(tmp) / "apply.json"
            apply_report.write_text(
                json.dumps(
                    {
                        "actions": [
                            {
                                "id": record["id"],
                                "status": "deferred_pending",
                                "split_digest": plan.native_split_digest(record),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            reconciled = plan.reconcile_with_apply_report(report, apply_report)

        deferred = reconciled["records"][0]
        self.assertEqual(deferred["status"], "deferred_pending")
        self.assertEqual(deferred["planned_status"], "ready_native_split")
        self.assertEqual(reconciled["deferred_pending_count"], 1)
        self.assertEqual(reconciled["blocked_count"], 0)

        rows, actions = overlay.apply_overlay(
            list(source),
            [source],
            [deferred],
        )
        self.assertEqual(actions[0]["status"], "overlay_applied")
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(overlay.amount(row["Amount"]) for row in rows), overlay.amount("-80.26"))
        self.assertTrue(
            all(
                "source_baselane_id=322955176" in row["Notes"]
                and "source_pending=true" in row["Notes"]
                for row in rows
            )
        )

        unchanged, second_actions = overlay.apply_overlay(list(source), rows, [deferred])
        self.assertEqual(second_actions[0]["status"], "already_overlayed")
        self.assertEqual(unchanged, rows)

    def test_pending_parent_absent_appends_manual_rows_idempotently(self):
        record = {
            "id": "morgan",
            "rule": "madison_morgan_linen_4_5_6_5",
            "status": "deferred_pending",
            "planned_status": "ready_native_split",
            "baselane_id": "322955176",
            "pending": True,
            "date": "July 30, 2026",
            "iso_date": "2026-07-30",
            "account": "Heron-88 Madison Operations",
            "merchant": "MORGAN LINEN SERVICES",
            "description": "",
            "amount": "-80.26",
            "source_property": "88 Madison Ave",
            "source_type": "Operating Expenses",
            "source_category": "Linens, Soaps, & Other Consumables",
            "source_notes": "",
            "category": "Cleaning & Maintenance",
            "splits": [
                {"property": "84 Madison Ave", "amount": "-16.05", "weight": 4, "category": "Cleaning & Maintenance"},
                {"property": "86 Madison Ave", "amount": "-20.07", "weight": 5, "category": "Cleaning & Maintenance"},
                {"property": "88 Madison Ave", "amount": "-24.08", "weight": 6, "category": "Cleaning & Maintenance"},
                {"property": "90 Madison Ave", "amount": "-20.06", "weight": 5, "category": "Cleaning & Maintenance"},
            ],
        }
        fieldnames = [
            "Date", "ISODate", "Merchant", "Description", "Amount", "Type",
            "Category", "Property", "Notes", "BaselaneId", "Pending",
        ]
        rows, actions = overlay.apply_overlay(fieldnames, [], [record])
        self.assertEqual(actions[0]["status"], "pending_manual_rows_applied")
        self.assertEqual(len(rows), 4)
        self.assertEqual(
            sum(overlay.amount(row["Amount"]) for row in rows),
            overlay.amount("-80.26"),
        )
        self.assertTrue(all(row["BaselaneId"] == "322955176" for row in rows))
        self.assertTrue(all(row["Pending"] == "true" for row in rows))

        unchanged, second_actions = overlay.apply_overlay(fieldnames, rows, [record])
        self.assertEqual(second_actions[0]["status"], "already_overlayed")
        self.assertEqual(unchanged, rows)

    def test_live_pending_audit_rows_are_added_to_plan(self):
        audit = {
            "status": "ok",
            "transactions": [
                {
                    "id": "322955176",
                    "date": "2026-07-30",
                    "amount": "-80.26",
                    "pending": True,
                    "account": "Heron-88 Madison Operations",
                    "merchant": "MORGAN LINEN SERVICES",
                    "property_id": "31499",
                    "property": "88 Madison Ave",
                    "tag_id": "62",
                    "category": "Linens, Soaps, & Other Consumables",
                    "note": "",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pending.json"
            path.write_text(json.dumps(audit), encoding="utf-8")
            rows, errors = plan.pending_source_rows(path)
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["BaselaneId"], "322955176")
        self.assertEqual(rows[0]["Pending"], "true")


if __name__ == "__main__":
    unittest.main()
