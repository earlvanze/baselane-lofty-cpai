#!/usr/bin/env python3
"""Produce sanitized, deterministic property allocations from LawnStarter billing."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

ROOT = Path(__file__).absolute().parents[1]
ADDRESS_RE = re.compile(r"\bat\s+(.+?)\s+on\s+\d{4}-\d{2}-\d{2}\b", re.IGNORECASE)


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def cents(value: Any) -> int:
    return int((Decimal(str(value or 0)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def money(value_cents: int) -> str:
    return f"{Decimal(value_cents) / 100:.2f}"


def address_from_note(note: Any) -> str | None:
    match = ADDRESS_RE.search(str(note or ""))
    return match.group(1).strip() if match else None


def build_evidence(pages: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [row for page in pages for row in (page.get("data") or []) if isinstance(row, dict)]
    charges = {
        str(row.get("confirmation")): row
        for row in rows
        if row.get("reference") == "StripeCharge" and row.get("confirmation") and cents(row.get("total")) > 0
    }
    allocations: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        confirmation = str(row.get("confirmation") or "")
        if not confirmation or confirmation not in charges or row.get("reference") == "StripeCharge":
            continue
        for item in row.get("items") or []:
            if not isinstance(item, dict):
                continue
            address = address_from_note(item.get("note"))
            if not address:
                continue
            allocations[confirmation][address] += abs(
                cents(item.get("amount")) + cents(item.get("tax")) + cents(item.get("fee"))
            )

    evidence_rows = []
    for confirmation, charge in charges.items():
        amount_cents = cents(charge.get("total"))
        children = [
            {"property_address": address, "amount": money(amount)}
            for address, amount in sorted(allocations.get(confirmation, {}).items())
            if amount
        ]
        allocation_cents = sum(cents(child["amount"]) for child in children)
        if not children or allocation_cents != amount_cents:
            continue
        evidence_rows.append(
            {
                "charge_date": str(charge.get("timestamp") or "")[:10],
                "amount": money(amount_cents),
                "confirmation_sha256": hashlib.sha256(confirmation.encode()).hexdigest(),
                "allocation_count": len(children),
                "allocations": children,
            }
        )
    evidence_rows.sort(key=lambda row: (row["charge_date"], row["amount"], row["confirmation_sha256"]))
    material = {"charges": evidence_rows}
    return {
        "generated_at": iso_z(),
        "status": "ok",
        "source": "lawnstarter_live_billing_api",
        "credential_material_persisted": False,
        "charge_count": len(evidence_rows),
        "evidence_digest": hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        **material,
    }


def live_pages(fetcher: Path) -> list[dict[str, Any]]:
    completed = subprocess.run(
        ["node", str(fetcher)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "LawnStarter CDP fetch failed")
    data = json.loads(completed.stdout)
    if not isinstance(data, list):
        raise RuntimeError("LawnStarter CDP fetch returned a non-list payload")
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--billing-json", type=Path)
    source.add_argument("--live", action="store_true")
    parser.add_argument("--fetcher", type=Path, default=ROOT / "scripts" / "lawnstarter_billing_fetch_cdp.js")
    parser.add_argument("--report", type=Path, default=ROOT / "reports" / "lawnstarter_billing_evidence.json")
    args = parser.parse_args()
    pages = json.loads(args.billing_json.read_text(encoding="utf-8")) if args.billing_json else live_pages(args.fetcher)
    report = build_evidence(pages)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "charge_count", "evidence_digest")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
