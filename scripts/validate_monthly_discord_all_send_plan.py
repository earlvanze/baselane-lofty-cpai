#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from discord_summary_routing_policy import (
    ACTIVE_PORTFOLIO_SUMMARY_POLICY,
    ACTIVE_PORTFOLIO_SUMMARY_POLICY_VERSION,
    DRAFT_REVIEW_PREFIX,
    EARLCOIN_GUILD_ID,
    EARLCOIN_REVIEW_FORUM_ID,
    EARLCOIN_REVIEW_TARGET,
    REVIEW_DESTINATION_CLASS,
    REVIEW_DESTINATION_PURPOSE,
    EXPECTED_ACTIVE_PHYSICAL_PROPERTY_COUNT,
    EXPECTED_ACTIVE_REPORTING_TARGET_COUNT,
    active_portfolio_summary_population_issues,
    normalize_thread_name,
    review_route_issues,
)


DEFAULT_PLAN = Path("reports/baselane_financials_monthly_discord_all_send_plan.json")
DEFAULT_REPORT = Path("reports/baselane_financials_monthly_discord_all_send_plan_validation.json")
CHANNEL_TARGET_RE = re.compile(r"^channel:\d{17,25}$")
DISCORD_REVIEW_BODY_LIMIT_BYTES = 2000 - len(DRAFT_REVIEW_PREFIX.encode("utf-8"))
REVIEW_PACKET_ARTIFACTS = {
    "monthly_accruals": [
        "reports/baselane_monthly_accrual_gap_approvals_review.csv",
        "config/baselane_monthly_accrual_gap_approvals.json",
        "reports/baselane_monthly_accrual_gap_approvals_import.requires-explicit-approval.sh",
    ],
    "property_cash_review:804 s quitman st": [
        "reports/baselane_804_quitman_cash_alignment_review.md",
        "reports/baselane_804_quitman_cash_alignment_group_review_queue.csv",
        "config/baselane_804_quitman_cash_alignment_reviewed_template.json",
        "reports/baselane_804_quitman_cash_alignment_import_group_review.requires-explicit-approval.sh",
    ],
    "coownership_85104_preclosing_retag": [
        "reports/coownership_gl_policy_validation.json",
        "reports/baselane_85104_preclosing_property_retag_audit.json",
        "reports/baselane_85104_preclosing_property_retag_audit.csv",
        "reports/baselane_85104_preclosing_property_retag_partial_apply.requires-explicit-approval.sh",
        "reports/baselane_85104_preclosing_protected_row_review_import.requires-explicit-approval.sh",
    ],
    "lofty_financial_summary": [
        "reports/lofty_financial_patch_readiness.json",
        "reports/lofty-review-candidates/2026-06/missing-lofty-reserve-review.csv",
        "reports/lofty-review-candidates/2026-06/missing-lofty-reserve-review.md",
    ],
}
FINANCIALS_SUMMARY_MARKERS = (
    "Financial detail:",
    "Financial summary from FINANCIALS.md:",
    "Financial summary as of ",
)
REQUIRED_SPENDABLE_CASH_SNIPPET = (
    "ECO Net DAO Funds (spendable cash held by ECO)"
)
OBSOLETE_LEDGER_CASH_SNIPPETS = (
    "ECO Operating Cash is the full DAO-attributed Column E sum",
    "ECO General Ledger is the complete DAO-attributed Column E total",
    "ECO GL Column E sum",
    "Reserve-adjusted operating position:",
    "The reserve-adjusted operating position combines",
    "ECO A/R - Due from DAO:",
)
DISALLOWED_SUMMARY_SNIPPETS = (
    "This month's update is limited to verified cash-position data from Lofty and ECO records.",
    "No tenant ledger rows are included.",
)
FINANCIAL_SUMMARY_HEADING_RE = re.compile(
    r"(?m)^##\s+(?:Cash Flow Snapshot|Monthly Cash Position|Source Evidence)\s*\((20\d{2}-\d{2})\)\s*$"
)


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "error": str(exc)}
    return data if isinstance(data, dict) else {"status": "unreadable", "error": "not a JSON object"}


