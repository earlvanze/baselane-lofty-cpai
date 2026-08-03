#!/usr/bin/env python3
"""Validate exact intercompany overrides against the canonical source index."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from baselane_live_dao_cash_reconciliation import (  # noqa: E402
    INTERCOMPANY_TRANSACTION_OVERRIDES,
    build_eco_intercompany_subledger,
    load_intercompany_transaction_overrides,
)


DEFAULT_SOURCE = ROOT / "reports" / "baselane_source_transaction_index.csv"


def build_report(source: Path, policy: Path, cutoff: date) -> dict[str, object]:
    source_rows = list(csv.DictReader(source.open(encoding="utf-8-sig", newline="")))
    rules = load_intercompany_transaction_overrides(policy)
    positions = build_eco_intercompany_subledger(source_rows, cutoff, rules)
    included_count = sum(int(row["included_row_count"]) for row in positions.values())
    return {
        "status": "ok",
        "mode": "read_only_local_evidence",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "as_of": cutoff.isoformat(),
        "source": str(source),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "source_row_count": len(source_rows),
        "policy": str(policy),
        "policy_sha256": hashlib.sha256(policy.read_bytes()).hexdigest(),
        "rule_count": len(rules),
        "property_position_count": len(positions),
        "included_cash_row_count": included_count,
        "verified_payable_property_count": sum(
            row["status"] == "verified_payable_from_id_bearing_cash_rollforward"
            for row in positions.values()
        ),
        "positive_activity_review_property_count": sum(
            row["status"] == "positive_activity_requires_custody_reconciliation"
            for row in positions.values()
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument(
        "--policy", type=Path, default=INTERCOMPANY_TRANSACTION_OVERRIDES
    )
    args = parser.parse_args()
    try:
        report = build_report(args.source, args.policy, args.as_of)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "mode": "read_only_local_evidence",
                    "as_of": args.as_of.isoformat(),
                    "error": f"{type(exc).__name__}: {exc}",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
