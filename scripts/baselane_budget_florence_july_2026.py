#!/usr/bin/env python3
"""Deterministic July 2026 Florence Odongo cleaning reserve schedule."""

from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "florence_cleaning_budget.20260729.json"
CENT = Decimal("0.01")

# Baselane-paid monthly invoices (March-June cleaning service months).
ACTUALS = {
    "84 Madison Ave": ["435.00", "410.00", "640.00", "1035.00"],
    "86 Madison Ave": ["1095.00", "1170.00", "850.00", "805.00"],
    "88 Madison Ave": ["1880.00", "2010.00", "2375.00", "1750.00"],
    "90 Madison Ave": ["1105.00", "1020.00", "1565.00", "1285.00"],
}

# Accepted checkout counts verified from Hospitable for June and July.
CHECKOUTS = {
    "84 Madison Ave": {"june": 12, "july": 11},
    "86 Madison Ave": {"june": 8, "july": 20},
    "88 Madison Ave": {"june": 33, "july": 43},
    "90 Madison Ave": {"june": 9, "july": 6},
}


def q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def main() -> int:
    rows = []
    total = Decimal("0")
    for property_name, raw_actuals in ACTUALS.items():
        actuals = [Decimal(value) for value in raw_actuals]
        counts = CHECKOUTS[property_name]
        scaled = q(actuals[-1] * Decimal(counts["july"]) / Decimal(counts["june"]))
        trailing_peak = max(actuals)
        budget = max(scaled, trailing_peak)
        total += budget
        rows.append(
            {
                "property": property_name,
                "march_to_june_actuals": raw_actuals,
                "june_accepted_checkouts": counts["june"],
                "july_accepted_checkouts": counts["july"],
                "june_invoice_scaled_to_july_checkouts": f"{scaled:.2f}",
                "trailing_four_month_peak": f"{trailing_peak:.2f}",
                "july_budget": f"{budget:.2f}",
            }
        )
    payload = {
        "status": "budget_estimate_not_booked_invoice",
        "as_of": "2026-07-29",
        "method": (
            "Higher of the March-June actual invoice peak and the June invoice "
            "scaled by July/June accepted Hospitable checkout counts."
        ),
        "rows": rows,
        "total_july_budget": f"{total:.2f}",
    }
    REPORT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**payload, "report": str(REPORT)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
