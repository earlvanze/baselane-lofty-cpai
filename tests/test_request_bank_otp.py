from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "baselane_request_bank_otp.js"


def test_request_bank_otp_uses_guarded_graphql_operation(tmp_path: Path) -> None:
    bridge = tmp_path / "bridge.js"
    capture = tmp_path / "payload.json"
    bridge.write_text(
        """#!/usr/bin/env node
const fs = require('fs');
const input = fs.readFileSync(process.argv[2], 'utf8');
fs.writeFileSync(process.env.CAPTURE_PATH, input);
process.stdout.write(JSON.stringify({data:{unitAPIVerification:true}}));
""",
        encoding="utf-8",
    )
    bridge.chmod(bridge.stat().st_mode | stat.S_IXUSR)

    result = subprocess.run(
        ["node", str(SCRIPT), "127684"],
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "BASELANE_GRAPHQL_BRIDGE": str(bridge),
            "CAPTURE_PATH": str(capture),
        },
    )

    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["status"] == "requested"
    assert report["sensitive_values_exposed"] is False
    payload = json.loads(capture.read_text(encoding="utf-8"))
    assert payload["operationName"] == "getOTP"
    assert payload["variables"] == {"bankId": 127684}


def test_request_bank_otp_rejects_invalid_bank_id() -> None:
    result = subprocess.run(
        ["node", str(SCRIPT), "not-a-bank"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["status"] == "request_failed"
    assert report["bank_id"] is None


def test_request_bank_otp_distinguishes_resend_scope_from_transfer_authority(
    tmp_path: Path,
) -> None:
    bridge = tmp_path / "bridge.js"
    bridge.write_text(
        """#!/usr/bin/env node
process.stderr.write('UNAUTHORIZED_ACCESS: Unauthorized access to bank id');
process.exit(1);
""",
        encoding="utf-8",
    )
    bridge.chmod(bridge.stat().st_mode | stat.S_IXUSR)

    result = subprocess.run(
        ["node", str(SCRIPT), "117744"],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "BASELANE_GRAPHQL_BRIDGE": str(bridge)},
    )

    assert result.returncode == 4
    report = json.loads(result.stdout)
    assert report["status"] == "request_unavailable"
    assert "transfer challenge" in report["detail"]
    assert report["sensitive_values_exposed"] is False
