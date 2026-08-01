import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import lofty_monthly_owner_email_packet as owner_email


def packet(property_id: str, property_name: str) -> dict:
    return {
        "property_ids": [property_id],
        "source_properties": [
            {
                "property_id": property_id,
                "property_name": property_name,
            }
        ],
    }


class OwnerEmailCooldownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

    def state_for(self, target: dict, sent_at: datetime) -> dict:
        key = owner_email.packet_property_send_keys(target)[0]
        return {"property_sent_at": {key: sent_at.isoformat()}}

    def test_property_is_held_until_seven_full_days_have_elapsed(self) -> None:
        target = packet("101", "101 Main St")
        state = self.state_for(target, self.now - timedelta(days=7) + timedelta(seconds=1))

        issues = owner_email.property_cooldown_issues([target], state, self.now)

        self.assertEqual(len(issues), 1)
        self.assertTrue(issues[0].startswith("property_email_cooldown_active:"))

    def test_property_is_released_at_exactly_seven_days(self) -> None:
        target = packet("101", "101 Main St")
        state = self.state_for(target, self.now - timedelta(days=7))

        self.assertEqual(owner_email.property_cooldown_issues([target], state, self.now), [])

    def test_cooldown_is_scoped_to_each_property(self) -> None:
        held = packet("101", "101 Main St")
        eligible = packet("202", "202 Main St")
        state = self.state_for(held, self.now - timedelta(days=1))

        issues, hold_keys = owner_email.property_cooldown_hold_keys(
            [held, eligible], state, self.now
        )

        self.assertEqual(len(issues), 1)
        self.assertEqual(hold_keys, set(owner_email.packet_property_send_keys(held)))
        self.assertTrue(hold_keys.isdisjoint(owner_email.packet_property_send_keys(eligible)))


if __name__ == "__main__":
    unittest.main()
