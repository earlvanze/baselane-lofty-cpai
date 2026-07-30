#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_MIN_FREE_MIB = 2048
DEFAULT_PATH_TIMEOUT_SECONDS = 15.0
KNOWN_USER_RELATIVE_CONSUMERS = (
    Path("AppData/Local/Docker/wsl/disk/docker_data.vhdx"),
    Path("AppData/Local/Temp/cyber-vhdx-compact"),
    Path("AppData/Local/CrashDumps"),
    Path("Dropbox/.dropbox.cache"),
)


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_path_spec(spec: str) -> tuple[str, Path]:
    if "=" in spec:
        name, raw_path = spec.split("=", 1)
        name = name.strip() or "path"
    else:
        name, raw_path = "path", spec
    return name, Path(raw_path).expanduser()


def path_for_usage(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def check_path(name: str, path: Path, min_free_bytes: int) -> dict[str, Any]:
    usage_path = path_for_usage(path)
    usage = shutil.disk_usage(usage_path)
    free_bytes = int(usage.free)
    free_deficit_bytes = max(0, min_free_bytes - free_bytes)
    return {
        "name": name,
        "path": str(path),
        "usage_path": str(usage_path),
        "exists": path.exists(),
        "total_bytes": int(usage.total),
        "used_bytes": int(usage.used),
        "free_bytes": free_bytes,
        "free_mib": round(free_bytes / 1024 / 1024, 1),
        "min_free_bytes": min_free_bytes,
        "min_free_mib": round(min_free_bytes / 1024 / 1024, 1),
        "free_deficit_bytes": free_deficit_bytes,
        "free_deficit_mib": round(free_deficit_bytes / 1024 / 1024, 1),
        "ok": free_bytes >= min_free_bytes,
    }


def check_path_with_timeout(
    name: str,
    path: Path,
    min_free_bytes: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    result: dict[str, Any] = {}

    def worker() -> None:
        try:
            result["status"] = "ok"
            result["value"] = check_path(name, path, min_free_bytes)
        except Exception as exc:  # noqa: BLE001
            result["status"] = "error"
            result["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(max(0.1, timeout_seconds))
    if thread.is_alive():
        return {
            "name": name,
            "path": str(path),
            "usage_path": None,
            "exists": None,
            "total_bytes": None,
            "used_bytes": None,
            "free_bytes": None,
            "free_mib": None,
            "min_free_bytes": min_free_bytes,
            "min_free_mib": round(min_free_bytes / 1024 / 1024, 1),
            "free_deficit_bytes": None,
            "free_deficit_mib": None,
            "ok": False,
            "status": "timeout",
            "error": f"path health check exceeded {timeout_seconds:.1f}s",
        }
    if result.get("status") == "ok":
        return result["value"]
    if result.get("status") != "error":
        return {
            "name": name,
            "path": str(path),
            "usage_path": None,
            "exists": None,
            "total_bytes": None,
            "used_bytes": None,
            "free_bytes": None,
            "free_mib": None,
            "min_free_bytes": min_free_bytes,
            "min_free_mib": round(min_free_bytes / 1024 / 1024, 1),
            "free_deficit_bytes": None,
            "free_deficit_mib": None,
            "ok": False,
            "status": "error",
            "error": "path health check exited without a result",
        }
    return {
        "name": name,
        "path": str(path),
        "usage_path": None,
        "exists": None,
        "total_bytes": None,
        "used_bytes": None,
        "free_bytes": None,
        "free_mib": None,
        "min_free_bytes": min_free_bytes,
        "min_free_mib": round(min_free_bytes / 1024 / 1024, 1),
        "free_deficit_bytes": None,
        "free_deficit_mib": None,
        "ok": False,
        "status": "error",
        "error": result.get("error") or "path health check failed",
    }


def find_user_root(path: Path) -> Path | None:
    parts = path.resolve().parts if path.exists() else path.absolute().parts
    for index, part in enumerate(parts[:-1]):
        if part.lower() == "users" and index + 1 < len(parts):
            return Path(*parts[: index + 2])
    return None


def known_consumer_hint_paths(checks: list[dict[str, Any]]) -> list[Path]:
    roots: list[Path] = []
    for check in checks:
        user_root = find_user_root(Path(str(check.get("usage_path") or check.get("path") or "")))
        if user_root and user_root not in roots:
            roots.append(user_root)
    candidates: list[Path] = []
    for root in roots:
        for relative in KNOWN_USER_RELATIVE_CONSUMERS:
            candidate = root / relative
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def cheap_path_size_bytes(path: Path) -> int | None:
    try:
        if path.is_file():
            return path.stat().st_size
        if path.is_dir():
            return sum(child.stat().st_size for child in path.iterdir() if child.is_file())
    except OSError:
        return None
    return None


def disk_pressure_hints(checks: list[dict[str, Any]]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for candidate in known_consumer_hint_paths(checks):
        size_bytes = cheap_path_size_bytes(candidate)
        if not size_bytes:
            continue
        try:
            usage = shutil.disk_usage(path_for_usage(candidate))
        except OSError:
            same_volume_as_failed_check = False
        else:
            same_volume_as_failed_check = any(
                int(check.get("total_bytes") or 0) == int(usage.total)
                for check in checks
                if not check.get("ok")
            )
        records.append(
            {
                "path": str(candidate),
                "size_bytes": int(size_bytes),
                "size_mib": round(size_bytes / 1024 / 1024, 1),
                "size_gib": round(size_bytes / 1024 / 1024 / 1024, 2),
                "same_volume_as_failed_check": same_volume_as_failed_check,
            }
        )
    records.sort(key=lambda item: int(item["size_bytes"]), reverse=True)
    same_volume = [record for record in records if record["same_volume_as_failed_check"]]
    top_hint = same_volume[0] if same_volume else (records[0] if records else None)
    return {
        "known_large_consumers": records,
        "top_known_large_consumer": top_hint,
        "hint_scope": "cheap_known_paths_only",
    }


def build_report(
    paths: list[tuple[str, Path]],
    min_free_mib: int,
    path_timeout_seconds: float = DEFAULT_PATH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    min_free_bytes = int(min_free_mib) * 1024 * 1024
    checks = [
        check_path_with_timeout(name, path, min_free_bytes, path_timeout_seconds)
        for name, path in paths
    ]
    failing = [check for check in checks if not check["ok"]]
    timed_out = [check for check in failing if check.get("status") == "timeout"]
    errored = [check for check in failing if check.get("status") == "error"]
    low_space = [check for check in failing if check.get("free_deficit_mib") is not None]
    required_free_mib = max((float(check["free_deficit_mib"]) for check in low_space), default=0.0)
    windows_path_probe = any(str(path).startswith("/mnt/c/") for _, path in paths)
    if timed_out or errored:
        hints = {
            "known_large_consumers": [],
            "top_known_large_consumer": None,
            "hint_scope": "skipped_due_to_path_health_failure",
        }
    elif windows_path_probe:
        hints = {
            "known_large_consumers": [],
            "top_known_large_consumer": None,
            "hint_scope": "skipped_for_bounded_windows_path_probe",
        }
    else:
        hints = disk_pressure_hints(checks)
    top_hint = hints.get("top_known_large_consumer") if failing else None
    next_action = "No disk action required."
    if timed_out or errored:
        next_action = (
            "Verify the Windows/Dropbox mount before Baselane downloads; a path health check timed out or failed."
        )
    elif failing:
        next_action = (
            f"Free local Dropbox/Windows disk space: free at least {required_free_mib:.1f} MiB "
            "before Baselane downloads or ledger writes."
        )
        if isinstance(top_hint, dict):
            next_action = (
                f"{next_action} Largest known same-volume consumer: "
                f"{top_hint.get('path')} ({top_hint.get('size_gib')} GiB)."
            )
    return {
        "generated_at": iso_z(),
        "status": "ok" if not failing else "review",
        "issue_count": len(failing),
        "min_free_mib": min_free_mib,
        "required_free_mib": required_free_mib,
        "checks": checks,
        "disk_pressure_hints": hints,
        "issues": [
            f"low_free_space:{check['name']}:{check['free_mib']}MiB<{check['min_free_mib']}MiB"
            for check in low_space
        ] + [
            f"path_health_{check.get('status')}:{check['name']}:{check.get('error')}"
            for check in timed_out + errored
        ],
        "next_action": next_action,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Baselane filesystem free-space preflight.")
    parser.add_argument("--path", action="append", default=[], help="Path or name=path to check.")
    parser.add_argument("--min-free-mib", type=int, default=int(os.environ.get("BASELANE_MIN_FREE_MIB", DEFAULT_MIN_FREE_MIB)))
    parser.add_argument(
        "--path-timeout-seconds",
        type=float,
        default=float(os.environ.get("BASELANE_DISK_PREFLIGHT_PATH_TIMEOUT_SECONDS", DEFAULT_PATH_TIMEOUT_SECONDS)),
    )
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    paths = [parse_path_spec(spec) for spec in args.path]
    if not paths:
        paths = [("workspace", Path.cwd())]
    report = build_report(paths, args.min_free_mib, args.path_timeout_seconds)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.report.with_suffix(args.report.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(args.report)
    print(json.dumps({"status": report["status"], "issue_count": report["issue_count"], "min_free_mib": report["min_free_mib"]}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
