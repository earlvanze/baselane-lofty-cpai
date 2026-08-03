from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import baselane_eod_telegram_report as telegram  # noqa: E402
import send_monthly_transfer_reconciliation_telegram as sender  # noqa: E402


class TelegramConfigTests(unittest.TestCase):
    def test_missing_token_and_chat_id_return_empty_strings(self) -> None:
        with (
            mock.patch.object(telegram, "read_json", return_value={}),
            mock.patch.object(telegram, "load_env_file", return_value={}),
            mock.patch.object(telegram, "default_telegram_allow_from", return_value=""),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            self.assertEqual(telegram.telegram_config(), ("", ""))


class MonthlyTransferTelegramTests(unittest.TestCase):
    def test_dry_run_does_not_require_telegram_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            message = root / "message.md"
            transfer_report = root / "transfer.json"
            report = root / "send.json"
            message.write_text("Monthly transfer reconciliation\nStatus: review\n", encoding="utf-8")
            transfer_report.write_text(
                json.dumps(
                    {
                        "status": "review",
                        "recommended_send_to_lofty_total": 0,
                        "recommended_send_to_lofty_total_is_final": False,
                        "bank_transfer_actions_final": False,
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(
                sender.telegram,
                "telegram_config",
                side_effect=AssertionError("dry run must not read Telegram credentials"),
            ):
                rc = sender.main(
                    [
                        "--message",
                        str(message),
                        "--transfer-report",
                        str(transfer_report),
                        "--report",
                        str(report),
                        "--dry-run",
                    ]
                )

            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(rc, 0)
            self.assertEqual(payload["status"], "ok_dry_run")
            self.assertFalse(payload["telegram_config_checked"])
            self.assertFalse(payload["telegram_token_present"])
            self.assertFalse(payload["telegram_chat_id_present"])


if __name__ == "__main__":
    unittest.main()
