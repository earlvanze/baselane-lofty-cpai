#!/usr/bin/env python3
"""Add and maintain an auditable ECO Net DAO Funds breakdown in Yhome.

The existing sheet is property-per-row, so the breakdown is represented by
adjacent columns immediately before ``ECO Net DAO Funds``.  The net cell is a
formula, never a copied number:

    gross ECO custody - unpaid accrued obligations - other restrictions
    + negative-only Yhome cash settlement adjustment

Default mode is preview-only.  Apply requires the preview digest, ``--apply``,
and ``YHOME_GSHEET_WRITE_ENABLED=1``.  Every apply is read back in formula and
unformatted-value modes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import subprocess
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


SPREADSHEET_ID = "1HerPv9U7IB47ipCpJ-XshajQWouCUEwfDdkHSfVCwfc"
TABS = {
    "Cleveland": 1187056671,
    "Chicago & non-Yhome": 433920866,
    "Yhome Deeded & Sold": 1902489452,
}
BREAKDOWN_HEADERS = (
    "ECO-held DAO Cash (Gross)",
    "Less: Accrued but Unpaid Obligations",
    "Less: Other ECO-held Restrictions",
    "Yhome Cash Settlement Adjustment",
)
NET_HEADER = "ECO Net DAO Funds"
PAYABLE_HEADER = "DAO A/P - Due to ECO for Verified Advances"
ROOT = Path(__file__).resolve().parents[1]


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def money(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")


def number(value: Any) -> float:
    return float(money(value))


def letters(column_number: int) -> str:
    result = ""
    while column_number:
        column_number, remainder = divmod(column_number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def latest_audit(path: Path | None, source_index: Path) -> Path:
    if path:
        return path
    candidates = sorted((ROOT / "reports").glob("yhome_all_property_eco_cash_audit*.json"), key=lambda item: item.stat().st_mtime)
    expected = source_index.resolve()
    for candidate in reversed(candidates):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            observed = Path(str((payload.get("sources") or {}).get("source_index") or "")).resolve()
        except Exception:
            continue
        if observed == expected:
            return candidate
    raise FileNotFoundError(f"no Yhome ECO-cash audit was generated from {expected}")


def source_index_issues(path: Path) -> list[dict[str, Any]]:
    """Reject simplified exports that omit the account custody dimension."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = set(reader.fieldnames or [])
        account_rows = sum(1 for row in reader if str(row.get("Account") or "").strip())
    issues = []
    required = {"Account", "Property", "Amount", "Notes"}
    if not required.issubset(headers):
        issues.append({"type": "source_index_missing_fields", "missing": sorted(required - headers)})
    if account_rows == 0:
        issues.append({"type": "source_index_has_no_account_custody_rows"})
    return issues


def load_policy() -> Any:
    path = ROOT / "scripts" / "coownership_reserve_policy.py"
    spec = importlib.util.spec_from_file_location("coownership_reserve_policy", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def gws_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("GOOGLE_WORKSPACE_CLI_CONFIG_DIR", "/home/digit/.openclaw/gws")
    env.setdefault("GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND", "file")
    return env


def run(command: list[str], timeout: float) -> dict[str, Any]:
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout, env=gws_env())
    if completed.returncode:
        raise RuntimeError(f"command failed rc={completed.returncode}: {completed.stderr.strip()[:1000]}")
    return json.loads(completed.stdout or "{}")


def read_live(args: argparse.Namespace, render: str) -> dict[str, list[list[Any]]]:
    payload = run(
        [
            args.gws_bin,
            "sheets", "spreadsheets", "values", "batchGet",
            "--params",
            json.dumps(
                {
                    "spreadsheetId": args.spreadsheet_id,
                    "ranges": [f"'{title}'!A1:AZ200" for title in TABS],
                    "valueRenderOption": render,
                },
                separators=(",", ":"),
            ),
            "--format", "json",
        ],
        args.timeout_seconds,
    )
    ranges = payload.get("valueRanges") or []
    if len(ranges) != len(TABS):
        raise RuntimeError(f"incomplete sheet readback: {len(ranges)}/{len(TABS)} tabs")
    return {title: (ranges[index].get("values") or []) for index, title in enumerate(TABS)}


def normalized_property(value: Any) -> str:
    return "".join(character for character in str(value or "").lower() if character.isalnum())


