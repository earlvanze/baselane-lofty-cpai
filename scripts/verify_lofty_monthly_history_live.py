#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PUBLISHER = ROOT / "skills" / "lofty-pm" / "scripts" / "publish_latest_update_to_lofty.py"


def load_module(path: Path) -> Any:
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("lofty_history_live_publisher", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def live_properties(data: dict[str, Any]) -> list[dict[str, Any]]:
    response = data.get("response") if isinstance(data.get("response"), dict) else data
    payload = response.get("data") if isinstance(response.get("data"), dict) else response
    return payload.get("properties") or []


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify exact canonical Lofty history after live writes.")
    parser.add_argument("--target-map", type=Path, required=True)
    parser.add_argument("--live-properties", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    publisher = load_module(PUBLISHER)
    targets = json.loads(args.target_map.read_text(encoding="utf-8")).get("records") or []
    live = json.loads(args.live_properties.read_text(encoding="utf-8"))
    live_by_id = {str(row.get("id") or ""): row for row in live_properties(live)}
    stale_phrases = (
        "Spendable cash ECO owes this DAO",
        "Cash ECO physically holds for this DAO, before unpaid bills",
        "ECO Net DAO Funds is the full property General Ledger net position",
    )
    rows = []
    for target in targets:
        property_id = str(target.get("lofty_property_id") or "")
        updates_md = Path(str(target.get("updates_md") or ""))
        live_text = str(live_by_id.get(property_id, {}).get("updates") or "")
        expected = publisher.listing_lofty_text(publisher.parse_entries(updates_md.read_text(encoding="utf-8")))
        issues = []
        if not live_text:
            issues.append("missing_live_updates")
        if live_text != expected:
            issues.append("live_history_differs_from_canonical_rendering")
        if "Property Update (07/30/2026)" in live_text:
            issues.append("stale_2026_07_30_entry")
        if live_text.count("Property Update (07/31/2026)") != 1:
            issues.append("expected_exactly_one_2026_07_31_entry")
        for phrase in stale_phrases:
            if phrase in live_text:
                issues.append(f"stale_phrase:{phrase}")
        rows.append(
            {
                "property_name": target.get("property_name"),
                "lofty_property_id": property_id,
                "status": "ok" if not issues else "review",
                "issues": issues,
                "expected_sha256": sha256_text(expected),
                "live_sha256": sha256_text(live_text),
                "july_31_count": live_text.count("Property Update (07/31/2026)"),
                "july_30_count": live_text.count("Property Update (07/30/2026)"),
            }
        )
    issue_count = sum(len(row["issues"]) for row in rows)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "target_count": len(targets),
        "verified_count": sum(row["status"] == "ok" for row in rows),
        "issue_count": issue_count,
        "properties": rows,
        "status": "ok" if len(rows) == len(targets) and issue_count == 0 else "review",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
