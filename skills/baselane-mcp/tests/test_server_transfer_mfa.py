from __future__ import annotations

import json
from unittest.mock import patch

from baselane_mcp import server


def test_fold7_mfa_runner_binds_parent_bank_and_transfer_timestamp() -> None:
    completed = type(
        "Completed",
        (),
        {
            "returncode": 0,
            "stdout": json.dumps(
                {
                    "status": "verified",
                    "bank_id": 117744,
                    "device_model": "SM-F966U1",
                    "otp_source": "fold7_messages_over_wireless_adb",
                    "sensitive_values_exposed": False,
                }
            ),
            "stderr": "",
        },
    )()

    with patch("baselane_mcp.server.subprocess.run", return_value=completed) as run:
        report = server._complete_fold7_mfa(117744, 90, 1785700000000)

    command = run.call_args.args[0]
    assert command[command.index("--bank-id") + 1] == "117744"
    assert command[command.index("--not-before-ms") + 1] == "1785700000000"
    assert report["status"] == "verified"
    assert report["sensitive_values_exposed"] is False
    assert "otp" not in report


def test_fold7_mfa_runner_does_not_echo_unreadable_output() -> None:
    completed = type(
        "Completed",
        (),
        {"returncode": 1, "stdout": "secret-looking-unreadable-output", "stderr": ""},
    )()

    with patch("baselane_mcp.server.subprocess.run", return_value=completed):
        report = server._complete_fold7_mfa(117744, 90, 1785700000000)

    assert report["status"] == "mfa_helper_error"
    assert "secret-looking" not in json.dumps(report)
    assert report["sensitive_values_exposed"] is False
