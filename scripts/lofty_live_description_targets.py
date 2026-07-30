#!/usr/bin/env python3
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FIELDS = (
    "id",
    "assetUnit",
    "address",
    "description",
    "property_type",
    "is_occupied",
    "custom_occupancy",
    "monthly_rent",
    "lease_begins_date",
    "lease_ends_date",
    "current_loan",
    "curr_maintenance_reserve",
    "ownerRent",
    "status",
)


def property_records(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    response = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    data = response.get("data") if isinstance(response.get("data"), dict) else response
    records = data.get("properties") if isinstance(data, dict) else None
    return [record for record in records or [] if isinstance(record, dict)]


def build_report(source: Path) -> dict[str, Any]:
    payload = json.loads(source.read_text(encoding="utf-8"))
    records = [{field: record.get(field) for field in FIELDS} for record in property_records(payload)]
    return {
        "status": "ok" if records else "review",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": str(source),
        "source_mtime": datetime.fromtimestamp(source.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
        "record_count": len(records),
        "records": records,
        "issues": [] if records else ["missing_lofty_manager_properties"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build DESCRIPTION.md accuracy targets from a live Lofty manager-properties response.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(args.source.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "record_count": report["record_count"], "output": str(args.output)}))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
