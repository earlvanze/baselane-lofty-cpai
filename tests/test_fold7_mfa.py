from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "baselane_fold7_mfa.py"
)
SPEC = importlib.util.spec_from_file_location("baselane_fold7_mfa", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def make_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_parse_sms_candidates_uses_latest_fresh_baselane_row() -> None:
    now_ms = int(time.time() * 1000)
    raw = "\n".join(
        [
            f"Row: 0 _id=8, address=123, date={now_ms}, body=Baselane code 654321",
            f"Row: 1 _id=7, address=123, date={now_ms - 1}, body=Other code 999999",
            f"Row: 2 _id=6, address=123, date={now_ms - 2}, body=Baselane code 123456",
        ]
    )

    candidates = MODULE.parse_sms_candidates(raw, now_ms - 10_000)

    assert [candidate.message_id for candidate in candidates] == ["8", "6"]
    assert [candidate.otp for candidate in candidates] == ["654321", "123456"]


def test_discovers_exactly_one_authorized_fold7_on_tailnet(tmp_path: Path) -> None:
    adb = tmp_path / "adb"
    make_executable(
        adb,
        """#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  "devices -l")
    printf 'List of devices attached\n100.88.253.107:42509 device product:q7q model:SM_F966U1\n192.168.1.9:5555 device product:q7q model:SM_F966U1\n'
    ;;
  "-s 100.88.253.107:42509 shell getprop ro.product.model") echo "SM_F966U1" ;;
  *) exit 9 ;;
esac
""",
    )

    assert MODULE.discover_adb_target(str(adb)) == "100.88.253.107:42509"


def test_discovery_fails_closed_when_multiple_tailnet_folds_are_connected(
    tmp_path: Path,
) -> None:
    adb = tmp_path / "adb"
    make_executable(
        adb,
        """#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  "devices -l")
    printf 'List of devices attached\n100.88.253.107:42509 device\n100.99.1.2:46607 device\n'
    ;;
  *"getprop ro.product.model") echo "SM-F966U1" ;;
  *) exit 9 ;;
esac
""",
    )

    try:
        MODULE.discover_adb_target(str(adb))
    except MODULE.PipelineError as error:
        assert error.status == "ambiguous_device"
    else:
        raise AssertionError("ambiguous Fold 7 discovery must fail closed")


def test_pipeline_passes_otp_only_in_handler_environment(tmp_path: Path) -> None:
    now_ms = int(time.time() * 1000)
    adb = tmp_path / "adb"
    node = tmp_path / "node"
    handler = tmp_path / "handler.js"
    state = tmp_path / "state.json"
    marker = tmp_path / "handler-result.json"
    handler.write_text("// fake handler\n", encoding="utf-8")

    make_executable(
        adb,
        f"""#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  "connect phone.test.ts.net:46017") echo "connected to phone.test.ts.net:46017" ;;
  *"get-state") echo "device" ;;
  *"getprop ro.product.model") echo "SM_F966U1" ;;
  *"content query"*) echo "Row: 0 _id=42, address=123, date={now_ms}, body=Your Baselane verification code is 246810" ;;
  *) exit 9 ;;
esac
""",
    )
    make_executable(
        node,
        f"""#!/usr/bin/env bash
set -euo pipefail
test "$BASELANE_LOCAL_OTP" = "246810"
test "$2" = "30293"
printf '{{"received":true}}\\n' > "{marker}"
""",
    )

    result = subprocess.run(
        [
            str(SCRIPT),
            "--adb-target",
            "phone.test.ts.net:46017",
            "--bank-id",
            "30293",
            "--adb-bin",
            str(adb),
            "--node-bin",
            str(node),
            "--handler",
            str(handler),
            "--state-file",
            str(state),
            "--timeout",
            "1",
            "--poll-interval",
            "0.01",
        ],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "XDG_STATE_HOME": str(tmp_path / "xdg")},
    )

    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["status"] == "verified"
    assert report["sensitive_values_exposed"] is False
    assert "246810" not in result.stdout
    assert "246810" not in result.stderr
    assert marker.exists()
    assert "246810" not in state.read_text(encoding="utf-8")


def test_pipeline_refuses_reused_message(tmp_path: Path) -> None:
    now_ms = int(time.time() * 1000)
    adb = tmp_path / "adb"
    node = tmp_path / "node"
    handler = tmp_path / "handler.js"
    state = tmp_path / "state.json"
    handler.write_text("// fake handler\n", encoding="utf-8")
    state.write_text(
        json.dumps({"version": 1, "used_message_keys": [f"42:{now_ms}"]}),
        encoding="utf-8",
    )

    make_executable(
        adb,
        f"""#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  "connect phone.test.ts.net:46017") echo "already connected to phone.test.ts.net:46017" ;;
  *"get-state") echo "device" ;;
  *"getprop ro.product.model") echo "SM-F966U1" ;;
  *"content query"*) echo "Row: 0 _id=42, address=123, date={now_ms}, body=Baselane code 246810" ;;
  *) exit 9 ;;
esac
""",
    )
    make_executable(node, "#!/usr/bin/env bash\nexit 99\n")

    result = subprocess.run(
        [
            str(SCRIPT),
            "--adb-target",
            "phone.test.ts.net:46017",
            "--bank-id",
            "30293",
            "--adb-bin",
            str(adb),
            "--node-bin",
            str(node),
            "--handler",
            str(handler),
            "--state-file",
            str(state),
            "--timeout",
            "0.03",
            "--poll-interval",
            "0.01",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["status"] == "no_fresh_code"
    assert report["sensitive_values_exposed"] is False
    assert "246810" not in result.stdout
