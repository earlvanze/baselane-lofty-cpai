#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OPENCLAW_SCRIPTS = Path(
    os.environ.get("OPENCLAW_WORKSPACE", Path(__file__).resolve().parents[3])
) / "scripts"
if OPENCLAW_SCRIPTS.is_dir():
    # Keep this repository's monthly modules authoritative; shared OpenClaw
    # scripts are fallback helpers (for example, the Discord route resolver).
    sys.path.append(str(OPENCLAW_SCRIPTS))

import post_property_update_discord as discord_route
from lofty_index_status import is_active_index_status
from lofty_monthly_publish_to_pm import DEFAULT_MANUAL_EXCLUDED_PROPERTIES


DEFAULT_CANDIDATE_PACKET = Path("reports/baselane_financials_monthly_review_candidate_packet.json")
DEFAULT_READINESS = Path("reports/baselane_financials_monthly_readiness.json")
DEFAULT_TRANSFER_RECONCILIATION = Path("reports/baselane_lofty_transfer_requirements.json")
DEFAULT_MONTHLY_RUN_REPORT = Path("reports/baselane_financials_monthly_run_report.json")
DEFAULT_FINANCIAL_PATCH_READINESS = Path("reports/lofty_financial_patch_readiness.json")
DEFAULT_PLAN = Path("reports/baselane_financials_monthly_discord_all_send_plan.json")
DISCORD_LIMIT_BYTES = 2000
FINANCIAL_SUMMARY_MARKERS = ("Financial detail:", "Financial summary from FINANCIALS.md:")
SPENDABLE_CASH_MARKER = "ECO Net DAO Funds (spendable cash held by ECO)"
OBSOLETE_LEDGER_CASH_SNIPPETS = (
    "ECO Operating Cash is the full DAO-attributed Column E sum",
    "ECO General Ledger is the complete DAO-attributed Column E total",
    "ECO GL Column E sum",
)
UPSTREAM_FINANCIAL_BLOCKER_PREFIXES = (
    "data_quality.",
    "operational.daily_sync",
    "operational.daily_run",
    "operational.baselane_sync",
    "operational.monthly_run",
    "operational.source_cash_balance",
    "operational.weekly_cf_review_gate",
    "operational.monthly_bank_statement",
    "operational.first_day_pm_fee",
    "operational.public_path_guard",
    "operational.tenant_ledger_folder_guard",
)
FINANCIAL_DETAIL_MARKER = "Financial detail:"
EMBEDDED_PROPERTY_UPDATE_RE = re.compile(r"(?m)^Property Update:\s+.+$")


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "path": str(path), "error": str(exc)}
    return data if isinstance(data, dict) else {"status": "unreadable", "path": str(path), "error": "not a JSON object"}


def read_text(path_value: object) -> tuple[str, str | None, str | None]:
    path_text = str(path_value or "").strip()
    if not path_text:
        return "", None, "missing_update_candidate"
    path = Path(path_text)
    if not path.is_file():
        return "", str(path), f"update_candidate_missing:{path}"
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip(), str(path), None
    except Exception as exc:  # noqa: BLE001
        return "", str(path), f"update_candidate_unreadable:{path}:{exc}"


def has_financial_summary(text: str) -> bool:
    return (
        any(marker in text for marker in FINANCIAL_SUMMARY_MARKERS)
        and SPENDABLE_CASH_MARKER in text
        and not any(snippet in text for snippet in OBSOLETE_LEDGER_CASH_SNIPPETS)
        and "## Monthly Cash Position (" in text
    )


def dedupe_financial_summary(text: str) -> str:
    marker = next((candidate for candidate in FINANCIAL_SUMMARY_MARKERS if candidate in text), None)
    if marker is None:
        return text
    first = text.find(marker)
    if first < 0:
        return text
    second = text.find(marker, first + len(marker))
    if second < 0:
        return text
    return text[:second].rstrip() + "\n"


def strip_embedded_property_update_history(text: str) -> str:
    financial_index = text.find(FINANCIAL_DETAIL_MARKER)
    if financial_index < 0:
        return text
    before_financial = text[:financial_index]
    after_financial = text[financial_index:]
    matches = list(EMBEDDED_PROPERTY_UPDATE_RE.finditer(before_financial))
    if len(matches) < 2:
        return text
    embedded_match = matches[1]
    return before_financial[: embedded_match.start()].rstrip() + "\n\n" + after_financial.lstrip()


def truncate_utf8(text: str, limit: int) -> str:
    suffix = "\n\n[Full detail remains in FINANCIALS.md.]\n"
    suffix_bytes = len(suffix.encode("utf-8"))
    if suffix_bytes >= limit:
        return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
    available = limit - suffix_bytes
    prefix = text.encode("utf-8")[:available].decode("utf-8", errors="ignore").rstrip()
    return f"{prefix}…{suffix}"