def load_live_cash_rows(path: Path | None) -> dict[str, dict[str, Any]]:
    if not path:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("status") != "ok" or payload.get("issues"):
        raise ValueError(f"live DAO cash report is not clean: {path}")
    indexed = {
        normalized_property(item.get("property")): item
        for item in payload.get("properties") or []
        if item.get("property")
    }
    for item in payload.get("intercompany_subledger") or []:
        key = normalized_property(item.get("property"))
        if not key:
            continue
        indexed.setdefault(key, {"property": item.get("property")}).update(
            {
                "dao_accounts_payable_to_eco": item.get("dao_accounts_payable_to_eco"),
                "eco_accounts_receivable_from_dao": item.get("eco_accounts_receivable_from_dao"),
                "intercompany_payable_status": item.get("status"),
                "intercompany_source_mode": item.get("source_mode"),
            }
        )
    return indexed


def load_breakdown_rows(
    audit_path: Path,
    source_index: Path,
    cutoff: date,
    live_cash_report: Path | None = None,
) -> list[dict[str, Any]]:
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    with source_index.open(encoding="utf-8-sig", newline="") as source_file:
        source_rows = list(csv.DictReader(source_file))
    by_property: dict[str, list[dict[str, Any]]] = {}
    for row in source_rows:
        by_property.setdefault(str(row.get("Property") or "").strip(), []).append(row)
    policy = load_policy()
    live_cash = load_live_cash_rows(live_cash_report)
    output = []
    for item in audit.get("properties") or []:
        title = str(item.get("sheet_title") or "").strip()
        source_property = str(item.get("source_property") or "").strip()
        row_number = int(item.get("sheet_row") or 0)
        if title not in TABS or row_number < 2:
            continue
        property_rows = by_property.get(source_property, [])
        liability = -policy.outstanding_manual_accrual_liability(property_rows, source_property, cutoff) if source_property else Decimal("0")
        gross = money(item.get("eco_cash_before_yhome"))
        yhome = money(item.get("expected_negative_yhome_adjustment"))
        other = Decimal("0.00")
        payable = Decimal("0.00")
        live_item = live_cash.get(normalized_property(source_property))
        if live_item:
            if live_item.get("eco_held_cash_gross") is not None:
                gross = max(Decimal("0.00"), money(live_item.get("eco_held_cash_gross")))
                liability = money(live_item.get("open_accrued_obligations"))
                other = money(live_item.get("eco_held_restricted_cash"))
                yhome = Decimal("0.00")
            payable = money(live_item.get("dao_accounts_payable_to_eco"))
        net = max(
            Decimal("0.00"),
            (gross - liability - other + yhome).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        )
        output.append(
            {
                "property": str(item.get("property") or "").strip(),
                "sheet_title": title,
                "sheet_gid": TABS[title],
                "row_number": row_number,
                "source_property": source_property,
                "eco_held_dao_cash_gross": float(gross),
                "accrued_but_unpaid_obligations": float(liability),
                "other_eco_held_restrictions": float(other),
                "yhome_cash_settlement_adjustment": float(yhome),
                "eco_net_dao_funds": float(net),
                "dao_accounts_payable_to_eco": float(payable),
            }
        )
    return output


def cell(rows: list[list[Any]], row: int, column: int) -> Any:
    try:
        return rows[row - 1][column - 1]
    except IndexError:
        return ""


