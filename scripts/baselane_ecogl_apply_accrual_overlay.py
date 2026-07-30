#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


ACTION_FIELDS = [
    "id",
    "property",
    "month",
    "date",
    "amount",
    "baselane_category",
    "label",
    "source_file",
    "source_row",
    "status",
    "reason",
]
REQUIRED_LEDGER_FIELDS = ["Date", "Amount", "Property", "Merchant", "Description", "Category", "Notes"]
OVERLAY_MERCHANT = "ECO GL Accrual Overlay"
OVERLAY_ACCOUNT = "ECO Systems, LLC-ECO Systems Accrual Overlay"


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_digest(payload: Any) -> str:
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return data


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        missing = [field for field in REQUIRED_LEDGER_FIELDS if field not in fieldnames]
        if missing:
            raise ValueError(f"Ledger missing required fields: {', '.join(missing)}")
        return fieldnames, list(reader)


def month_end_label(month: str) -> str:
    year, month_number = [int(part) for part in month.split("-", 1)]
    if month_number == 12:
        last_day = date(year, 12, 31)
    else:
        last_day = date(year, month_number + 1, 1).replace(day=1)
        last_day = date.fromordinal(last_day.toordinal() - 1)
    return last_day.strftime("%B %d, %Y")


def overlay_id(action: dict[str, Any]) -> str:
    return stable_digest(
        {
            "source_action_id": action.get("id"),
            "property": action.get("property"),
            "month": action.get("month"),
            "amount": action.get("amount"),
            "category": action.get("baselane_category"),
            "label": action.get("label"),
            "source_file": action.get("source_file"),
            "source_row": action.get("source_row"),
        }
    )[:16]


def eligible_actions(source_fix: dict[str, Any]) -> list[dict[str, Any]]:
    actions = []
    for action in source_fix.get("actions") or []:
        if not isinstance(action, dict):
            continue
        if action.get("action_type") != "book_or_tag_baselane_accrual":
            continue
        if action.get("automation_status") != "ready_for_baselane_source_fix":
            continue
        if str(action.get("baselane_category") or "").strip() == "Management Fees":
            continue
        label_text = " ".join(
            [
                str(action.get("label") or ""),
                str(action.get("merchant") or ""),
                str(action.get("description") or ""),
            ]
        ).lower()
        if "management fee" in label_text or "pm fee" in label_text:
            continue
        # Insurance from OSC Risk Secure (Obie) for OH/IL/TN properties does not need accrual
        source_file = str(action.get("source_file") or "").lower()
        is_oh_il_tn = any(f"/{state}/" in source_file for state in ["oh", "il", "tn"])
        is_obie_insurance = (
            str(action.get("baselane_category") or "").strip() == "Insurance"
            and ("obie" in label_text or "osc risk secure" in label_text)
        )
        if is_oh_il_tn and is_obie_insurance:
            continue
        if not str(action.get("property") or "").strip():
            continue
        if not str(action.get("baselane_category") or "").strip():
            continue
        if not str(action.get("amount") or "").strip():
            continue
        actions.append(action)
    return actions


def build_overlay_row(fieldnames: list[str], action: dict[str, Any], action_id: str, month: str) -> dict[str, str]:
    label = str(action.get("label") or action.get("baselane_category") or "").strip()
    source_file = str(action.get("source_file") or "").strip()
    source_row = str(action.get("source_row") or "").strip()
    notes = (
        f"ecogl_accrual_overlay_id={action_id}; source=cf_statement; month={month}; "
        f"source_file={source_file}; source_row={source_row}"
    )
    row = {field: "" for field in fieldnames}
    row.update(
        {
            "Account": OVERLAY_ACCOUNT,
            "Date": month_end_label(month),
            "Merchant": OVERLAY_MERCHANT,
            "Description": f"CF accrual overlay: {label}",
            "Amount": str(action.get("amount") or ""),
            "Type": "Accrual Overlay",
            "Category": str(action.get("baselane_category") or ""),
            "Sub-category": "",
            "Property": str(action.get("property") or ""),
            "Unit": "",
            "Notes": notes,
        }
    )
    return row