def bound_message_bytes(message: str) -> tuple[str, bool]:
    if len(message.encode("utf-8")) <= DISCORD_LIMIT_BYTES:
        return message, False

    financial_index = message.find("\nFinancial detail:")
    if financial_index >= 0:
        prefix = message[:financial_index].rstrip()
        for heading in ("\n- Financial Reconciliation Correction:", "\nFinancial Reconciliation Correction:"):
            if heading in prefix:
                prefix = prefix.split(heading, 1)[0].rstrip()
        financial_detail = message[financial_index:].lstrip()
        candidate = f"{prefix}\n\n{financial_detail}"
        if len(candidate.encode("utf-8")) <= DISCORD_LIMIT_BYTES:
            return candidate, True
        title = message.splitlines()[0] if message.splitlines() else "Property Update"
        candidate = f"{title}\n\n{financial_detail}"
        if len(candidate.encode("utf-8")) <= DISCORD_LIMIT_BYTES:
            return candidate, True

    return truncate_utf8(message, DISCORD_LIMIT_BYTES), True


def compact_message(message: str) -> tuple[str, bool]:
    compacted = strip_embedded_property_update_history(dedupe_financial_summary(message))
    compacted, bounded = bound_message_bytes(compacted)
    return compacted, bounded or compacted != message


def normalize_property_name(value: object) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    return " ".join(token for token in text.split() if token != "public")


def display_property_name(value: object) -> str:
    return re.sub(r"\s+Public\s*$", "", str(value or "").strip(), flags=re.IGNORECASE).strip()


def property_names_match(left: object, right: object) -> bool:
    left_normalized = normalize_property_name(left)
    right_normalized = normalize_property_name(right)
    return bool(
        left_normalized
        and right_normalized
        and (left_normalized in right_normalized or right_normalized in left_normalized)
    )


def excluded_by_manual_policy(property_name: object, property_path: object = None) -> str:
    haystack = f"{property_name or ''}\n{property_path or ''}".lower()
    for excluded in DEFAULT_MANUAL_EXCLUDED_PROPERTIES:
        if excluded.lower() in haystack:
            return excluded
    return ""