def build_plan(live: dict[str, list[list[Any]]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    tab_plans: dict[str, dict[str, Any]] = {}
    issues = []
    for title, gid in TABS.items():
        values = live[title]
        headers = [str(value).strip() for value in (values[0] if values else [])]
        if NET_HEADER not in headers:
            issues.append({"type": "net_header_missing", "sheet_title": title})
            continue
        present = [header in headers for header in BREAKDOWN_HEADERS]
        if any(present) and not all(present):
            issues.append({"type": "partial_breakdown_schema", "sheet_title": title, "present": present})
            continue
        net_index = headers.index(NET_HEADER) + 1
        payable_indexes = [index + 1 for index, header in enumerate(headers) if header == PAYABLE_HEADER]
        if len(payable_indexes) > 1:
            issues.append({"type": "duplicate_payable_header", "sheet_title": title})
            continue
        insert_required = not all(present)
        gross_index = net_index if insert_required else headers.index(BREAKDOWN_HEADERS[0]) + 1
        expected = list(range(gross_index, gross_index + 4))
        if not insert_required and [headers.index(header) + 1 for header in BREAKDOWN_HEADERS] != expected:
            issues.append({"type": "breakdown_columns_not_adjacent", "sheet_title": title})
            continue
        net_after = net_index + 4 if insert_required else net_index
        payable_insert_required = not payable_indexes
        payable_column = net_after + 1 if payable_insert_required else payable_indexes[0] + (4 if insert_required and payable_indexes[0] >= net_index else 0)
        if not payable_insert_required and payable_column != net_after + 1:
            issues.append({"type": "payable_column_not_adjacent", "sheet_title": title})
            continue
        tab_plans[title] = {
            "sheet_gid": gid,
            "insert_required": insert_required,
            "insert_before_column": net_index,
            "gross_column": gross_index,
            "accrued_column": gross_index + 1,
            "other_column": gross_index + 2,
            "yhome_column": gross_index + 3,
            "net_column": net_after,
            "payable_column": payable_column,
            "payable_insert_required": payable_insert_required,
        }
    changes = []
    for item in rows:
        tab = tab_plans.get(item["sheet_title"])
        if not tab:
            continue
        formula = (
            f"=MAX(0,ROUND(N({letters(tab['gross_column'])}{item['row_number']})"
            f"-N({letters(tab['accrued_column'])}{item['row_number']})"
            f"-N({letters(tab['other_column'])}{item['row_number']})"
            f"+N({letters(tab['yhome_column'])}{item['row_number']}),2))"
        )
        changes.append({**item, "formula": formula, "net_cell": f"{letters(tab['net_column'])}{item['row_number']}"})
    digest_payload = {"tabs": tab_plans, "changes": changes}
    digest = hashlib.sha256(json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"tabs": tab_plans, "changes": changes, "issues": issues, "digest": digest}


def requests_for(plan: dict[str, Any]) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for title, tab in plan["tabs"].items():
        if tab["insert_required"]:
            start = tab["insert_before_column"] - 1
            requests.append({"insertDimension": {"range": {"sheetId": tab["sheet_gid"], "dimension": "COLUMNS", "startIndex": start, "endIndex": start + 4}, "inheritFromBefore": True}})
        start = tab["gross_column"] - 1
        requests.append(
            {
                "updateCells": {
                    "range": {"sheetId": tab["sheet_gid"], "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": start, "endColumnIndex": start + 4},
                    "rows": [{"values": [{"userEnteredValue": {"stringValue": header}} for header in BREAKDOWN_HEADERS]}],
                    "fields": "userEnteredValue",
                }
            }
            )
        if tab["payable_insert_required"]:
            start = tab["net_column"]
            requests.append({"insertDimension": {"range": {"sheetId": tab["sheet_gid"], "dimension": "COLUMNS", "startIndex": start, "endIndex": start + 1}, "inheritFromBefore": True}})
    for item in plan["changes"]:
        tab = plan["tabs"][item["sheet_title"]]
        values = [
            item["eco_held_dao_cash_gross"],
            item["accrued_but_unpaid_obligations"],
            item["other_eco_held_restrictions"],
            item["yhome_cash_settlement_adjustment"],
        ]
        row_index = item["row_number"] - 1
        start = tab["gross_column"] - 1
        requests.append(
            {
                "updateCells": {
                    "range": {"sheetId": tab["sheet_gid"], "startRowIndex": row_index, "endRowIndex": row_index + 1, "startColumnIndex": start, "endColumnIndex": start + 4},
                    "rows": [{"values": [{"userEnteredValue": {"numberValue": value}} for value in values]}],
                    "fields": "userEnteredValue",
                }
            }
        )
        payable_index = tab["payable_column"] - 1
        requests.append(
            {
                "updateCells": {
                    "range": {"sheetId": tab["sheet_gid"], "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": payable_index, "endColumnIndex": payable_index + 1},
                    "rows": [{"values": [{"userEnteredValue": {"stringValue": PAYABLE_HEADER}}]}],
                    "fields": "userEnteredValue",
                }
            }
        )
        net_index = tab["net_column"] - 1
        requests.append(
            {
                "updateCells": {
                    "range": {"sheetId": tab["sheet_gid"], "startRowIndex": row_index, "endRowIndex": row_index + 1, "startColumnIndex": net_index, "endColumnIndex": net_index + 1},
                    "rows": [{"values": [{"userEnteredValue": {"formulaValue": item["formula"]}}]}],
                    "fields": "userEnteredValue",
                }
            }
        )
        payable_index = tab["payable_column"] - 1
        requests.append(
            {
                "updateCells": {
                    "range": {"sheetId": tab["sheet_gid"], "startRowIndex": row_index, "endRowIndex": row_index + 1, "startColumnIndex": payable_index, "endColumnIndex": payable_index + 1},
                    "rows": [{"values": [{"userEnteredValue": {"numberValue": item["dao_accounts_payable_to_eco"]}}]}],
                    "fields": "userEnteredValue",
                }
            }
        )
    return requests


def verify(args: argparse.Namespace, plan: dict[str, Any]) -> list[dict[str, Any]]:
    formulas = read_live(args, "FORMULA")
    values = read_live(args, "UNFORMATTED_VALUE")
    issues = []
    for item in plan["changes"]:
        tab = plan["tabs"][item["sheet_title"]]
        observed_formula = str(cell(formulas[item["sheet_title"]], item["row_number"], tab["net_column"]))
        observed_value = money(cell(values[item["sheet_title"]], item["row_number"], tab["net_column"]))
        observed_payable = money(cell(values[item["sheet_title"]], item["row_number"], tab["payable_column"]))
        if observed_formula != item["formula"]:
            issues.append({"type": "formula_mismatch", "property": item["property"], "expected": item["formula"], "observed": observed_formula})
        if observed_value != money(item["eco_net_dao_funds"]):
            issues.append({"type": "value_mismatch", "property": item["property"], "expected": item["eco_net_dao_funds"], "observed": float(observed_value)})
        if observed_payable != money(item["dao_accounts_payable_to_eco"]):
            issues.append({"type": "payable_value_mismatch", "property": item["property"], "expected": item["dao_accounts_payable_to_eco"], "observed": float(observed_payable)})
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--source-index", type=Path, default=ROOT / "reports/baselane_source_transaction_index.csv")
    parser.add_argument("--live-dao-cash-report", type=Path)
    parser.add_argument("--spreadsheet-id", default=SPREADSHEET_ID)
    parser.add_argument("--cutoff", type=date.fromisoformat, default=date(2026, 7, 31))
    parser.add_argument("--report", type=Path, default=ROOT / "reports/yhome_eco_cash_breakdown_sync.json")
    parser.add_argument("--gws-bin", default="gws")
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument("--expected-digest")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    audit_path = latest_audit(args.audit, args.source_index)
    live = read_live(args, "FORMULA")
    rows = load_breakdown_rows(
        audit_path,
        args.source_index,
        args.cutoff,
        args.live_dao_cash_report,
    )
    plan = build_plan(live, rows)
    plan["issues"].extend(source_index_issues(args.source_index))
    report = {
        "job": "yhome-eco-cash-breakdown-sync",
        "generated_at": now(),
        "status": "review" if plan["issues"] else "ok",
        "spreadsheet_id": args.spreadsheet_id,
        "cutoff": args.cutoff.isoformat(),
        "audit": str(audit_path),
        "audit_sha256": sha256(audit_path),
        "source_index": str(args.source_index),
        "source_index_sha256": sha256(args.source_index),
        "live_dao_cash_report": str(args.live_dao_cash_report) if args.live_dao_cash_report else None,
        "live_dao_cash_report_sha256": sha256(args.live_dao_cash_report) if args.live_dao_cash_report else None,
        "digest": plan["digest"],
        "row_count": len(plan["changes"]),
        "tabs": plan["tabs"],
        "changes": plan["changes"],
        "issues": plan["issues"],
        "apply_requested": args.apply,
        "write_enabled": os.environ.get("YHOME_GSHEET_WRITE_ENABLED") == "1",
    }
    if args.apply:
        if os.environ.get("YHOME_GSHEET_WRITE_ENABLED") != "1":
            report["status"] = "review"; report["issues"].append({"type": "write_not_enabled"})
        elif args.expected_digest != plan["digest"]:
            report["status"] = "review"; report["issues"].append({"type": "preview_digest_mismatch", "expected": args.expected_digest, "observed": plan["digest"]})
        elif not report["issues"]:
            request_body = {"requests": requests_for(plan)}
            run([args.gws_bin, "sheets", "spreadsheets", "batchUpdate", "--params", json.dumps({"spreadsheetId": args.spreadsheet_id}), "--json", json.dumps(request_body, separators=(",", ":")), "--format", "json"], args.timeout_seconds)
            verification_issues = verify(args, plan)
            report["verification_issues"] = verification_issues
            report["status"] = "ok" if not verification_issues else "review"
            report["applied"] = not verification_issues
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "digest": plan["digest"], "rows": len(plan["changes"]), "issues": len(report["issues"])}))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
