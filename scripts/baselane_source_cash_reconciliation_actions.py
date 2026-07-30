#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from lofty_monthly_exclusions import match_exclusion_guard, monthly_exclusion_guards
except ImportError:
    match_exclusion_guard = None
    monthly_exclusion_guards = None


ROOT = Path(__file__).absolute().parents[1]
DEFAULT_SOURCE_CASH_REPORT = ROOT / "reports" / "baselane_daily_source_cash_balance_report.json"
DEFAULT_WEEKLY_CF_REPORT = ROOT / "reports" / "baselane_weekly_cf_statement_sync_report.json"
DEFAULT_CANDIDATE_PACKET = ROOT / "reports" / "baselane_financials_monthly_review_candidate_packet.json"
DEFAULT_OWNER_REVIEW_GATE = ROOT / "reports" / "baselane_monthly_owner_review_gate.json"
DEFAULT_ZERO_ROW_DECISIONS = ROOT / "config" / "baselane_zero_row_source_ledger_decisions.json"
DEFAULT_YHOME_CSV = ROOT / "reports" / "yhome_transition_reconciliation.csv"
DEFAULT_LISTING_POLICY = ROOT / "config" / "lofty_listing_update_policy.json"
DEFAULT_REPORT = ROOT / "reports" / "baselane_source_cash_reconciliation_actions.json"
DEFAULT_MARKDOWN = ROOT / "reports" / "baselane_source_cash_reconciliation_actions.md"
DEFAULT_CSV = ROOT / "reports" / "baselane_source_cash_reconciliation_actions.csv"


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "path": str(path), "error": f"{type(exc).__name__}: {exc}"}
    return value if isinstance(value, dict) else {"status": "unreadable", "path": str(path), "error": "not a JSON object"}


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def count(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_property_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def property_tokens(value: object) -> set[str]:
    aliases = {"s": "south", "n": "north", "e": "east", "w": "west", "st": "street", "ave": "avenue"}
    stopwords = {
        "akron",
        "albany",
        "ave",
        "avenue",
        "blvd",
        "cleveland",
        "co",
        "denver",
        "fl",
        "hi",
        "ny",
        "oh",
        "ohio",
        "public",
        "st",
        "street",
    }
    return {
        aliases.get(token, token)
        for token in normalize_property_name(value).split()
        if token not in stopwords
    }


def leading_number(value: object) -> str | None:
    match = re.search(r"\b(\d+)\b", normalize_property_name(value))
    return match.group(1) if match else None


def property_matches(left: object, right: object) -> bool:
    left_norm = normalize_property_name(left)
    right_norm = normalize_property_name(right)
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True
    left_tokens = property_tokens(left)
    right_tokens = property_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    left_number = leading_number(left)
    right_number = leading_number(right)
    if left_number and right_number and left_number != right_number:
        return False
    shared = left_tokens & right_tokens
    if left_number and right_number and left_number == right_number and len(shared) >= 2:
        return True
    return len(shared) >= min(len(left_tokens), len(right_tokens), 3)


def load_scope(
    candidate_packet: Path,
    owner_review_gate: Path,
    *,
    yhome_csv: Path | None = None,
    listing_policy: Path | None = None,
) -> dict[str, Any]:
    candidate = read_json(candidate_packet)
    owner = read_json(owner_review_gate)
    active_names: set[str] = set()
    excluded_names: set[str] = set()
    exclusion_guards: list[dict[str, Any]] = []
    if callable(monthly_exclusion_guards) and (yhome_csv or listing_policy):
        exclusion_guards, _yhome_guard, _manual_exclusions = monthly_exclusion_guards(
            yhome_csv,
            policy_path=listing_policy,
        )

    def exclusion_for_name(name: object) -> dict[str, Any] | None:
        if not exclusion_guards or not callable(match_exclusion_guard):
            return None
        return match_exclusion_guard(Path(str(name or "")), exclusion_guards)

    for record in candidate.get("records") or []:
        if not isinstance(record, dict):
            continue
        for key in ("property_name", "managed_name", "input_property_name"):
            if record.get(key):
                name = str(record[key])
                guard = exclusion_for_name(name)
                if guard:
                    excluded_names.add(name)
                else:
                    active_names.add(name)
    for record in owner.get("property_checklist") or []:
        if not isinstance(record, dict):
            continue
        name = str(record.get("property_name") or "")
        if not name:
            continue
        if record.get("external_exclusion") is True or str(record.get("status") or "").startswith(("skipped", "excluded")):
            excluded_names.add(name)
        elif exclusion_for_name(name):
            excluded_names.add(name)
        else:
            active_names.add(name)
    return {
        "candidate_packet": str(candidate_packet),
        "candidate_packet_status": candidate.get("status"),
        "owner_review_gate": str(owner_review_gate),
        "owner_review_gate_status": owner.get("status"),
        "active_property_names": sorted(active_names),
        "excluded_property_names": sorted(excluded_names),
        "policy_excluded_property_names": sorted(
            {
                str(guard.get("property_name") or "").strip()
                for guard in exclusion_guards
                if str(guard.get("property_name") or "").strip()
            }
        ),
        "candidate_records": candidate.get("records") if isinstance(candidate.get("records"), list) else [],
    }


def classify_scope(property_name: str, scope: dict[str, Any]) -> str:
    if any(property_matches(property_name, name) for name in scope.get("active_property_names") or []):
        return "active_monthly_candidate"
    if any(property_matches(property_name, name) for name in scope.get("excluded_property_names") or []):
        return "excluded_or_inactive"
    return "unknown_or_legacy"


def matched_scope_name(property_name: str, scope: dict[str, Any], scope_key: str) -> str | None:
    for name in scope.get(scope_key) or []:
        if property_matches(property_name, name):
            return str(name)
    return None


def candidate_record_for(property_name: str, scope: dict[str, Any]) -> dict[str, Any]:
    for record in scope.get("candidate_records") or []:
        if not isinstance(record, dict):
            continue
        monthly_summary = record.get("monthly_financial_summary") if isinstance(record.get("monthly_financial_summary"), dict) else {}
        names = [
            record.get("property_name"),
            record.get("managed_name"),
            record.get("input_property_name"),
            monthly_summary.get("property_name"),
        ]
        if any(property_matches(property_name, name) for name in names if name):
            return record
    return {}


def candidate_financial_evidence(record: dict[str, Any]) -> dict[str, Any]:
    summary = record.get("monthly_financial_summary") if isinstance(record.get("monthly_financial_summary"), dict) else {}
    return {
        "financials_md": record.get("financials_md"),
        "financial_summary_source_mode": record.get("financial_summary_source_mode"),
        "eco_gl_column_e_status": summary.get("eco_gl_column_e_status"),
        "eco_gl_column_e_source_mode": summary.get("eco_gl_column_e_source_mode"),
        "eco_gl_column_e_row_count": summary.get("eco_gl_column_e_row_count"),
        "eco_gl_column_e_sum": summary.get("eco_gl_column_e_sum"),
        "eco_gl_column_e_source": summary.get("eco_gl_column_e_source"),
        "lofty_curr_maintenance_reserve": summary.get("lofty_curr_maintenance_reserve"),
        "candidate_issues": record.get("issues") if isinstance(record.get("issues"), list) else [],
    }


VALID_ZERO_ROW_DECISIONS = {"include_active_no_activity", "exclude_no_dao_activity"}


def load_zero_row_decisions(path: Path) -> dict[str, dict[str, Any]]:
    payload = read_json(path)
    records = payload.get("decisions") if isinstance(payload.get("decisions"), list) else []
    decisions: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        property_name = str(record.get("property") or record.get("property_name") or "").strip()
        decision = str(record.get("decision") or "").strip()
        reviewed = record.get("reviewed") is True
        if not property_name or not reviewed or decision not in VALID_ZERO_ROW_DECISIONS:
            continue
        decisions[normalize_property_name(property_name)] = {
            "zero_row_source_ledger_reviewed": True,
            "zero_row_source_ledger_decision": decision,
            "zero_row_source_ledger_decision_note": record.get("note"),
            "zero_row_source_ledger_decision_reviewed_at": record.get("reviewed_at"),
            "zero_row_source_ledger_decision_source": str(path),
        }
    return decisions


def zero_row_decision_for(property_name: str, matched_active_property: str | None, decisions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    names = [property_name, matched_active_property]
    for name in names:
        if not name:
            continue
        direct = decisions.get(normalize_property_name(name))
        if direct:
            return direct
    for decision_property, decision in decisions.items():
        if any(property_matches(name, decision_property) for name in names if name):
            return decision
    return {}


def unmatched_cf_action(property_name: str, scope_name: str, matched_active_property: str | None = None) -> str:
    if scope_name == "active_monthly_candidate" and matched_active_property:
        return (
            f"Active monthly candidate {matched_active_property} has a Cash Flow Statement but no matching Baselane GL property rows. "
            "Tag/import upstream Baselane transactions to this DAO/property before transfer reconciliation, or explicitly exclude it from the monthly candidate set if it has no DAO activity."
        )
    if scope_name == "active_monthly_candidate":
        return (
            "Active monthly candidate has a Cash Flow Statement but no matching Baselane GL property. "
            "Retag upstream Baselane transactions to this DAO/property or exclude the property from the monthly candidate set before transfer reconciliation."
        )
    return "Map this Cash Flow Statement workbook to a Baselane GL property or exclude it if inactive/sold."


def action_record(kind: str, property_name: str, action: str, **extra: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "property": property_name,
        "action": action,
        **{key: value for key, value in extra.items() if value not in (None, "", [])},
    }


def numeric_delta(actual: object, expected: object) -> float | None:
    try:
        return round(float(actual) - float(expected), 2)
    except (TypeError, ValueError):
        return None


def absolute_delta(action: dict[str, Any]) -> float:
    try:
        return abs(float(action.get("delta") or 0))
    except (TypeError, ValueError):
        return 0.0


def action_sort_key(action: dict[str, Any]) -> tuple[int, int, float, str]:
    scope_rank = {
        "active_monthly_candidate": 0,
        "unknown_or_legacy": 1,
        "excluded_or_inactive": 2,
    }
    kind_rank = {
        "source_cash_mismatch": 0,
        "unmatched_cf_workbook": 1,
        "split_scope_missing_source_cash": 2,
        "duplicate_cf_workbook_mapping": 3,
    }
    return (
        scope_rank.get(str(action.get("scope") or ""), 9),
        kind_rank.get(str(action.get("kind") or ""), 9),
        -absolute_delta(action),
        normalize_property_name(action.get("property")),
    )


def effective_action_scope(action: dict[str, Any]) -> str:
    evidence = action.get("candidate_financial_evidence") if isinstance(action.get("candidate_financial_evidence"), dict) else {}
    if (
        action.get("scope") == "active_monthly_candidate"
        and action.get("kind") == "duplicate_cf_workbook_mapping"
        and str(action.get("selected") or "").strip()
    ):
        return "active_monthly_candidate_canonical_selected_duplicate_hygiene"
    if (
        action.get("scope") == "active_monthly_candidate"
        and action.get("kind") == "unmatched_cf_workbook"
        and evidence.get("eco_gl_column_e_source_mode") == "source_ledger_zero_rows"
        and evidence.get("zero_row_source_ledger_reviewed") is True
        and evidence.get("zero_row_source_ledger_decision") == "include_active_no_activity"
    ):
        return "active_monthly_candidate_reviewed_no_activity"
    return str(action.get("scope") or "unknown_or_legacy")


def build_report(
    *,
    source_cash_report: Path,
    weekly_cf_report: Path,
    candidate_packet: Path,
    owner_review_gate: Path,
    zero_row_decisions: Path = DEFAULT_ZERO_ROW_DECISIONS,
    yhome_csv: Path | None = None,
    listing_policy: Path | None = None,
    report_path: Path,
    markdown_path: Path,
) -> dict[str, Any]:
    source_cash = read_json(source_cash_report)
    weekly_cf = read_json(weekly_cf_report)
    scope = load_scope(
        candidate_packet,
        owner_review_gate,
        yhome_csv=yhome_csv,
        listing_policy=listing_policy,
    )
    zero_row_decision_index = load_zero_row_decisions(zero_row_decisions)
    actions: list[dict[str, Any]] = []

    for violation in source_cash.get("violations_bounded") or []:
        if not isinstance(violation, dict):
            continue
        actions.append(
            action_record(
                "source_cash_mismatch",
                str(violation.get("property") or "Unknown"),
                "Refresh the Cash Flow Statement ECO GL Net Cash Balance from current Baselane GL, then rerun source-cash audit.",
                file=violation.get("file"),
                expected=violation.get("expected"),
                actual=violation.get("actual"),
                delta=numeric_delta(violation.get("actual"), violation.get("expected")),
                scope=classify_scope(str(violation.get("property") or "Unknown"), scope),
            )
        )

    for item in source_cash.get("no_match_properties_bounded") or []:
        if not isinstance(item, dict):
            continue
        property_name = str(item.get("property") or "Unknown")
        scope_name = classify_scope(property_name, scope)
        matched_active_property = matched_scope_name(property_name, scope, "active_property_names")
        candidate_record = candidate_record_for(property_name, scope)
        evidence = candidate_financial_evidence(candidate_record) if candidate_record else {}
        if evidence.get("eco_gl_column_e_source_mode") == "source_ledger_zero_rows":
            evidence = {
                **evidence,
                **zero_row_decision_for(property_name, matched_active_property, zero_row_decision_index),
            }
        actions.append(
            action_record(
                "unmatched_cf_workbook",
                property_name,
                unmatched_cf_action(property_name, scope_name, matched_active_property),
                file=item.get("file"),
                matched_active_property=matched_active_property,
                matched_excluded_property=matched_scope_name(property_name, scope, "excluded_property_names"),
                candidate_financial_evidence=evidence if evidence else None,
                scope=scope_name,
            )
        )

    for property_name in source_cash.get("split_scope_missing_properties_bounded") or []:
        actions.append(
            action_record(
                "split_scope_missing_source_cash",
                str(property_name),
                "Create or map canonical Cash Flow Statement source-cash coverage for this split-current GL property.",
                scope=classify_scope(str(property_name), scope),
            )
        )

    for item in source_cash.get("duplicate_checked_properties_bounded") or []:
        if not isinstance(item, dict):
            continue
        resolution = item.get("resolution") if isinstance(item.get("resolution"), dict) else {}
        if resolution.get("selected") and resolution.get("reason") == "public_workbook_preferred":
            continue
        actions.append(
            action_record(
                "duplicate_cf_workbook_mapping",
                str(item.get("property") or "Unknown"),
                str(resolution.get("action") or "Resolve duplicate Cash Flow Statement workbooks so exactly one canonical workbook feeds source-cash per DAO."),
                workbook_count=item.get("workbook_count"),
                files=item.get("files"),
                selected=resolution.get("selected"),
                ignored=resolution.get("ignored"),
                reason=resolution.get("reason"),
                scope=classify_scope(str(item.get("property") or "Unknown"), scope),
            )
        )

    kind_counts: dict[str, int] = {}
    scope_counts: dict[str, int] = {}
    for action in actions:
        kind_counts[action["kind"]] = kind_counts.get(action["kind"], 0) + 1
        action["effective_scope"] = effective_action_scope(action)
        scope_counts[action["effective_scope"]] = scope_counts.get(action["effective_scope"], 0) + 1
    sorted_actions = sorted(actions, key=action_sort_key)
    active_actions = [action for action in sorted_actions if action.get("effective_scope") == "active_monthly_candidate"]
    status = "review" if active_actions else "ok"
    active_mismatches = [action for action in active_actions if action.get("kind") == "source_cash_mismatch"]
    active_abs_delta_total = round(sum(absolute_delta(action) for action in active_mismatches), 2)
    largest_active_mismatches = sorted(
        active_mismatches,
        key=lambda action: (-absolute_delta(action), normalize_property_name(action.get("property"))),
    )[:10]

    return {
        "generated_at": iso_z(),
        "status": status,
        "job": "baselane-source-cash-reconciliation-actions",
        "source_cash_report": str(source_cash_report),
        "source_cash_report_status": source_cash.get("status"),
        "source_cash_report_generated_at": source_cash.get("generated_at"),
        "source_cash_report_digest": sha256_file(source_cash_report),
        "weekly_cf_report": str(weekly_cf_report),
        "weekly_cf_report_status": weekly_cf.get("status"),
        "candidate_packet": str(candidate_packet),
        "candidate_packet_status": scope.get("candidate_packet_status"),
        "owner_review_gate": str(owner_review_gate),
        "owner_review_gate_status": scope.get("owner_review_gate_status"),
        "yhome_csv": str(yhome_csv) if yhome_csv else None,
        "listing_policy": str(listing_policy) if listing_policy else None,
        "policy_excluded_property_names": scope.get("policy_excluded_property_names", []),
        "zero_row_decisions": str(zero_row_decisions),
        "zero_row_decision_count": len(zero_row_decision_index),
        "report": str(report_path),
        "markdown": str(markdown_path),
        "action_count": len(actions),
        "action_kind_counts": dict(sorted(kind_counts.items())),
        "action_scope_counts": dict(sorted(scope_counts.items())),
        "active_monthly_candidate_action_count": scope_counts.get("active_monthly_candidate", 0),
        "nonblocking_action_count": len(actions) - scope_counts.get("active_monthly_candidate", 0),
        "active_monthly_candidate_source_cash_mismatch_count": len(active_mismatches),
        "active_monthly_candidate_source_cash_abs_delta_total": active_abs_delta_total,
        "largest_active_monthly_candidate_mismatches_bounded": largest_active_mismatches,
        "source_cash_violation_count": count(source_cash.get("violation_count")),
        "source_cash_no_match_count": count(source_cash.get("no_match_count")),
        "source_cash_split_scope_missing_property_count": count(source_cash.get("split_scope_missing_property_count")),
        "source_cash_duplicate_checked_property_count": count(source_cash.get("duplicate_checked_property_count")),
        "active_monthly_candidate_actions_bounded": active_actions[:100],
        "actions_bounded": sorted_actions[:100],
        "next_action": (
            "Resolve source-cash reconciliation actions, rerun daily source-cash audit, weekly CF summary, transfer reconciliation, then monthly readiness."
            if active_actions
            else "No active monthly source-cash actions block close; non-active/legacy actions remain informational."
            if actions
            else "Source-cash reconciliation action report is clear."
        ),
    }


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Baselane Source-Cash Reconciliation Actions",
        "",
        f"- Status: `{report['status']}`",
        f"- Action count: `{report['action_count']}`",
        f"- Active monthly candidate action count: `{report['active_monthly_candidate_action_count']}`",
        f"- Nonblocking action count: `{report.get('nonblocking_action_count', 0)}`",
        f"- Active source-cash mismatch count: `{report.get('active_monthly_candidate_source_cash_mismatch_count', 0)}`",
        f"- Active source-cash absolute delta total: `{report.get('active_monthly_candidate_source_cash_abs_delta_total', 0)}`",
        f"- Source-cash report: `{report['source_cash_report']}`",
        "",
    ]
    if report["action_kind_counts"]:
        lines.append("## Counts")
        for kind, value in report["action_kind_counts"].items():
            lines.append(f"- `{kind}`: `{value}`")
        for scope, value in report.get("action_scope_counts", {}).items():
            lines.append(f"- scope `{scope}`: `{value}`")
        lines.append("")
    if report.get("largest_active_monthly_candidate_mismatches_bounded"):
        lines.append("## Largest Active Source-Cash Mismatches")
        for action in report["largest_active_monthly_candidate_mismatches_bounded"]:
            lines.append(
                f"- {action['property']}: delta `{action.get('delta')}`, expected `{action.get('expected')}`, actual `{action.get('actual')}`, file `{action.get('file')}`"
            )
        lines.append("")
    if report.get("active_monthly_candidate_actions_bounded"):
        lines.append("## Active Monthly Candidate Actions")
        for action in report["active_monthly_candidate_actions_bounded"]:
            lines.append(f"- `{action['kind']}` — {action['property']}: {action['action']}")
            evidence = action.get("candidate_financial_evidence") if isinstance(action.get("candidate_financial_evidence"), dict) else {}
            if evidence:
                lines.append(
                    "  - candidate FINANCIALS evidence: "
                    f"ECO rows `{evidence.get('eco_gl_column_e_row_count')}`, "
                    f"ECO sum `{evidence.get('eco_gl_column_e_sum')}`, "
                    f"source mode `{evidence.get('eco_gl_column_e_source_mode')}`, "
                    f"financials `{evidence.get('financials_md')}`"
                )
        lines.append("")
    if report["actions_bounded"]:
        lines.append("## Actions")
        for action in report["actions_bounded"]:
            lines.append(f"- `{action['kind']}` / `{action.get('scope')}` — {action['property']}: {action['action']}")
        lines.append("")
    lines.append(f"Next action: {report['next_action']}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_csv(path: Path, report: dict[str, Any]) -> None:
    fieldnames = [
        "scope",
        "kind",
        "property",
        "delta",
        "expected",
        "actual",
        "matched_active_property",
        "matched_excluded_property",
        "candidate_eco_gl_column_e_row_count",
        "candidate_eco_gl_column_e_sum",
        "candidate_eco_gl_column_e_source_mode",
        "candidate_financials_md",
        "file",
        "action",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for action in report.get("actions_bounded") or []:
            row = {field: action.get(field, "") for field in fieldnames}
            evidence = action.get("candidate_financial_evidence") if isinstance(action.get("candidate_financial_evidence"), dict) else {}
            row["candidate_eco_gl_column_e_row_count"] = evidence.get("eco_gl_column_e_row_count", "")
            row["candidate_eco_gl_column_e_sum"] = evidence.get("eco_gl_column_e_sum", "")
            row["candidate_eco_gl_column_e_source_mode"] = evidence.get("eco_gl_column_e_source_mode", "")
            row["candidate_financials_md"] = evidence.get("financials_md", "")
            writer.writerow(row)
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build actionable source-cash reconciliation report.")
    parser.add_argument("--source-cash-report", type=Path, default=DEFAULT_SOURCE_CASH_REPORT)
    parser.add_argument("--weekly-cf-report", type=Path, default=DEFAULT_WEEKLY_CF_REPORT)
    parser.add_argument("--candidate-packet", type=Path, default=DEFAULT_CANDIDATE_PACKET)
    parser.add_argument("--owner-review-gate", type=Path, default=DEFAULT_OWNER_REVIEW_GATE)
    parser.add_argument("--zero-row-decisions", type=Path, default=DEFAULT_ZERO_ROW_DECISIONS)
    parser.add_argument("--yhome-csv", type=Path, default=DEFAULT_YHOME_CSV)
    parser.add_argument("--listing-policy", type=Path, default=DEFAULT_LISTING_POLICY)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    args = parser.parse_args()

    report = build_report(
        source_cash_report=args.source_cash_report,
        weekly_cf_report=args.weekly_cf_report,
        candidate_packet=args.candidate_packet,
        owner_review_gate=args.owner_review_gate,
        zero_row_decisions=args.zero_row_decisions,
        yhome_csv=args.yhome_csv,
        listing_policy=args.listing_policy,
        report_path=args.report,
        markdown_path=args.markdown,
    )
    write_json(args.report, report)
    write_markdown(args.markdown, report)
    write_csv(args.csv, report)
    print(json.dumps({key: report[key] for key in ("status", "action_count", "action_kind_counts")}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