def financial_review_issue_records(
    readiness: dict[str, Any],
    transfer_reconciliation: dict[str, Any],
    financial_patch_readiness: dict[str, Any] | None = None,
) -> list[dict[str, str | None]]:
    issues: list[dict[str, str | None]] = []
    financial_patch_readiness = financial_patch_readiness or {}

    def add(issue: str, property_name: object = None) -> None:
        issues.append(
            {
                "issue": issue,
                "property_name": str(property_name or "").strip() or None,
            }
        )

    for key in (
        "lofty_financial_patch_candidate_packet_monthly_summary_issue_records",
        "lofty_financial_patch_runtime_monthly_summary_issue_records",
    ):
        records = readiness.get(key) if isinstance(readiness.get(key), list) else []
        source = "candidate_packet" if "candidate_packet" in key else "runtime"
        for record in records:
            if not isinstance(record, dict):
                continue
            property_text = str(record.get("property_name") or "unknown")
            missing_fields = ",".join(str(field) for field in (record.get("missing_required_fields") or []))
            add(
                f"lofty_monthly_summary_issue:{source}:{property_text}:missing={missing_fields or 'unknown'}",
                property_text,
            )
    primary = readiness.get("primary_blocker") if isinstance(readiness.get("primary_blocker"), dict) else {}
    primary_class = str(primary.get("class") or "")
    if primary_class.startswith(UPSTREAM_FINANCIAL_BLOCKER_PREFIXES):
        add(f"monthly_readiness_upstream_blocker:{primary.get('blocker') or primary_class}")
    patch_status = str(financial_patch_readiness.get("status") or "").strip()
    patch_blocked_count = int(financial_patch_readiness.get("blocked_count") or 0)
    if patch_status and (patch_status != "ok" or patch_blocked_count):
        # Financial listing approval is portfolio-wide: do not publish a mixed month.
        add(
            "lofty_financial_patch_readiness_not_ready:"
            f"status={patch_status}:blocked_count={patch_blocked_count}"
        )
    if primary_class.startswith("operational.source_cash_balance"):
        primary_evidence = primary.get("evidence") if isinstance(primary.get("evidence"), dict) else {}
        scoped_primary_actions = primary_evidence.get("zero_row_source_ledger_decision_missing_actions") or []
        if not scoped_primary_actions:
            add(f"monthly_readiness_source_cash_blocker:{primary.get('blocker') or primary_class}")
        for action in scoped_primary_actions:
            if not isinstance(action, dict):
                continue
            property_text = str(action.get("matched_active_property") or action.get("property") or "unknown")
            source_mode = str(action.get("eco_gl_column_e_source_mode") or "source_ledger_zero_rows")
            rows = action.get("eco_gl_column_e_row_count")
            amount = action.get("eco_gl_column_e_sum")
            add(
                f"zero_row_source_ledger_decision_missing:{property_text}:rows={rows}:sum={amount}:source_mode={source_mode}",
                property_text,
            )
    scoped_transfer_actions = [
        action
        for action in transfer_reconciliation.get("source_cash_reconciliation_active_monthly_candidate_actions") or []
        if isinstance(action, dict)
    ]
    property_cash_review_details = [
        detail
        for detail in transfer_reconciliation.get("property_cash_review_details") or []
        if isinstance(detail, dict)
    ]
    if (
        transfer_reconciliation.get("recommended_send_to_lofty_total_is_final") is False
        and not scoped_transfer_actions
        and not property_cash_review_details
    ):
        add("transfer_reconciliation_not_final")
    active_actions = int(transfer_reconciliation.get("source_cash_reconciliation_active_monthly_candidate_action_count") or 0)
    if active_actions and not scoped_transfer_actions:
        add(f"source_cash_active_reconciliation_actions={active_actions}")
    for blocker in transfer_reconciliation.get("source_blockers") or []:
        blocker_text = str(blocker or "")
        if blocker_text.startswith(
            (
                "source_cash_",
                "property_cash_review:",
                "monthly_accruals_",
                "missing_lofty_reserve_decision_",
            )
        ):
            add(f"transfer_source_blocker:{blocker_text}")
    for row in transfer_reconciliation.get("rows") or []:
        if not isinstance(row, dict):
            continue
        property_text = str(row.get("property_name") or row.get("property") or "unknown")
        for reason in row.get("hold_reasons") or []:
            reason_text = str(reason or "")
            if reason_text.startswith("coownership_gl_policy:"):
                add(f"transfer_hold_reason:{property_text}:{reason_text}", property_text)
    for action in scoped_transfer_actions:
        property_text = str(action.get("matched_active_property") or action.get("property") or "unknown")
        action_text = str(action.get("action") or action.get("kind") or "review")
        add(f"source_cash_active_action:{property_text}:{action_text}", property_text)
    for detail in property_cash_review_details:
        property_text = str(detail.get("property") or "unknown")
        exposure = detail.get("net_cash_exposure_review") if isinstance(detail.get("net_cash_exposure_review"), dict) else {}
        exposure_text = exposure.get("high_priority_unresolved_sum")
        add(
            f"property_cash_review_detail:{property_text}:review_rows={detail.get('classification_review_count', 0)}:high_priority_unresolved={exposure_text}",
            property_text,
        )
    return issues


def financial_review_issues(readiness: dict[str, Any], transfer_reconciliation: dict[str, Any]) -> list[str]:
    return [str(record["issue"]) for record in financial_review_issue_records(readiness, transfer_reconciliation)]


