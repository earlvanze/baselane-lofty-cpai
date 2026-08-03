import unittest

from scripts import baselane_cleanup_1456_aligned_duplicate_receipts as cleanup


def live_row(target, *, deleted=False):
    return {
        "id": target["id"],
        "amount": float(target["amount"]),
        "date": target["date"],
        "merchantName": target["merchantName"],
        "bankAccountId": None,
        "propertyId": cleanup.PROPERTY_ID,
        "tagId": cleanup.TAG_ID,
        "note": (
            "Aligned clearing detail import | Rent or tenant receipt | post_yhome_transition | "
            f"source={cleanup.SOURCE_BASENAME} {target['source_line']} | "
            "accounting/manual detail only, no ECO bank transfer | "
            f"key={target['key']}"
        ),
        "isManual": True,
        "hidden": False,
        "isDeleted": deleted,
    }


class Baselane1456AlignedDuplicateCleanupTests(unittest.TestCase):
    def test_exact_live_state_is_ready(self):
        active = [live_row(target) for target in cleanup.KEEP_TARGETS + cleanup.DELETE_TARGETS]

        result = cleanup.assess_live_state(active, [])

        self.assertEqual(result["status"], "ready")
        self.assertEqual(
            {row["id"] for row in result["active_delete_targets"]},
            {target["id"] for target in cleanup.DELETE_TARGETS},
        )
        self.assertEqual(result["issues"], [])

    def test_deleted_targets_and_active_source_rows_are_idempotent(self):
        active = [live_row(target) for target in cleanup.KEEP_TARGETS]
        deleted = [live_row(target, deleted=True) for target in cleanup.DELETE_TARGETS]

        result = cleanup.assess_live_state(active, deleted)

        self.assertEqual(result["status"], "already_applied")
        self.assertEqual(result["issues"], [])

    def test_identity_drift_blocks_cleanup(self):
        active = [live_row(target) for target in cleanup.KEEP_TARGETS + cleanup.DELETE_TARGETS]
        active[-1]["amount"] = 999.00

        result = cleanup.assess_live_state(active, [])

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["issues"][0]["failed"], ["amount"])

    def test_payload_digest_is_stable(self):
        self.assertEqual(cleanup.payload_digest(), cleanup.payload_digest())
        self.assertEqual(len(cleanup.payload_digest()), 64)


if __name__ == "__main__":
    unittest.main()
