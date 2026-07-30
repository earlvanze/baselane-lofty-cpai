#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from baselane_first_day_pm_fee_audit import default_gl_csv, is_first_day_pm_fee_row, iso_z, parse_date


APPLY_ENV = "BASELANE_FIRST_DAY_PM_FEE_SOURCE_CLEANUP_APPLY"


ACTION_FIELDS = [
    "id",
    "action_type",
    "review_status",
    "source_row",
    "date",
    "property",
    "amount",
    "merchant",
    "description",
    "category",
    "source_month",
    "reason",
    "notes",
]


def stable_digest(payload: object) -> str:
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def text(value: object) -> str:
    return "" if value is None else str(value)


def action_id(source_file: Path, line_number: int, row: dict[str, str]) -> str:
    return stable_digest(
        {
            "source_file": str(source_file),
            "line_number": line_number,
            "date": row.get("Date"),
            "property": row.get("Property"),
            "amount": row.get("Amount"),
            "notes": row.get("Notes"),
        }
    )[:16]


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        if not fieldnames:
            raise ValueError(f"Ledger has no CSV header: {path}")
        return fieldnames, list(reader)


def write_rows(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def build_plan(gl_csv: Path, target_month: str | None, limit: int = 25) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    month_counts: Counter[str] = Counter()
    property_counts: Counter[str] = Counter()
    amount_total = 0.0
    parse_error_count = 0
    source_row_count = 0
    scope = "all_months" if target_month is None else "single_month"

    if not gl_csv.is_file():
        return {
            "generated_at": iso_z(),
            "status": "missing",
            "month": target_month or "all",
            "scope": scope,
            "source_file": str(gl_csv),
            "action_count": 0,
            "actions_bounded": [],
            "error": "ledger CSV missing",
        }

    with gl_csv.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for line_number, row in enumerate(reader, start=2):
            source_row_count += 1
            parsed = parse_date(row.get("Date") or "")
            if row.get("Date") and parsed is None:
                parse_error_count += 1
            if not is_first_day_pm_fee_row(row, target_month):
                continue
            month_key = parsed.strftime("%Y-%m") if parsed is not None else "unknown"
            amount_text = text(row.get("Amount")).replace(",", "").strip()
            try:
                amount = float(amount_text)
            except ValueError:
                amount = 0.0
            amount_total += amount
            property_name = text(row.get("Property")).strip() or "unknown"
            month_counts[month_key] += 1
            property_counts[property_name] += 1
            actions.append(
                {
                    "id": action_id(gl_csv, line_number, row),
                    "action_type": "delete_or_disable_first_day_pm_fee_source_row",
                    "review_status": "needs_source_cleanup",
                    "source_row": line_number,
                    "date": row.get("Date") or "",
                    "property": property_name,
                    "amount": row.get("Amount") or "",
                    "merchant": row.get("Merchant") or "",
                    "description": row.get("Description") or "",
                    "category": row.get("Category") or "",
                    "source_month": month_key,
                    "reason": "1st-day AOPS-PM-FEE source row contaminates PM-fee reporting; disable/delete source row before investor reporting.",
                    "notes": row.get("Notes") or "",
                }
            )

    return {
        "generated_at": iso_z(),
        "status": "review" if actions else "ok",
        "month": target_month or "all",
        "scope": scope,
        "source_file": str(gl_csv),
        "source_row_count": source_row_count,
        "action_count": len(actions),
        "action_type_counts": {"delete_or_disable_first_day_pm_fee_source_row": len(actions)} if actions else {},
        "month_counts": dict(sorted(month_counts.items())),
        "property_counts": dict(sorted(property_counts.items())),
        "amount_total": round(amount_total, 2),
        "parse_error_count": parse_error_count,
        "mutation_mode": "plan_only",
        "raw_source_mutated": False,
        "live_baselane_mutated": False,
        "local_source_write_allowed": False,
        "baselane_source_write_allowed": False,
        "cleanup_command_after_review": f"{APPLY_ENV}=1 bash scripts/baselane_first_day_pm_fee_cleanup_then_refresh.sh",
        "idempotency_digest": stable_digest({"source_file": str(gl_csv), "actions": actions}),
        "actions_bounded": actions[:limit],
        "bounded": len(actions) > limit,
        "artifacts": {
            "actions_csv": str(Path(__file__).absolute().parents[1] / "reports" / "baselane_first_day_pm_fee_source_cleanup_actions.csv"),
            "markdown": str(Path(__file__).absolute().parents[1] / "reports" / "baselane_first_day_pm_fee_source_cleanup_plan.md"),
        },
        "policy": (
            "Plan-only by default. Apply mode is local-ledger cleanup only, requires "
            f"`--apply` plus `{APPLY_ENV}=1`, writes a backup first, and never mutates live Baselane."
        ),
        "next_actions": [
            f"After source-owner approval, run `{APPLY_ENV}=1 bash scripts/baselane_first_day_pm_fee_cleanup_then_refresh.sh` to delete the exact 1st-day AOPS-PM-FEE source rows from the local ECO GL CSV with backup, then refresh daily/weekly/monthly evidence.",
        ]
        if actions
        else ["No first-day PM-fee source cleanup actions remain."],
        "actions": actions,
    }


def apply_cleanup(gl_csv: Path, plan: dict[str, Any], backup_dir: Path) -> dict[str, Any]:
    if plan.get("action_count", 0) <= 0:
        return {
            "status": "ok",
            "mode": "noop",
            "mutated": False,
            "deleted_row_count": 0,
            "backup_file": None,
            "reason": "no cleanup actions remain",
        }
    fieldnames, rows = read_rows(gl_csv)
    action_ids = {str(action.get("id")) for action in plan.get("actions") or [] if action.get("id")}
    kept_rows: list[dict[str, str]] = []
    deleted_rows: list[dict[str, str]] = []
    deleted_details: list[dict[str, Any]] = []
    for line_number, row in enumerate(rows, start=2):
        current_id = action_id(gl_csv, line_number, row)
        if current_id in action_ids and is_first_day_pm_fee_row(row, None):
            deleted_rows.append(dict(row))
            deleted_details.append(
                {
                    "id": current_id,
                    "source_row": line_number,
                    "date": row.get("Date") or "",
                    "property": row.get("Property") or "",
                    "amount": row.get("Amount") or "",
                    "merchant": row.get("Merchant") or "",
                }
            )
            continue
        kept_rows.append(dict(row))
    missing_action_count = len(action_ids) - len(deleted_rows)
    if missing_action_count:
        return {
            "status": "blocked_stale_plan",
            "mode": "apply",
            "mutated": False,
            "deleted_row_count": len(deleted_rows),
            "expected_action_count": len(action_ids),
            "missing_action_count": missing_action_count,
            "reason": "not all planned action IDs still match source rows; rebuild the plan before applying",
        }
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_file = backup_dir / f"{gl_csv.name}.{timestamp}.{stable_digest({'deleted': deleted_details})[:12]}.bak.csv"
    write_rows(backup_file, fieldnames, rows)
    write_rows(gl_csv, fieldnames, kept_rows)
    return {
        "status": "ok",
        "mode": "apply",
        "mutated": True,
        "deleted_row_count": len(deleted_rows),
        "expected_action_count": len(action_ids),
        "missing_action_count": 0,
        "backup_file": str(backup_file),
        "applied_actions_bounded": deleted_details[:25],
        "bounded": len(deleted_details) > 25,
        "pre_row_count": len(rows),
        "post_row_count": len(kept_rows),
        "deleted_rows_digest": stable_digest({"deleted": deleted_rows}),
    }


def write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_csv(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ACTION_FIELDS)
        writer.writeheader()
        for action in report.get("actions") or []:
            writer.writerow({field: action.get(field, "") for field in ACTION_FIELDS})
    tmp.replace(path)


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Baselane 1st-Day PM Fee Source Cleanup Plan",
        "",
        f"- Status: `{report['status']}`",
        f"- Mutation mode: `{report['mutation_mode']}`",
        f"- Raw source mutated: `{str(report['raw_source_mutated']).lower()}`",
        f"- Cleanup actions: `{report['action_count']}`",
        f"- Amount total: `{report.get('amount_total')}`",
        f"- Digest: `{report.get('idempotency_digest')}`",
        f"- Source: `{report.get('source_file')}`",
        "",
        "## Policy",
        "",
        report["policy"],
        "",
        "## Next Actions",
    ]
    for item in report.get("next_actions") or []:
        lines.append(f"- {item}")
    lines.extend(["", "## Month Counts"])
    for month, count in (report.get("month_counts") or {}).items():
        lines.append(f"- `{month}`: {count}")
    if not report.get("month_counts"):
        lines.append("- None")
    sample = (report.get("actions") or [])[:12]
    if sample:
        lines.extend(["", "## First Actions"])
        for action in sample:
            lines.append(
                f"- row `{action['source_row']}` — {action['date']} — {action['property']} — "
                f"{action['amount']} — `{action['action_type']}`"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a non-mutating cleanup plan for first-day AOPS PM-fee source rows.")
    parser.add_argument("--gl-csv", type=Path, default=default_gl_csv())
    parser.add_argument("--month", default=datetime.now().strftime("%Y-%m"))
    parser.add_argument("--all-months", action="store_true")
    parser.add_argument("--report", type=Path, default=Path(__file__).absolute().parents[1] / "reports" / "baselane_first_day_pm_fee_source_cleanup_plan.json")
    parser.add_argument("--actions-csv", type=Path, default=Path(__file__).absolute().parents[1] / "reports" / "baselane_first_day_pm_fee_source_cleanup_actions.csv")
    parser.add_argument("--markdown", type=Path, default=Path(__file__).absolute().parents[1] / "reports" / "baselane_first_day_pm_fee_source_cleanup_plan.md")
    parser.add_argument("--backup-dir", type=Path, default=Path(__file__).absolute().parents[1] / "reports" / "source-cleanup-backups")
    parser.add_argument("--apply", action="store_true", help=f"Rewrite the local ECO GL CSV after backup; requires {APPLY_ENV}=1.")
    args = parser.parse_args(argv)
    target_month = None if args.all_months else args.month
    report = build_plan(args.gl_csv, target_month)
    report["artifacts"] = {
        "actions_csv": str(args.actions_csv),
        "markdown": str(args.markdown),
    }
    return_code = 0
    if args.apply:
        if os.environ.get(APPLY_ENV) != "1":
            report["cleanup_apply"] = {
                "status": "blocked_env_not_set",
                "mutated": False,
                "required_env": APPLY_ENV,
                "reason": f"Set {APPLY_ENV}=1 with --apply to mutate the local ECO GL CSV after backup.",
            }
            report["status"] = "review"
            report["mutation_mode"] = "apply_blocked"
            return_code = 2
        else:
            pre_report = report
            apply_result = apply_cleanup(args.gl_csv, pre_report, args.backup_dir)
            post_report = build_plan(args.gl_csv, target_month)
            post_report["artifacts"] = report["artifacts"]
            post_report["cleanup_apply"] = apply_result
            post_report["pre_cleanup_action_count"] = pre_report.get("action_count", 0)
            post_report["pre_cleanup_digest"] = pre_report.get("idempotency_digest")
            post_report["mutation_mode"] = "apply"
            post_report["raw_source_mutated"] = apply_result.get("mutated") is True
            post_report["live_baselane_mutated"] = False
            post_report["local_source_write_allowed"] = True
            post_report["baselane_source_write_allowed"] = False
            report = post_report
            return_code = 0 if apply_result.get("status") == "ok" and report.get("status") == "ok" else 2
    write_json(args.report, report)
    write_csv(args.actions_csv, report)
    write_markdown(args.markdown, report)
    print(json.dumps({key: report[key] for key in ("status", "month", "action_count")}, indent=2, sort_keys=True))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