def build_plan(
    candidate_packet: dict[str, Any],
    readiness: dict[str, Any] | None = None,
    transfer_reconciliation: dict[str, Any] | None = None,
    monthly_run_report: dict[str, Any] | None = None,
    financial_patch_readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    readiness = readiness or {}
    transfer_reconciliation = transfer_reconciliation or {}
    monthly_run_report = monthly_run_report or {}
    financial_patch_readiness = financial_patch_readiness or {}
    merged_readiness = {**readiness, **monthly_run_report}
    records = candidate_packet.get("records") if isinstance(candidate_packet.get("records"), list) else []
    plan_records: list[dict[str, Any]] = []
    issues: list[str] = []
    financial_issue_records = financial_review_issue_records(
        merged_readiness,
        transfer_reconciliation,
        financial_patch_readiness,
    )
    financial_issues = [str(item["issue"]) for item in financial_issue_records]
    issues.extend(financial_issues)
    for record in records:
        if not isinstance(record, dict):
            continue
        raw_status = str(record.get("status") or "").strip()
        if raw_status and not is_active_index_status(raw_status):
            continue
        property_name = str(record.get("property_name") or record.get("input_property_name") or "").strip()
        manual_exclusion = excluded_by_manual_policy(
            property_name,
            record.get("property_path") or record.get("input_property_path"),
        )
        if manual_exclusion:
            continue
        record_financial_issues = [
            str(item["issue"])
            for item in financial_issue_records
            if item.get("property_name") is None or property_names_match(property_name, item.get("property_name"))
        ]
        message_body, draft_path, read_issue = read_text(record.get("update_candidate"))
        if read_issue:
            issues.append(f"{read_issue}:{property_name}")
            continue
        message, compacted = compact_message(f"Property Update: {display_property_name(property_name)}\n\n{message_body}\n")
        channel_id, route_matched = discord_route.channel_for_property(property_name)
        plan_records.append(
            {
                "property_name": property_name,
                "property_path": record.get("property_path") or record.get("input_property_path"),
                "draft_path": draft_path,
                "message": message,
                "message_bytes": len(message.encode("utf-8")),
                "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
                "financials_md_summary_sha256": record.get("financials_md_summary_sha256"),
                "financials_md_summary_char_count": record.get("financials_md_summary_char_count"),
                "financial_summary_source_mode": record.get("financial_summary_source_mode"),
                "message_compacted": compacted,
                "has_financial_summary": has_financial_summary(message),
                "route_matched": route_matched,
                "target": f"channel:{channel_id}",
                "status": "current_candidate",
                "financial_review_blocked": bool(record_financial_issues),
                "financial_review_blockers": record_financial_issues[:25],
            }
        )
    blocked_record_count = sum(1 for record in plan_records if record.get("financial_review_blocked") is True)
    ready_record_count = sum(1 for record in plan_records if record.get("financial_review_blocked") is not True)
    global_financial_issue_count = sum(1 for item in financial_issue_records if item.get("property_name") is None)
    non_financial_issue_count = len([issue for issue in issues if issue not in financial_issues])
    plan_status = (
        "ok"
        if not issues
        else (
            "ok_partial"
            if ready_record_count and blocked_record_count and not global_financial_issue_count and not non_financial_issue_count
            else "review"
        )
    )
    return {
        "generated_at": iso_z(),
        "status": plan_status,
        "run_month": candidate_packet.get("run_month"),
        "candidate_packet_status": candidate_packet.get("status"),
        "candidate_packet": str(DEFAULT_CANDIDATE_PACKET),
        "monthly_readiness_status": readiness.get("status"),
        "monthly_readiness_primary_blocker": readiness.get("primary_blocker"),
        "transfer_reconciliation_status": transfer_reconciliation.get("status"),
        "transfer_reconciliation_recommended_total_is_final": transfer_reconciliation.get("recommended_send_to_lofty_total_is_final"),
        "lofty_financial_patch_readiness_status": financial_patch_readiness.get("status"),
        "lofty_financial_patch_blocked_count": int(financial_patch_readiness.get("blocked_count") or 0),
        "source_cash_reconciliation_active_monthly_candidate_action_count": transfer_reconciliation.get("source_cash_reconciliation_active_monthly_candidate_action_count"),
        "candidate_packet_record_count": len(records),
        "record_count": len(plan_records),
        "plan_count": len(plan_records),
        "issue_count": len(issues),
        "issues": issues,
        "financial_review_issue_count": len(financial_issues),
        "financial_review_issues": financial_issues,
        "property_scoped_financial_review_issue_count": sum(
            1 for item in financial_issue_records if item.get("property_name") is not None
        ),
        "global_financial_review_issue_count": global_financial_issue_count,
        "financial_review_blocked_record_count": blocked_record_count,
        "financial_review_ready_record_count": ready_record_count,
        "financials_md_summary_digest_required": True,
        "records": plan_records,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the monthly all-property Discord review send plan.")
    parser.add_argument("--candidate-packet", type=Path, default=DEFAULT_CANDIDATE_PACKET)
    parser.add_argument("--readiness", type=Path, default=DEFAULT_READINESS)
    parser.add_argument("--transfer-reconciliation", type=Path, default=DEFAULT_TRANSFER_RECONCILIATION)
    parser.add_argument("--monthly-run-report", type=Path, default=DEFAULT_MONTHLY_RUN_REPORT)
    parser.add_argument("--financial-patch-readiness", type=Path, default=DEFAULT_FINANCIAL_PATCH_READINESS)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    args = parser.parse_args(argv)
    candidate_packet = read_json(args.candidate_packet)
    readiness = read_json(args.readiness)
    transfer_reconciliation = read_json(args.transfer_reconciliation)
    monthly_run_report = read_json(args.monthly_run_report)
    financial_patch_readiness = read_json(args.financial_patch_readiness)
    plan = build_plan(
        candidate_packet,
        readiness,
        transfer_reconciliation,
        monthly_run_report,
        financial_patch_readiness,
    )
    plan["candidate_packet"] = str(args.candidate_packet)
    plan["monthly_readiness"] = str(args.readiness)
    plan["monthly_run_report"] = str(args.monthly_run_report)
    plan["financial_patch_readiness"] = str(args.financial_patch_readiness)
    plan["transfer_reconciliation"] = str(args.transfer_reconciliation)
    args.plan.parent.mkdir(parents=True, exist_ok=True)
    args.plan.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"status={plan['status']} records={plan['record_count']} issues={plan['issue_count']} plan={args.plan}")
    return 0 if plan["status"] in {"ok", "ok_partial"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
