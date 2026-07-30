import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import baselane_full_property_coverage_apply as coverage  # noqa: E402


def transaction(row_id: str, *, property_id: str = "", tag_id: str = "9"):
    return {
        "id": row_id,
        "amount": -10,
        "date": "2026-07-01",
        "merchantName": "Vendor",
        "description": "Maintenance",
        "name": "",
        "pending": False,
        "propertyId": property_id,
        "tagId": tag_id,
        "isSplit": False,
        "parentId": None,
        "hidden": False,
        "isDeleted": False,
    }


class CoverageGraphqlTests(unittest.TestCase):
    def test_metadata_uses_one_batched_bridge_call(self):
        payloads = [
            {"data": {"property": [{"id": "1", "name": "One"}]}},
            {
                "data": {
                    "tag": [
                        {
                            "subType": [
                                {"id": "9", "name": "Repairs", "subType": []}
                            ]
                        }
                    ]
                }
            },
        ]
        with patch.object(coverage, "graphql_batch", return_value=payloads) as batch:
            property_ids, tag_ids, property_names = coverage.fetch_metadata()

        self.assertEqual(batch.call_count, 1)
        self.assertEqual(property_ids["ONE"], ["1"])
        self.assertEqual(tag_ids["REPAIRS"], ["9"])
        self.assertEqual(property_names["1"], "One")

    def test_transaction_pages_are_batched_after_first_page(self):
        first = {
            "data": {
                "transactions": {
                    "total": 5,
                    "data": [transaction("5"), transaction("4")],
                }
            }
        }
        remaining = [
            {
                "data": {
                    "transactions": {
                        "total": 5,
                        "data": [transaction("3"), transaction("2")],
                    }
                }
            },
            {
                "data": {
                    "transactions": {"total": 5, "data": [transaction("1")]}
                }
            },
        ]
        with (
            patch.object(coverage, "graphql", return_value=first) as graphql,
            patch.object(
                coverage, "graphql_batch", return_value=remaining
            ) as graphql_batch,
        ):
            rows, total = coverage.fetch_all_transactions(
                page_limit=2, operation_batch_size=10
            )

        self.assertEqual(total, 5)
        self.assertEqual([row["id"] for row in rows], ["5", "4", "3", "2", "1"])
        self.assertEqual(graphql.call_count, 1)
        self.assertEqual(
            [
                operation["variables"]["input"]["page"]
                for operation in graphql_batch.call_args.args[0]
            ],
            [2, 3],
        )

    def test_execute_uses_exact_id_precondition_and_readback(self):
        before = transaction("7")
        after = transaction("7", property_id="101", tag_id="202")
        record = {
            "baselane_id": "7",
            "amount": "-10",
            "date": "2026-07-01",
            "merchant": "Vendor",
            "description": "Maintenance",
            "current_property_id": "",
            "current_tag_id": "9",
            "parent_id": "",
            "target_property_id": "101",
            "target_tag_id": "202",
            "apply_status": "ready",
            "apply_reason": "",
        }
        with (
            patch.object(
                coverage,
                "verify_by_ids",
                side_effect=[{"7": before}, {"7": after}],
            ) as verify,
            patch.object(
                coverage,
                "update_batch",
                return_value=[{"id": "7", "propertyId": "101", "tagId": "202"}],
            ) as update,
            patch.object(
                coverage,
                "fetch_all_transactions",
                side_effect=AssertionError("full readback must not run"),
            ),
        ):
            applied, failed = coverage.execute([record], batch_size=25)

        self.assertEqual((applied, failed), (1, 0))
        self.assertEqual(record["apply_status"], "applied_verified")
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(update.call_count, 1)


if __name__ == "__main__":
    unittest.main()
