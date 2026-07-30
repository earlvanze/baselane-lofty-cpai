#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


GUARD_TIMEOUT_SECONDS = 30
SAFE_MONTHLY_CRON_DRY_RUN_COMMAND = (
    "DRY_RUN=1 SEND_OWNER_EMAILS=0 PUBLISH_LOFTY_PM_UPDATES=0 "
    "RUN_LOFTY_GUARDED_APPLY=1 APPLY_LOFTY_GUARDED_UPDATES=0 bash scripts/baselane_financials_monthly_cron.sh"
)


def normalize_index_status(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        return ""
    tokens = {token for token in text.split("_") if token}
    if "delisted" in tokens:
        return "skipped_delisted" if text.startswith("skipped") else "delisted"
    if "sold" in tokens:
        return "skipped_sold" if text.startswith("skipped") else "sold"
    if "closed" in tokens:
        return "skipped_closed" if text.startswith("skipped") else "closed"
    return text


def is_active_index_status(value: object) -> bool:
    return normalize_index_status(value) in {"created", "existing", "would_create"}


def first_error_line(stderr: object) -> str | None:
    text = str(stderr or "").strip()
    if not text:
        return None
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first_line[:300] or None


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def fallback_normalize(value: object) -> str:
    text = str(value).lower()
    text = re.sub(r"\blfty\d+\b", " ", text)
    text = text.replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\bavenue\b", "ave", text)
    text = re.sub(r"\bstreet\b", "st", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def load_source_held_guards(transfer_reconciliation_file: Path | None, normalize_exclusion) -> list[dict[str, Any]]:
    if not transfer_reconciliation_file or not transfer_reconciliation_file.is_file():
        return []
    transfer = read_json(transfer_reconciliation_file)
    guards: list[dict[str, Any]] = []
    for detail in transfer.get("property_cash_review_details") or []:
        if not isinstance(detail, dict):
            continue
        property_name = str(detail.get("property") or detail.get("property_name") or "").strip()
        if not property_name:
            continue
        guards.append(
            {
                "source": "property_cash_review",
                "property_name": property_name,
                "normalized_property": normalize_exclusion(property_name),
                "exclude_reason": (
                    "property source-cash/cash-alignment review is held; suppress live update/email guard "
                    "until source-clean"
                ),
                "source_clean_status": detail.get("source_clean_status"),
                "review_rows_remaining": detail.get("property_cash_review_unreviewed_group_count"),
                "high_priority_unresolved_sum": detail.get("property_cash_review_high_priority_unresolved_sum"),
            }
        )
    return guards


def load_current_only_verified(root: Path) -> dict[str, Any]:
    path = root / "reports" / "lofty_listing_update_cleanup_queue.local-live-verify.json"
    payload = read_json(path)
    if payload.get("status") != "ok" or int(payload.get("issue_count") or 0):
        return {}
    records = payload.get("records") if isinstance(payload.get("records"), list) else []
    verified: dict[str, Any] = {}
    for record in records:
        if not isinstance(record, dict) or record.get("ok") is not True:
            return {}
        property_id = str(record.get("lofty_property_id") or "").strip()
        if not property_id or record.get("financial_summary_verified") is not True:
            return {}
        verified[property_id] = record
    if int(payload.get("target_count") or 0) != len(verified) or int(payload.get("ok_count") or 0) != len(verified):
        return {}
    return verified


def build_audit(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root
    sys.path.insert(0, str(root / "scripts"))
    from lofty_monthly_exclusions import (  # pylint: disable=import-outside-toplevel
        DEFAULT_MANUAL_EXCLUDED_PROPERTIES,
        match_exclusion_guard,
        monthly_exclusion_guards,
    )
    from lofty_property_paths import public_dir_for_property, resolve_property_path  # pylint: disable=import-outside-toplevel

    try:
        from lofty_monthly_exclusions import normalize as normalize_exclusion  # pylint: disable=import-outside-toplevel
    except ImportError:
        normalize_exclusion = fallback_normalize

    issues: list[str] = []
    records: list[dict[str, Any]] = []
    externally_excluded_records: list[dict[str, Any]] = []
    source_held_records: list[dict[str, Any]] = []
    yhome_guard: dict[str, Any] = {
        "status": "not_checked",
        "path": str(args.yhome_transition_csv) if args.yhome_transition_csv else None,
        "excluded_count": 0,
    }
    manual_exclusions: list[dict[str, Any]] = []

    def append_guard_issue(kind: str, path: Path, check: dict[str, Any], issue_text: str | None = None) -> None:
        error = first_error_line(check.get("stderr") if isinstance(check, dict) else None)
        detail = f"{kind} guard failed: {path}"
        if issue_text:
            detail = issue_text
        if error:
            detail = f"{detail} :: {error}"
        issues.append(detail)

    if not args.index_csv.is_file():
        issues.append(f"monthly index missing: {args.index_csv}")
    else:
        current_only_verified = load_current_only_verified(root)
        live_update_capture = read_json(root / "reports" / "baselane_financials_monthly_live_update_capture.json")
        live_update_current_only_ids: set[str] = set()
        live_update_current_only_by_updates_md: dict[str, str] = {}
        for live_record in live_update_capture.get("records") or []:
            if isinstance(live_record, dict) and live_record.get("status") == "guard_ok_current_only":
                property_id = str(live_record.get("lofty_property_id") or "")
                live_update_current_only_ids.add(property_id)
                updates_md_value = str(live_record.get("updates_md") or "")
                if property_id and updates_md_value:
                    live_update_current_only_by_updates_md[updates_md_value] = property_id

        exclusion_guards, yhome_guard, manual_exclusions = monthly_exclusion_guards(
            args.yhome_transition_csv,
            DEFAULT_MANUAL_EXCLUDED_PROPERTIES,
        )
        source_held_guards = load_source_held_guards(args.transfer_reconciliation, normalize_exclusion)
        with args.index_csv.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        for prop in rows:
            if not is_active_index_status(prop.get("status")):
                continue
            property_path_value = prop.get("property_path") or ""
            if not property_path_value:
                continue
            property_path, path_resolution = resolve_property_path(Path(property_path_value))
            exclusion = match_exclusion_guard(property_path, exclusion_guards)
            if exclusion:
                externally_excluded_records.append(
                    {
                        "status": "excluded_no_live_update_or_email",
                        "property_name": property_path.name,
                        "property_path": str(property_path),
                        "exclude_source": exclusion.get("source"),
                        "exclude_reason": exclusion.get("exclude_reason"),
                        "matched_exclusion_property": exclusion.get("property_name"),
                        "yhome_column_b": exclusion.get("yhome_column_b"),
                        **path_resolution,
                    }
                )
                continue
            source_hold = match_exclusion_guard(property_path, source_held_guards)
            if source_hold:
                source_held_records.append(
                    {
                        "status": "held_source_cash_review",
                        "property_name": property_path.name,
                        "property_path": str(property_path),
                        "hold_source": source_hold.get("source"),
                        "hold_reason": source_hold.get("exclude_reason"),
                        "matched_hold_property": source_hold.get("property_name"),
                        "source_clean_status": source_hold.get("source_clean_status"),
                        "review_rows_remaining": source_hold.get("review_rows_remaining"),
                        "high_priority_unresolved_sum": source_hold.get("high_priority_unresolved_sum"),
                        **path_resolution,
                    }
                )
                continue
            public_dir = public_dir_for_property(property_path)
            updates_path = public_dir / "00 - README & Property Snapshot" / "UPDATES.md"
            financials_path = public_dir / "00 - README & Property Snapshot" / "FINANCIALS.md"
            record = {
                "property_name": property_path.name,
                "property_path": str(property_path),
                "updates_md": str(updates_path),
                "financials_md": str(financials_path),
                "checks": {},
                **path_resolution,
            }
            if not updates_path.is_file():
                record["checks"]["updates"] = {"status": "missing"}
                issues.append(f"UPDATES.md missing: {updates_path}")
            elif not args.updates_guard or not args.updates_guard.is_file():
                record["checks"]["updates"] = {"status": "guard_missing"}
                issues.append(f"updates guard missing: {args.updates_guard}")
            else:
                try:
                    result = subprocess.run(
                        [sys.executable, str(args.updates_guard), "check", str(updates_path)],
                        capture_output=True,
                        text=True,
                        timeout=GUARD_TIMEOUT_SECONDS,
                        check=False,
                    )
                    record["checks"]["updates"] = {
                        "status": "ok" if result.returncode == 0 else "failed",
                        "return_code": result.returncode,
                        "stderr": result.stderr[-1000:],
                    }
                except subprocess.TimeoutExpired:
                    result = None
                    record["checks"]["updates"] = {
                        "status": "failed",
                        "return_code": 124,
                        "stderr": f"guard command timed out after {GUARD_TIMEOUT_SECONDS}s",
                        "timed_out": True,
                    }
                if result is None:
                    append_guard_issue("UPDATES", updates_path, record["checks"]["updates"], f"UPDATES guard timed out: {updates_path}")
                elif result.returncode != 0:
                    property_id = live_update_current_only_by_updates_md.get(str(updates_path), "")
                    if not property_id and live_update_current_only_ids:
                        for item in live_update_current_only_ids:
                            if item in current_only_verified:
                                verified_name = str(current_only_verified[item].get("property_name") or "")
                                if verified_name and (
                                    verified_name == property_path.name
                                    or verified_name in property_path.name
                                    or property_path.name in verified_name
                                ):
                                    property_id = item
                                    break
                    if property_id and property_id in current_only_verified:
                        record["checks"]["updates"] = {
                            **record["checks"]["updates"],
                            "status": "ok",
                            "guard_mode": "current_only_live_listing_verified",
                            "live_update_capture": "guard_ok_current_only",
                            "current_only_listing_verify": current_only_verified[property_id],
                        }
                    else:
                        append_guard_issue("UPDATES", updates_path, record["checks"]["updates"])
            if not financials_path.is_file():
                record["checks"]["financials"] = {"status": "missing"}
                issues.append(f"FINANCIALS.md missing: {financials_path}")
            else:
                record["checks"]["financials"] = {
                    "status": "ok",
                    "guard_mode": "local_document_exists",
                    "live_field_guard": "baselane_financials_monthly_live_financial_capture",
                    "note": (
                        "FINANCIALS.md is local Dropbox documentation; Lofty live financial field drift is guarded "
                        "by the live financial capture report, not by requiring local markdown to equal Lofty sparse "
                        "live fields."
                    ),
                }
            records.append(record)

    guard_failures: list[dict[str, Any]] = []
    for record in records:
        for section, label in (("updates", "UPDATES"), ("financials", "FINANCIALS")):
            check = record.get("checks", {}).get(section, {})
            if check.get("status") != "failed":
                continue
            target_key = "updates_md" if section == "updates" else "financials_md"
            guard_failures.append(
                {
                    "property_name": record.get("property_name"),
                    "property_path": record.get("property_path"),
                    "target": label,
                    "target_path": record.get(target_key),
                    "return_code": check.get("return_code"),
                    "timed_out": bool(check.get("timed_out")),
                    "first_error": first_error_line(check.get("stderr")),
                    "next_action": (
                        f"Review the local {label}.md guard stderr for this property; if Lofty live evidence is stale, "
                        f"rerun `{SAFE_MONTHLY_CRON_DRY_RUN_COMMAND}` with an authenticated Lofty tab. "
                        "Do not enable owner email or guarded apply until this audit is ok."
                    ),
                }
            )

    return {
        "status": "ok" if not issues else "review",
        "issue_count": len(issues),
        "issues": issues[:50],
        "guard_failure_count": len(guard_failures),
        "guard_failures": guard_failures[:50],
        "property_count": len(records),
        "externally_excluded_count": len(externally_excluded_records),
        "externally_excluded_records": externally_excluded_records,
        "source_held_count": len(source_held_records),
        "source_held_records": source_held_records,
        "excluded_property_names": [record["property_name"] for record in externally_excluded_records],
        "yhome_transition_guard": yhome_guard,
        "manual_excluded_property_names": [record["property_name"] for record in manual_exclusions],
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit monthly Lofty local guards without running the full monthly cron.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--index-csv", type=Path, required=True)
    parser.add_argument("--updates-guard", type=Path, required=True)
    parser.add_argument("--live-guard", type=Path)
    parser.add_argument("--yhome-transition-csv", type=Path)
    parser.add_argument("--transfer-reconciliation", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    args.root = args.root.resolve()
    audit = build_audit(args)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: audit.get(key) for key in ("status", "issue_count", "property_count", "source_held_count")}, indent=2))
    return 0 if audit["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