def draft_text_for(record: dict[str, Any], plan_path: Path) -> tuple[str, str | None, str]:
    inline_text = record.get("message") or record.get("content") or record.get("text")
    if isinstance(inline_text, str) and inline_text.strip():
        return inline_text, None, "inline_message"
    draft_path_text = str(record.get("draft_path") or "").strip()
    if not draft_path_text:
        return "", "missing_draft_path", "missing"
    draft_path = Path(draft_path_text)
    if not draft_path.is_absolute():
        draft_path = plan_path.parent / draft_path
    if not draft_path.is_file():
        return "", f"draft_not_found:{draft_path}", "draft_path"
    try:
        return draft_path.read_text(encoding="utf-8", errors="replace"), None, "draft_path"
    except Exception as exc:  # noqa: BLE001
        return "", f"draft_unreadable:{draft_path}:{exc}", "draft_path"


def run_month_from_plan(plan: dict[str, Any], plan_path: Path) -> str:
    for key in ("run_month", "month", "reporting_month"):
        value = str(plan.get(key) or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}", value):
            return value
    match = re.search(r"(20\d{2}-\d{2})", plan_path.name)
    return match.group(1) if match else ""


def month_end_for(run_month: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}", run_month):
        return ""
    year, month = (int(part) for part in run_month.split("-"))
    if month == 12:
        next_month_start = datetime(year + 1, 1, 1, tzinfo=timezone.utc).date()
    else:
        next_month_start = datetime(year, month + 1, 1, tzinfo=timezone.utc).date()
    return (next_month_start - timedelta(days=1)).isoformat()


def has_as_of_or_month_heading(text: str, run_month: str) -> bool:
    if re.search(r"\bas of\b", text, flags=re.IGNORECASE):
        return True
    return bool(
        run_month
        and re.search(
            rf"(?m)^##\s+(?:Cash Flow Snapshot|Monthly Cash Position|Source Evidence)\s*\({re.escape(run_month)}\)\s*$",
            text,
        )
    )


def has_run_month_financial_summary(text: str, run_month: str) -> bool:
    if not run_month:
        return True
    run_month_end = ""
    try:
        year_text, month_text = run_month.split("-")
        year = int(year_text)
        month = int(month_text)
        if month == 12:
            next_year, next_month = year + 1, 1
        else:
            next_year, next_month = year, month + 1
        next_month_start = datetime(next_year, next_month, 1, tzinfo=timezone.utc).date()
        run_month_end = (next_month_start - timedelta(days=1)).isoformat()
    except Exception:  # noqa: BLE001
        run_month_end = ""
    return bool(
        re.search(rf"(?m)^##\s+(?:Cash Flow Snapshot|Monthly Cash Position|Source Evidence)\s*\({re.escape(run_month)}\)\s*$", text)
        or re.search(rf"\b(?:as of|Amounts are shown as of)\s+{re.escape(run_month)}(?:\b|[.-])", text, flags=re.IGNORECASE)
        or (run_month_end and re.search(rf"\b(?:as of|Amounts are shown as of)\s+{re.escape(run_month_end)}(?:\b|[.-])", text, flags=re.IGNORECASE))
    )


def stale_financial_summary_months(text: str, run_month: str) -> list[str]:
    if not run_month:
        return []
    months = [match.group(1) for match in FINANCIAL_SUMMARY_HEADING_RE.finditer(text)]
    return sorted({month for month in months if month != run_month})


