#!/usr/bin/env python3
"""Retrieve a fresh Baselane SMS from an authorized Fold 7 and submit it.

The OTP is held only in this process and the child handler environment. It is
never written to disk, included in command arguments, or printed.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


OTP_PATTERN = re.compile(r"(?<!\d)(\d{6})(?!\d)")
DATE_PATTERN = re.compile(r"(?:^|,\s*)date=(\d+)(?:,|$)")
ID_PATTERN = re.compile(r"\b_id=(\d+)(?:,|$)")
BODY_PATTERN = re.compile(r"(?:^|,\s*)body=(.*)$", re.DOTALL)
MODEL_PATTERN = re.compile(r"^SM[-_]F966", re.IGNORECASE)
ADB_TARGET_PATTERN = re.compile(
    r"(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\]):[1-9]\d{0,4}"
)


class PipelineError(RuntimeError):
    def __init__(self, status: str, stage: str, detail: str, exit_code: int = 2):
        super().__init__(detail)
        self.status = status
        self.stage = stage
        self.detail = detail
        self.exit_code = exit_code


@dataclass(frozen=True)
class SmsCandidate:
    message_id: str
    received_ms: int
    otp: str

    @property
    def reuse_key(self) -> str:
        return f"{self.message_id}:{self.received_ms}"


def emit(status: str, **fields: object) -> None:
    payload = {
        "status": status,
        **fields,
        "sensitive_values_exposed": False,
    }
    print(json.dumps(payload, sort_keys=True))


def run_command(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise PipelineError(
            "command_timeout", "preflight", f"{Path(command[0]).name} timed out"
        ) from error
    except OSError as error:
        raise PipelineError(
            "command_failed", "preflight", f"{Path(command[0]).name} could not run"
        ) from error


def parse_sms_candidates(raw: str, minimum_received_ms: int) -> list[SmsCandidate]:
    candidates: list[SmsCandidate] = []
    for row in raw.replace("\r", "").splitlines():
        if "baselane" not in row.lower():
            continue
        date_match = DATE_PATTERN.search(row)
        body_match = BODY_PATTERN.search(row)
        if not date_match or not body_match:
            continue
        received_ms = int(date_match.group(1))
        if received_ms < minimum_received_ms:
            continue
        otp_match = OTP_PATTERN.search(body_match.group(1))
        if not otp_match:
            continue
        id_match = ID_PATTERN.search(row)
        if not id_match:
            continue
        candidates.append(
            SmsCandidate(
                message_id=id_match.group(1),
                received_ms=received_ms,
                otp=otp_match.group(1),
            )
        )
    return sorted(candidates, key=lambda item: item.received_ms, reverse=True)


def read_used_keys(state_path: Path) -> set[str]:
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise PipelineError(
            "invalid_state", "state", "OTP reuse state is unreadable"
        ) from error
    keys = payload.get("used_message_keys", [])
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise PipelineError("invalid_state", "state", "OTP reuse state is invalid")
    return set(keys)


def record_used_key(state_path: Path, used_keys: set[str], key: str) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    retained = [*sorted(used_keys - {key})[-99:], key]
    temporary = state_path.with_suffix(f"{state_path.suffix}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "used_message_keys": retained}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, state_path)
        os.chmod(state_path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def query_sms(adb: str, target: str) -> str:
    remote_command = (
        "content query --uri content://sms/inbox "
        "--projection _id:address:date:body --sort 'date DESC'"
    )
    result = run_command([adb, "-s", target, "shell", remote_command])
    if result.returncode != 0:
        raise PipelineError(
            "sms_query_failed",
            "sms",
            "Messages inbox could not be queried; verify READ_SMS access",
        )
    return result.stdout


def is_tailnet_target(target: str) -> bool:
    if not ADB_TARGET_PATTERN.fullmatch(target):
        return False
    host = target.rsplit(":", 1)[0].strip("[]")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host.lower().endswith(".ts.net")
    return isinstance(address, ipaddress.IPv4Address) and address in ipaddress.ip_network(
        "100.64.0.0/10"
    )


def discover_adb_target(adb: str) -> str:
    """Find exactly one already-authorized Fold 7 on the Tailnet."""
    devices = run_command([adb, "devices", "-l"])
    if devices.returncode != 0:
        raise PipelineError(
            "device_unavailable", "device", "wireless ADB devices could not be listed"
        )
    matches: list[str] = []
    for row in devices.stdout.splitlines()[1:]:
        fields = row.split()
        if len(fields) < 2 or fields[1] != "device":
            continue
        target = fields[0]
        if not is_tailnet_target(target):
            continue
        model_result = run_command(
            [adb, "-s", target, "shell", "getprop ro.product.model"]
        )
        if model_result.returncode == 0 and MODEL_PATTERN.match(
            model_result.stdout.strip()
        ):
            matches.append(target)
    if not matches:
        raise PipelineError(
            "device_unavailable",
            "device",
            "no authorized Fold 7 wireless ADB target is connected on the Tailnet",
        )
    if len(matches) > 1:
        raise PipelineError(
            "ambiguous_device",
            "device",
            "more than one authorized Fold 7 Tailnet target is connected",
        )
    return matches[0]


def verify_device(adb: str, target: str) -> str:
    connect = run_command([adb, "connect", target])
    connect_text = f"{connect.stdout}\n{connect.stderr}".lower()
    if connect.returncode != 0 or (
        "connected" not in connect_text and "already connected" not in connect_text
    ):
        raise PipelineError(
            "device_unavailable", "device", "wireless ADB connection failed"
        )

    state = run_command([adb, "-s", target, "get-state"])
    if state.returncode != 0 or state.stdout.strip() != "device":
        raise PipelineError(
            "device_unavailable", "device", "Fold 7 is not authorized over ADB"
        )

    model_result = run_command(
        [adb, "-s", target, "shell", "getprop ro.product.model"]
    )
    model = model_result.stdout.strip()
    if model_result.returncode != 0 or not MODEL_PATTERN.match(model):
        raise PipelineError(
            "wrong_device", "device", "connected device is not the configured Fold 7"
        )
    return model


def default_state_path() -> Path:
    state_root = Path(
        os.environ.get(
            "XDG_STATE_HOME", str(Path.home() / ".local" / "state")
        )
    )
    return state_root / "baselane-mcp" / "fold7-mfa.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Pull a fresh Baselane SMS from an authorized Fold 7 over wireless "
            "ADB and submit it to the Baselane bank-token MFA handler."
        )
    )
    parser.add_argument(
        "--adb-target",
        default=os.environ.get("FOLD7_ADB_TARGET"),
        help="Fold 7 wireless-debugging host:port (or FOLD7_ADB_TARGET)",
    )
    parser.add_argument("--bank-id", required=True, type=int)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--poll-interval", type=float, default=5)
    parser.add_argument("--max-message-age", type=float, default=600)
    parser.add_argument(
        "--not-before-ms",
        type=int,
        help="Ignore SMS rows received before this transfer-attempt timestamp",
    )
    parser.add_argument("--state-file", type=Path, default=default_state_path())
    parser.add_argument(
        "--adb-bin",
        default=os.environ.get("BASELANE_ADB_BIN") or shutil.which("adb"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--node-bin",
        default=os.environ.get("BASELANE_NODE_BIN") or shutil.which("node"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--handler",
        type=Path,
        default=Path(
            os.environ.get(
                "BASELANE_LOCAL_OTP_HANDLER",
                str(Path(__file__).with_name("baselane_verify_bank_otp.js")),
            )
        ),
        help=argparse.SUPPRESS,
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if not args.adb_bin:
        raise PipelineError("missing_dependency", "preflight", "adb was not found")
    if not args.adb_target:
        args.adb_target = discover_adb_target(args.adb_bin)
    if not is_tailnet_target(args.adb_target):
        raise PipelineError(
            "invalid_arguments",
            "arguments",
            "Fold 7 wireless ADB target must use the Tailnet",
        )
    if not 1 <= args.bank_id <= 2_147_483_647:
        raise PipelineError("invalid_arguments", "arguments", "bank ID is invalid")
    if args.timeout <= 0 or args.poll_interval <= 0 or args.max_message_age <= 0:
        raise PipelineError(
            "invalid_arguments", "arguments", "timeouts must be positive"
        )
    if args.not_before_ms is not None and args.not_before_ms <= 0:
        raise PipelineError(
            "invalid_arguments", "arguments", "not-before timestamp is invalid"
        )
    if not args.node_bin:
        raise PipelineError("missing_dependency", "preflight", "node was not found")
    if not args.handler.is_file():
        raise PipelineError(
            "missing_dependency", "preflight", "local OTP handler was not found"
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_args(args)
        model = verify_device(args.adb_bin, args.adb_target)
        used_keys = read_used_keys(args.state_file)
        started_ms = int(time.time() * 1000)
        minimum_received_ms = started_ms - int(args.max_message_age * 1000)
        if args.not_before_ms is not None:
            minimum_received_ms = max(minimum_received_ms, args.not_before_ms)
        deadline = time.monotonic() + args.timeout
        attempts = 0
        candidate: SmsCandidate | None = None

        while time.monotonic() <= deadline:
            attempts += 1
            candidates = parse_sms_candidates(
                query_sms(args.adb_bin, args.adb_target), minimum_received_ms
            )
            candidate = next(
                (item for item in candidates if item.reuse_key not in used_keys), None
            )
            if candidate:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(args.poll_interval, remaining))

        if not candidate:
            raise PipelineError(
                "no_fresh_code",
                "sms",
                "no fresh, unused Baselane SMS code arrived before timeout",
            )

        handler_env = os.environ.copy()
        handler_env["BASELANE_LOCAL_OTP"] = candidate.otp
        result = run_command(
            [args.node_bin, str(args.handler), str(args.bank_id)],
            env=handler_env,
            timeout=90,
        )
        handler_env.pop("BASELANE_LOCAL_OTP", None)
        if result.returncode != 0:
            raise PipelineError(
                "mfa_submission_failed",
                "baselane",
                "Baselane rejected the code or the authenticated tab was unavailable",
                exit_code=3,
            )

        record_used_key(args.state_file, used_keys, candidate.reuse_key)
        emit(
            "verified",
            bank_id=args.bank_id,
            device_model=model,
            attempts=attempts,
            otp_source="fold7_messages_over_wireless_adb",
        )
        return 0
    except PipelineError as error:
        emit(error.status, stage=error.stage, detail=error.detail)
        return error.exit_code


if __name__ == "__main__":
    sys.exit(main())
