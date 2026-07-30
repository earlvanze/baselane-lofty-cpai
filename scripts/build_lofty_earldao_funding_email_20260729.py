#!/usr/bin/env python3
"""Build the exact July 29 Lofty-to-EARLDAO funding request draft.

The schedule combines:

* current coownership ECO/Lofty accounting positions and unrestricted bank cash;
* the approved $3,000 coownership reserve floor;
* the July Madison cleaning/repair budgets; and
* corrected Yhome ``DAO Net Cash (Capital Call)`` values for non-coownerships.

It refuses to produce a final draft unless the Cleveland formula-hardening
verification is clean and the ECO Net DAO Funds update plan is idempotent.
This script only writes local reports; it never sends email.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


ROOT = Path("/home/digit/.openclaw/workspace")
ACTIVE_CASH = ROOT / "reports/baselane_active_dao_cash_balances.csv"
LIVE_RECON = ROOT / "reports/baselane_live_dao_cash_reconciliation.json"
YHOME = ROOT / "reports/yhome_transition_reconciliation.csv"
FORMULA_VERIFY = ROOT / "reports/yhome_formula_sort_repair.20260729.verify.json"
ECO_PLAN = ROOT / "reports/yhome_operating_cash_update_plan.csv"
OUTPUT_JSON = ROOT / "reports/lofty_earldao_combined_funding.20260729.json"
OUTPUT_CSV = ROOT / "reports/lofty_earldao_combined_funding.20260729.csv"
OUTPUT_EMAIL = ROOT / "reports/lofty_earldao_combined_capital_call_funding_email.20260729.md"
CENT = Decimal("0.01")

COOWNERSHIPS = (
    ("22164 Umland Cir, Jenner, CA 95450", "22164 Umland Cir, Jenner, CA 95450", "22164 Umland Circle", Decimal("0")),
    ("49 Bannbury Ln, Palm Coast, FL 32137", "49 Bannbury Ln, Palm Coast, FL 32137", "49 Bannbury Ln", Decimal("0")),
    ("85-104 Alawa Pl, Waianae, HI 96792", "85-104 Alawa Pl", "85-104 Alawa Pl", Decimal("0")),
    ("724 3rd Ave, Watervliet, NY 12189", None, "724 3rd Ave", Decimal("0")),
    ("84 Madison Ave, Albany, NY 12202", "84 Madison Ave", "84 Madison Ave", Decimal("1035.00")),
    ("86 Madison Ave, Albany, NY 12202", "86 Madison Ave", "86 Madison Ave", Decimal("17012.50")),
    ("88 Madison Ave, Albany, NY 12202", "88 Madison Ave", "88 Madison Ave", Decimal("8375.00")),
    ("9 Country Club Lane North, Briarcliff Manor, NY 10510", "9 Country Club Lane N", "9 Country Club Ln N", Decimal("0")),
    ("90 Madison Ave, Albany, NY 12202", "90 Madison Ave", "90 Madison Ave", Decimal("1565.00")),
    ("326-332 S Alcott St, Denver, CO 80219", "326-332 S Alcott St", "326 South Alcott Street", Decimal("0")),
)

EXCLUDED_PREFIXES = (
    "1518 Dille Rd.",       # Ohio 3: existing $20,000 authority
    "1258 Lily St.",        # Ohio 3: existing $20,000 authority
    "1321 Allendale Ave.",  # Ohio 3: existing $20,000 authority
    "1432 Sara Ave.",       # existing $16,000 authority
    "8708 Willard Ave.",    # sold; buyer/EARLDAO closing settlement
)


def money(value: Any) -> Decimal:
    return Decimal(str(value or "0")).quantize(CENT, rounding=ROUND_HALF_UP)


def generated_at() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def find_prefix(rows: list[dict[str, Any]], field: str, prefix: str) -> dict[str, Any]:
    matches = [row for row in rows if str(row.get(field) or "").startswith(prefix)]
    if len(matches) != 1:
        raise RuntimeError(f"expected one {field} prefix match for {prefix!r}, found {len(matches)}")
    return matches[0]


def validate_inputs() -> dict[str, Any]:
    formula = json.loads(FORMULA_VERIFY.read_text(encoding="utf-8"))
    if (
        formula.get("status") != "ok"
        or formula.get("change_count") != 0
        or formula.get("missing_formula_count") != 0
    ):
        raise RuntimeError("Cleveland formula verification is not clean")
    eco_rows = csv_rows(ECO_PLAN)
    pending_eco = [row for row in eco_rows if row.get("action") == "update"]
    if pending_eco:
        raise RuntimeError(f"ECO Net DAO Funds still has {len(pending_eco)} pending updates")
    return {
        "formula_verification": str(FORMULA_VERIFY),
        "formula_remaining_change_count": 0,
        "eco_update_plan": str(ECO_PLAN),
        "eco_pending_update_count": 0,
    }


def build_coownership_rows(
    active_rows: list[dict[str, Any]],
    live_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output = []
    for property_name, active_prefix, live_prefix, obligations in COOWNERSHIPS:
        live = find_prefix(live_rows, "property", live_prefix)
        if active_prefix is None:
            accounting_position = money(live["gl_column_e_full_as_of"])
        else:
            active = find_prefix(active_rows, "property", active_prefix)
            accounting_position = money(active["combined_eco_and_lofty_reserve"])
        unrestricted_bank_cash = money(live["operations_balance"])
        target_cash = money(Decimal("3000.00") + obligations)
        accounting_gap = money(max(Decimal("0"), target_cash - accounting_position))
        bank_gap = money(max(Decimal("0"), target_cash - unrestricted_bank_cash))
        request = max(accounting_gap, bank_gap)
        output.append(
            {
                "property": property_name,
                "group": "coownership",
                "status": "active",
                "accounting_position": accounting_position,
                "unrestricted_bank_cash": unrestricted_bank_cash,
                "reserve_floor": Decimal("3000.00"),
                "near_term_obligations": obligations,
                "target_cash": target_cash,
                "accounting_gap": accounting_gap,
                "bank_gap": bank_gap,
                "request": request,
                "basis": "higher of accounting-position gap and unrestricted-bank gap",
            }
        )
    return output


def build_noncoownership_rows(yhome_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for row in yhome_rows:
        property_name = str(row.get("Property") or "").strip()
        sheet = str(row.get("__yhome_sheet_title") or "")
        if sheet not in {"Cleveland", "Chicago & non-Yhome"}:
            continue
        if not property_name or property_name.startswith("Sum of ") or property_name == "Yhome-Aligned Transition Summary":
            continue
        if property_name.startswith(EXCLUDED_PREFIXES):
            continue
        raw_net = str(row.get("DAO Net Cash (Capital Call)") or "").strip()
        if not raw_net:
            continue
        accounting_position = money(raw_net)
        if accounting_position >= 0:
            continue
        manager_status = str(row.get("New PM") or "")
        sold = "sold" in manager_status.lower()
        floor = Decimal("0.00") if sold else Decimal("500.00")
        request = money(-accounting_position + floor)
        output.append(
            {
                "property": property_name,
                "group": "non-coownership",
                "status": "sold" if sold else manager_status or "active",
                "accounting_position": accounting_position,
                "unrestricted_bank_cash": None,
                "reserve_floor": floor,
                "near_term_obligations": Decimal("0.00"),
                "target_cash": floor,
                "accounting_gap": request,
                "bank_gap": None,
                "request": request,
                "basis": "corrected Yhome DAO Net Cash plus applicable $500 non-coownership floor",
            }
        )
    return output


def json_ready(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: (f"{value:.2f}" if isinstance(value, Decimal) else value)
            for key, value in row.items()
        }
        for row in rows
    ]


def write_csv(rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0])
    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(json_ready(rows))


def fmt(value: Decimal) -> str:
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def write_email(rows: list[dict[str, Any]], total: Decimal, fee: Decimal) -> None:
    co_total = sum((row["request"] for row in rows if row["group"] == "coownership"), Decimal("0"))
    non_total = total - co_total
    lines = [
        "To: info@lofty.ai",
        "Subject: Funding request — EARLDAO combined DAO capital calls through July 29, 2026",
        "",
        "DRAFT — NOT SENT",
        "",
        "Hi Lofty team,",
        "",
        "Following the completed ECO cash and Yhome-transition reconciliation through July 29, 2026, the exact principal funding needed from Lofty to EARLDAO to support the combined incoming DAO capital calls is "
        f"**{fmt(total)}**.",
        "",
        f"- Coownership facilities: **{fmt(co_total)}**",
        f"- Non-coownership facilities: **{fmt(non_total)}**",
        f"- Total principal funding requested: **{fmt(total)}**",
        f"- EARLDAO 1% origination fees charged to the borrower DAOs: **{fmt(fee)}** (separate from, and not added to, the principal funding request)",
        "",
        "| DAO / property | Current reconciled position | Reserve / obligations included | Requested LOC principal |",
        "|---|---:|---:|---:|",
    ]
    for row in rows:
        included = row["reserve_floor"] + row["near_term_obligations"]
        lines.append(
            f"| {row['property']} | {fmt(row['accounting_position'])} | {fmt(included)} | **{fmt(row['request'])}** |"
        )
    lines.extend(
        [
            "",
            "The coownership amounts use the higher of (1) the accounting shortfall and (2) the unrestricted bank-cash shortfall after the $3,000 reserve floor and known July obligations. The other amounts use the corrected Yhome DAO Net Cash result and the approved $500 non-coownership floor; sold properties receive no reserve floor.",
            "",
            "Not included as new capital calls: Ohio 3 Property Package (existing $20,000 EARLDAO authority), 1432 Sara Ave (existing $16,000 authority), 8708 Willard Ave (buyer/EARLDAO closing settlement), 1315 E 114th St (Yhome/EARLDAO sold-property settlement), and 724's separate $5,200 individual loan from @thegottfather.",
            "",
            "Please confirm that Lofty can fund **EARLDAO** in the amount of **"
            f"{fmt(total)}** so EARLDAO can fund the approved borrower facilities as the corresponding DAO votes are completed.",
            "",
            "Best,",
            "Earl",
            "",
        ]
    )
    OUTPUT_EMAIL.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    controls = validate_inputs()
    active_rows = csv_rows(ACTIVE_CASH)
    live = json.loads(LIVE_RECON.read_text(encoding="utf-8"))
    yhome_rows = csv_rows(YHOME)
    rows = build_coownership_rows(active_rows, live["properties"])
    rows.extend(build_noncoownership_rows(yhome_rows))
    total = money(sum((row["request"] for row in rows), Decimal("0")))
    fee = money(total * Decimal("0.01"))
    if total != Decimal("291400.63") or len(rows) != 22:
        raise RuntimeError(f"funding control total/count changed: {total} across {len(rows)} rows")
    report = {
        "status": "ok",
        "draft_only": True,
        "generated_at": generated_at(),
        "as_of": "2026-07-29",
        "borrower_count": len(rows),
        "coownership_subtotal": f"{sum((row['request'] for row in rows if row['group'] == 'coownership'), Decimal('0')):.2f}",
        "non_coownership_subtotal": f"{sum((row['request'] for row in rows if row['group'] == 'non-coownership'), Decimal('0')):.2f}",
        "total_principal_funding_request": f"{total:.2f}",
        "one_percent_origination_fees_separate": f"{fee:.2f}",
        "controls": controls,
        "sources": {
            "active_cash": str(ACTIVE_CASH),
            "live_reconciliation": str(LIVE_RECON),
            "yhome_transition_reconciliation": str(YHOME),
        },
        "rows": json_ready(rows),
        "email_draft": str(OUTPUT_EMAIL),
        "email_sent": False,
    }
    write_csv(rows)
    write_email(rows, total, fee)
    OUTPUT_JSON.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "borrower_count", "coownership_subtotal", "non_coownership_subtotal", "total_principal_funding_request", "one_percent_origination_fees_separate")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