def financial_summary_issue(record: dict[str, Any], plan_path: Path, run_month: str = "") -> tuple[str, str]:
    text, read_issue, text_source = draft_text_for(record, plan_path)
    if read_issue:
        return read_issue, text_source
    if record.get("has_financial_summary") is not True:
        return "record_has_financial_summary_false", text_source
    if not any(marker in text for marker in FINANCIALS_SUMMARY_MARKERS):
        return "draft_missing_financial_summary_text", text_source
    if not has_as_of_or_month_heading(text, run_month):
        return "draft_missing_as_of_date", text_source
    if not has_run_month_financial_summary(text, run_month):
        return f"draft_financial_summary_not_current_for_run_month:{run_month}", text_source
    stale_months = stale_financial_summary_months(text, run_month)
    if stale_months:
        return f"draft_contains_stale_financial_summary_months:{','.join(stale_months)}:run_month={run_month}", text_source
    for snippet in DISALLOWED_SUMMARY_SNIPPETS:
        if snippet in text:
            return f"draft_contains_disallowed_limited_summary:{snippet}", text_source
    if REQUIRED_SPENDABLE_CASH_SNIPPET not in text:
        return "draft_missing_verified_spendable_eco_cash", text_source
    for snippet in OBSOLETE_LEDGER_CASH_SNIPPETS:
        if snippet in text:
            return f"draft_contains_obsolete_ledger_cash_wording:{snippet}", text_source
    return "", text_source


def message_digest_issue(record: dict[str, Any], plan_path: Path) -> tuple[str, str]:
    expected = str(record.get("message_sha256") or "").strip().lower()
    if not expected:
        return "message_sha256_missing", "missing"
    text, read_issue, text_source = draft_text_for(record, plan_path)
    if read_issue:
        return f"message_digest_source_{read_issue}", text_source
    actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if actual != expected:
        return "message_sha256_mismatch_current_text", text_source
    return "", text_source


def sha256ish(value: object) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", str(value or "").strip().lower()))


def monthly_accrual_review_markdown_path(plan: dict[str, Any]) -> str:
    run_month = str(plan.get("run_month") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}", run_month):
        return f"reports/baselane_monthly_accruals_{run_month.replace('-', '')}_review.md"
    return "reports/baselane_monthly_accruals_YYYYMM_review.md"


def financial_review_artifacts(plan: dict[str, Any], financial_review_issues: list[Any]) -> list[dict[str, Any]]:
    buckets: dict[str, set[str]] = {}
    for issue in financial_review_issues:
        issue_text = str(issue or "").lower()
        if "monthly_accruals_" in issue_text:
            buckets.setdefault("monthly_accruals", set()).update(REVIEW_PACKET_ARTIFACTS["monthly_accruals"])
            buckets["monthly_accruals"].add(monthly_accrual_review_markdown_path(plan))
        if "property_cash_review:804 s quitman st" in issue_text or "property_cash_review_detail:804 s quitman" in issue_text:
            buckets.setdefault("property_cash_review:804 s quitman st", set()).update(
                REVIEW_PACKET_ARTIFACTS["property_cash_review:804 s quitman st"]
            )
        if "coownership" in issue_text or "85-104" in issue_text or "85104" in issue_text or "pre_launch_rows" in issue_text:
            buckets.setdefault("coownership_85104_preclosing_retag", set()).update(
                REVIEW_PACKET_ARTIFACTS["coownership_85104_preclosing_retag"]
            )
        if (
            "lofty_monthly_summary_issue:" in issue_text
            or "lofty_curr_maintenance_reserve" in issue_text
            or "lofty_financial_patch_readiness" in issue_text
        ):
            buckets.setdefault("lofty_financial_summary", set()).update(
                REVIEW_PACKET_ARTIFACTS["lofty_financial_summary"]
            )
    return [
        {
            "review_area": area,
            "artifacts": sorted(paths),
            "missing_artifacts": sorted(path for path in paths if not Path(path).is_file()),
        }
        for area, paths in sorted(buckets.items())
    ]