def build_report(ledger: Path, source_fix_plan: Path, out_ledger: Path, apply: bool) -> dict[str, Any]:
    fieldnames, rows = read_rows(ledger)
    source_fix = read_json(source_fix_plan)
    actions = eligible_actions(source_fix)
    raw_ready_actions = [
        action
        for action in source_fix.get("actions") or []
        if isinstance(action, dict)
        and action.get("action_type") == "book_or_tag_baselane_accrual"
        and action.get("automation_status") == "ready_for_baselane_source_fix"
    ]
    skipped_management_fee_actions = [
        action
        for action in raw_ready_actions
        if str(action.get("baselane_category") or "").strip() == "Management Fees"
        or "management fee"
        in " ".join([str(action.get("label") or ""), str(action.get("merchant") or ""), str(action.get("description") or "")]).lower()
        or "pm fee"
        in " ".join([str(action.get("label") or ""), str(action.get("merchant") or ""), str(action.get("description") or "")]).lower()
    ]
    existing_ids = set()
    for row in rows:
        notes = str(row.get("Notes") or "")
        marker = "ecogl_accrual_overlay_id="
        if marker in notes:
            existing_ids.add(notes.split(marker, 1)[1].split(";", 1)[0].strip())

    output_rows = [dict(row) for row in rows]
    applied_actions: list[dict[str, Any]] = []
    skipped_existing = 0
    month = str(source_fix.get("source_month") or "")
    for action in actions:
        action_month = str(action.get("month") or month)
        action_id = overlay_id(action)
        action_record = {
            "id": action_id,
            "property": action.get("property"),
            "month": action_month,
            "date": month_end_label(action_month),
            "amount": action.get("amount"),
            "baselane_category": action.get("baselane_category"),
            "label": action.get("label"),
            "source_file": action.get("source_file"),
            "source_row": action.get("source_row"),
            "status": "already_present" if action_id in existing_ids else "appended",
            "reason": "derived_reporting_ledger_accrual_from_cash_flow_statement",
        }
        if action_id in existing_ids:
            skipped_existing += 1
        else:
            output_rows.append(build_overlay_row(fieldnames, action, action_id, action_month))
            existing_ids.add(action_id)
        applied_actions.append(action_record)

    category_counts = Counter(str(action.get("baselane_category") or "") for action in applied_actions)
    input_digest = stable_digest({"fieldnames": fieldnames, "rows": rows})
    output_digest = stable_digest({"fieldnames": fieldnames, "rows": output_rows})
    actions_digest = stable_digest({"actions": applied_actions})
    if apply:
        out_ledger.parent.mkdir(parents=True, exist_ok=True)
        with out_ledger.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(output_rows)
    return {
        "status": "ok",
        "generated_at": iso_z(),
        "mode": "apply" if apply else "dry_run",
        "ledger": str(ledger),
        "source_fix_plan": str(source_fix_plan),
        "out_ledger": str(out_ledger),
        "source_month": source_fix.get("source_month"),
        "input_row_count": len(rows),
        "output_row_count": len(output_rows),
        "eligible_action_count": len(actions),
        "blocked_management_fee_action_count": len(skipped_management_fee_actions),
        "appended_action_count": sum(1 for action in applied_actions if action["status"] == "appended"),
        "already_present_action_count": skipped_existing,
        "category_counts": dict(sorted(category_counts.items())),
        "input_digest": input_digest,
        "output_digest": output_digest,
        "actions_digest": actions_digest,
        "output_written": bool(apply),
        "policy": "Append only GL-empty CF accruals to the derived reporting ledger; raw Baselane export remains untouched.",
        "actions": applied_actions,
    }


def write_actions_csv(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ACTION_FIELDS)
        writer.writeheader()
        for action in report.get("actions") or []:
            writer.writerow({field: action.get(field, "") for field in ACTION_FIELDS})


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        f"# ECO GL Accrual Overlay — {report.get('source_month') or 'unknown month'}",
        "",
        f"- Status: `{report['status']}`",
        f"- Mode: `{report['mode']}`",
        f"- Eligible actions: `{report['eligible_action_count']}`",
        f"- Appended actions: `{report['appended_action_count']}`",
        f"- Already present: `{report['already_present_action_count']}`",
        f"- Output written: `{report['output_written']}`",
        f"- Actions digest: `{report['actions_digest']}`",
        "",
        "## Policy",
        "",
        f"- {report['policy']}",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Append deterministic CF accrual overlay rows to a derived ECO GL reporting ledger.")
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--source-fix-plan", type=Path, required=True)
    parser.add_argument("--out-ledger", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--actions-csv", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    report = build_report(args.ledger, args.source_fix_plan, args.out_ledger, args.apply)
    write_actions_csv(report, args.actions_csv)
    write_markdown(report, args.markdown)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ["status", "eligible_action_count", "appended_action_count", "actions_digest"]}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
