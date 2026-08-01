#!/usr/bin/env python3
"""Restore active Lofty listing financial fields from an authenticated capture."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOFTY_SCRIPTS = ROOT / "skills" / "lofty-pm" / "scripts"
sys.path.insert(0, str(LOFTY_SCRIPTS))

from update_lofty_pm_property import (  # noqa: E402
    get_manager_properties_data_via_turbopack_bridge,
    request_update_via_turbopack_bridge,
)


RESTORED_FIELDS = (
    "cash_flow",
    "projected_annual_cash_flow",
    "cashflow_per_unit",
    "coc",
    "projected_rental_yield",
    "is_occupied",
    "current_loan",
    "monthly_loan_repayment",
)

# Corrections verified after the capture but still belonging to the June close.
CAPTURE_CORRECTIONS = {
    "01FKVM5P0WS9JPMCDZ1GB1RQQG": {
        "coc": 4.75,
        "projected_rental_yield": 4.75,
    },
}


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def properties_from_response(response: dict) -> list[dict]:
    data = response.get("data") if isinstance(response, dict) else None
    properties = data.get("properties") if isinstance(data, dict) else None
    if not isinstance(properties, list):
        raise RuntimeError("Lofty get-manager-properties response has no property list")
    return [item for item in properties if isinstance(item, dict)]


def captured_targets(capture: dict, property_snapshot: dict, active_ids: set[str]) -> dict[str, dict]:
    snapshot_rows = properties_from_response(property_snapshot)
    snapshot_by_id = {str(item.get("id") or ""): item for item in snapshot_rows}
    targets: dict[str, dict] = {}
    for record in capture.get("records") or []:
        property_id = str(record.get("lofty_property_id") or "")
        verify = record.get("live_distribution_verify") or {}
        if property_id not in active_ids:
            continue
        targets[property_id] = {
            "property_name": record.get("property_name"),
            "cash_flow": verify.get("actual"),
            "projected_annual_cash_flow": verify.get("expected_projected_annual_cash_flow"),
            "cashflow_per_unit": snapshot_by_id.get(property_id, {}).get("cashflow_per_unit"),
            "coc": verify.get("actual_coc"),
            "projected_rental_yield": verify.get("actual_projected_rental_yield"),
            "is_occupied": verify.get("actual_is_occupied"),
            "current_loan": verify.get("actual_current_loan"),
            "monthly_loan_repayment": verify.get("actual_monthly_loan_repayment"),
        }
        targets[property_id].update(CAPTURE_CORRECTIONS.get(property_id, {}))
    missing = sorted(active_ids - set(targets))
    if missing:
        raise RuntimeError(f"capture is missing active Lofty properties: {missing}")
    for property_id, target in targets.items():
        missing_fields = [field for field in RESTORED_FIELDS if field not in target]
        if missing_fields:
            raise RuntimeError(f"capture target {property_id} is missing fields: {missing_fields}")
    return targets


def equal(actual: object, expected: object) -> bool:
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return abs(float(actual) - float(expected)) < 0.005
    return actual == expected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--capture",
        type=Path,
        default=ROOT / "reports/lofty_financial_capture_recovery_20260728T1119Z.json",
    )
    parser.add_argument(
        "--runtime-map",
        type=Path,
        default=ROOT / "reports/baselane_financials_monthly_lofty_pm_runtime_map.json",
    )
    parser.add_argument(
        "--property-snapshot",
        type=Path,
        default=ROOT / "reports/lofty_live_updates_manager_properties.post-restore-20260722T023443Z.json",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "reports/lofty_june_live_financial_restore_20260731.json",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    runtime_map = read_json(args.runtime_map)
    active = runtime_map.get("properties") or []
    active_ids = {str(item.get("lofty_property_id") or "") for item in active}
    active_ids.discard("")
    targets = captured_targets(read_json(args.capture), read_json(args.property_snapshot), active_ids)

    before_response, before_context = get_manager_properties_data_via_turbopack_bridge(
        {"year": "2026", "month": "6"}, close_extra_tabs=True
    )
    before = {str(item.get("id")): item for item in properties_from_response(before_response)}
    records = []
    for property_id in sorted(active_ids, key=lambda item: str(targets[item]["property_name"])):
        target = targets[property_id]
        current = before.get(property_id)
        if current is None:
            raise RuntimeError(f"active Lofty property missing from live read: {property_id}")
        patch = {
            field: target[field]
            for field in RESTORED_FIELDS
            if not equal(current.get(field), target[field])
        }
        record = {
            "property_id": property_id,
            "property_name": target["property_name"],
            "before": {field: current.get(field) for field in RESTORED_FIELDS},
            "target": {field: target[field] for field in RESTORED_FIELDS},
            "patch": patch,
            "status": "planned" if patch else "unchanged",
        }
        if args.apply and patch:
            try:
                result = request_update_via_turbopack_bridge(
                    {"propertyId": property_id, "patch": patch},
                    property_id=property_id,
                    close_extra_tabs=True,
                )
                record["mutation"] = result
                record["status"] = "applied"
            except (Exception, SystemExit) as exc:
                record["status"] = "failed"
                record["error"] = str(exc)
        records.append(record)

    after_response, after_context = get_manager_properties_data_via_turbopack_bridge(
        {"year": "2026", "month": "6"}, close_extra_tabs=True
    )
    after = {str(item.get("id")): item for item in properties_from_response(after_response)}
    mismatch_count = 0
    for record in records:
        live = after.get(record["property_id"]) or {}
        record["after"] = {field: live.get(field) for field in RESTORED_FIELDS}
        mismatches = {
            field: {"expected": record["target"][field], "actual": live.get(field)}
            for field in RESTORED_FIELDS
            if not equal(live.get(field), record["target"][field])
        }
        record["mismatches"] = mismatches
        if mismatches:
            mismatch_count += 1
            if args.apply and record["status"] != "failed":
                record["status"] = "readback_mismatch"
        elif args.apply:
            record["status"] = "verified"

    report = {
        "generated_at": iso_z(),
        "apply": args.apply,
        "status": "ok" if mismatch_count == 0 else ("planned" if not args.apply else "failed"),
        "capture": str(args.capture),
        "capture_generated_at": read_json(args.capture).get("generated_at"),
        "runtime_map": str(args.runtime_map),
        "property_snapshot": str(args.property_snapshot),
        "active_property_count": len(active_ids),
        "changed_property_count": sum(bool(item["patch"]) for item in records),
        "mismatch_count": mismatch_count,
        "before_runtime": before_context.get("runtime"),
        "after_runtime": after_context.get("runtime"),
        "records": records,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"status={report['status']} apply={args.apply} active={len(active_ids)} "
        f"changed={report['changed_property_count']} mismatches={mismatch_count} report={args.report}"
    )
    return 0 if report["status"] in {"ok", "planned"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