def validate_plan(plan: dict[str, Any], plan_path: Path) -> dict[str, Any]:
    records = plan.get("records") if isinstance(plan.get("records"), list) else []
    authoritative_active_property_count = plan.get("authoritative_active_property_count")
    authoritative_reporting_target_count = plan.get("authoritative_reporting_target_count")
    population_issues = active_portfolio_summary_population_issues(
        authoritative_active_property_count,
        authoritative_reporting_target_count,
        len(records),
    )
    blocked_records = [
        record for record in records if isinstance(record, dict) and record.get("financial_review_blocked") is True
    ]
    ready_records = [
        record for record in records if isinstance(record, dict) and record.get("financial_review_blocked") is not True
    ]
    source_issues = plan.get("issues") if isinstance(plan.get("issues"), list) else []
    financial_review_issues = (
        plan.get("financial_review_issues")
        if isinstance(plan.get("financial_review_issues"), list)
        else []
    )
    financial_review_issue_set = {str(issue) for issue in financial_review_issues}
    global_financial_review_issue_count = int(
        plan.get("global_financial_review_issue_count")
        if plan.get("global_financial_review_issue_count") is not None
        else len(financial_review_issues)
    )
    review_artifacts = financial_review_artifacts(plan, financial_review_issues)
    summary_digest_required = plan.get("financials_md_summary_digest_required") is True
    source_issues_without_financial = [
        issue for issue in source_issues if str(issue) not in financial_review_issue_set
    ]
    run_month = run_month_from_plan(plan, plan_path)
    expected_cutoff_date = month_end_for(run_month)
    reporting_cutoff_date = str(plan.get("reporting_cutoff_date") or "").strip()
    unmapped = []
    stale_route = []
    missing_summary = []
    digest_issues = []
    summary_digest_issues = []
    text_source_counts: dict[str, int] = {}
    invalid_target = []
    oversize = []
    thread_properties: dict[str, list[str]] = {}
    destination_issues = review_route_issues(plan)
    for issue in destination_issues:
        issue_name = str(issue.get("issue") or "")
        if issue_name == "record.thread_name_missing":
            unmapped.append(issue)
        elif issue_name != "record.duplicate_thread_route":
            stale_route.append(issue)
    for record in records:
        if not isinstance(record, dict):
            continue
        property_name = str(record.get("property_name") or "")
        target = str(record.get("target") or "")
        normalized_thread = normalize_thread_name(record.get("thread_name"))
        if normalized_thread:
            thread_properties.setdefault(normalized_thread, []).append(property_name)
        if not property_name.strip() or record.get("route_matched") is not True:
            unmapped.append(
                {
                    "property_name": property_name,
                    "target": target,
                    "thread_name": record.get("thread_name"),
                    "draft_path": record.get("draft_path"),
                }
            )
        summary_issue, text_source = financial_summary_issue(record, plan_path, run_month)
        text_source_counts[text_source] = text_source_counts.get(text_source, 0) + 1
        digest_issue, digest_text_source = message_digest_issue(record, plan_path)
        if digest_issue:
            digest_issues.append(
                {
                    "property_name": property_name,
                    "draft_path": record.get("draft_path"),
                    "reason": digest_issue,
                    "text_source": digest_text_source,
                }
            )
        if summary_issue:
            missing_summary.append(
                {
                    "property_name": property_name,
                    "draft_path": record.get("draft_path"),
                    "reason": summary_issue,
                    "text_source": text_source,
                }
            )
        if summary_digest_required and not summary_issue and not sha256ish(record.get("financials_md_summary_sha256")):
            summary_digest_issues.append(
                {
                    "property_name": property_name,
                    "draft_path": record.get("draft_path"),
                    "reason": "financials_md_summary_sha256_missing_or_invalid",
                    "text_source": text_source,
                }
            )
        if not CHANNEL_TARGET_RE.fullmatch(target):
            invalid_target.append({"property_name": property_name, "target": target})
        if int(record.get("message_bytes") or 0) > DISCORD_REVIEW_BODY_LIMIT_BYTES:
            oversize.append({"property_name": property_name, "message_bytes": record.get("message_bytes")})
    duplicate_targets = [
        {
            "target": EARLCOIN_REVIEW_TARGET,
            "thread_name_normalized": thread_name,
            "property_names": property_names,
        }
        for thread_name, property_names in sorted(thread_properties.items())
        if len(property_names) > 1
    ]
    blocking_issues = []
    blocking_issues.extend(population_issues)
    if reporting_cutoff_date != expected_cutoff_date:
        blocking_issues.append(
            "reporting cutoff is not the exact month end: "
            f"actual={reporting_cutoff_date or 'missing'} expected={expected_cutoff_date or 'unknown'}"
        )
    if not records:
        blocking_issues.append("all-send plan has no records")
    if unmapped:
        blocking_issues.append(f"{len(unmapped)} Discord property route(s) unresolved")
    if stale_route:
        blocking_issues.append(
            f"{len(stale_route)} EARLCoin review destination field(s) stale; regenerate all-send plan"
        )
    if missing_summary:
        blocking_issues.append(f"{len(missing_summary)} Discord draft(s) missing FINANCIALS.md summary")
    if digest_issues:
        blocking_issues.append(f"{len(digest_issues)} Discord draft digest check(s) failed")
    if summary_digest_issues:
        blocking_issues.append(f"{len(summary_digest_issues)} Discord FINANCIALS.md summary digest proof(s) missing")
    if invalid_target:
        blocking_issues.append(f"{len(invalid_target)} Discord target(s) invalid")
    if oversize:
        blocking_issues.append(
            f"{len(oversize)} Discord draft body message(s) exceed {DISCORD_REVIEW_BODY_LIMIT_BYTES} bytes"
        )
    if duplicate_targets:
        blocking_issues.append(f"{len(duplicate_targets)} EARLCoin property thread route(s) are duplicated")
    if source_issues_without_financial:
        blocking_issues.append(f"{len(source_issues_without_financial)} source plan issue(s) present")
    non_financial_issue_count = len(blocking_issues)
    discord_review_ready = bool(records) and non_financial_issue_count == 0
    eligible_discord_send_ready = bool(ready_records) and discord_review_ready and global_financial_review_issue_count == 0
    if global_financial_review_issue_count:
        blocking_issues.append(f"{global_financial_review_issue_count} financial review blocker(s) present")
    partial_ready = bool(blocked_records and eligible_discord_send_ready and not blocking_issues)
    if not blocking_issues:
        next_action = None
    elif (
        global_financial_review_issue_count
        and not unmapped
        and not stale_route
        and not missing_summary
        and not digest_issues
        and not summary_digest_issues
        and not invalid_target
        and not oversize
        and not duplicate_targets
        and not source_issues_without_financial
    ):
        next_action = (
            "Post the blocked drafts to the EARLCoin review forum with their financial-review hold labels. "
            "Resolve those blockers and obtain separate human approvals before Lofty guild publication or email."
        )
    elif partial_ready:
        next_action = (
            "Post only financially ready records to the EARLCoin review forum; keep explicitly blocked "
            "properties, Lofty guild publication, and email held."
        )
    else:
        next_action = (
            "Regenerate FINANCIALS-backed drafts with the canonical EARLCoin review route, rerun validation, "
            "then post for review. Do not publish financial summaries to Lofty or send email from this stage."
        )
    return {
        "generated_at": iso_z(),
        "status": "ok_partial" if partial_ready else ("ok" if not blocking_issues else "review"),
        "plan": str(plan_path),
        "run_month": run_month or None,
        "reporting_cutoff_date": reporting_cutoff_date or None,
        "expected_reporting_cutoff_date": expected_cutoff_date or None,
        "record_count": len(records),
        "authoritative_active_property_count": authoritative_active_property_count,
        "authoritative_reporting_target_count": authoritative_reporting_target_count,
        "active_reporting_summary_count": len(records),
        "active_portfolio_summary_population_ok": not population_issues,
        "active_portfolio_summary_population_issues": population_issues,
        "active_portfolio_summary_policy_version": ACTIVE_PORTFOLIO_SUMMARY_POLICY_VERSION,
        "active_portfolio_summary_policy": ACTIVE_PORTFOLIO_SUMMARY_POLICY,
        "expected_active_physical_property_count": EXPECTED_ACTIVE_PHYSICAL_PROPERTY_COUNT,
        "expected_active_reporting_target_count": EXPECTED_ACTIVE_REPORTING_TARGET_COUNT,
        "issue_count": len(blocking_issues),
        "issues": blocking_issues,
        "non_financial_issue_count": non_financial_issue_count,
        "discord_review_ready": discord_review_ready,
        "eligible_discord_send_ready": eligible_discord_send_ready,
        "discord_review_ready_but_financial_blocked": bool(
            discord_review_ready and financial_review_issues
        ),
        "discord_review_policy": (
            "Monthly financial draft summaries route only to the EARLCoin eco-systems-pm review forum. "
            "Lofty Investors publication and owner email each require subsequent human approval."
        ),
        "earlcoin_review_route_ok": not destination_issues,
        "destination_issue_count": len(destination_issues),
        "destination_issues": destination_issues,
        "destination_class": REVIEW_DESTINATION_CLASS,
        "destination_purpose": REVIEW_DESTINATION_PURPOSE,
        "destination_guild_id": EARLCOIN_GUILD_ID,
        "destination_forum_id": EARLCOIN_REVIEW_FORUM_ID,
        "destination_target": EARLCOIN_REVIEW_TARGET,
        "source_issue_count": len(source_issues_without_financial),
        "source_issues": source_issues_without_financial,
        "financial_review_issue_count": len(financial_review_issues),
        "global_financial_review_issue_count": global_financial_review_issue_count,
        "financial_review_blocked_record_count": len(blocked_records),
        "financial_review_ready_record_count": len(ready_records),
        "financial_review_blocked_properties": [
            {
                "property_name": str(record.get("property_name") or ""),
                "financial_review_blockers": record.get("financial_review_blockers") or [],
            }
            for record in blocked_records
        ],
        "financial_review_issues": financial_review_issues,
        "financial_review_artifacts": review_artifacts,
        "financial_review_artifact_area_count": len(review_artifacts),
        "financial_review_missing_artifact_count": sum(
            len(item.get("missing_artifacts") or []) for item in review_artifacts
        ),
        "transfer_reconciliation_recommended_total_is_final": plan.get("transfer_reconciliation_recommended_total_is_final"),
        "source_cash_reconciliation_active_monthly_candidate_action_count": plan.get("source_cash_reconciliation_active_monthly_candidate_action_count"),
        "text_source_counts": dict(sorted(text_source_counts.items())),
        "unmapped_count": len(unmapped),
        "stale_route_count": len(stale_route),
        "missing_financial_summary_count": len(missing_summary),
        "message_digest_issue_count": len(digest_issues),
        "financials_md_summary_digest_issue_count": len(summary_digest_issues),
        "invalid_target_count": len(invalid_target),
        "oversize_message_count": len(oversize),
        "duplicate_target_count": len(duplicate_targets),
        "unmapped": unmapped,
        "stale_routes": stale_route,
        "missing_financial_summary": missing_summary,
        "message_digest_issues": digest_issues,
        "financials_md_summary_digest_issues": summary_digest_issues,
        "invalid_target": invalid_target,
        "oversize_messages": oversize,
        "duplicate_targets": duplicate_targets,
        "next_action": next_action,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate monthly all-property Discord send plan coverage.")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    plan = read_json(args.plan)
    report = validate_plan(plan, args.plan)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"status={report['status']} records={report['record_count']} issues={report['issue_count']} report={args.report}")
    return 0 if report["status"] in {"ok", "ok_partial"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
