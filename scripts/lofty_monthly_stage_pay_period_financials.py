#!/usr/bin/env python3
"""Stage ledger-backed prior-month P&L data for Lofty's next pay period."""

from __future__ import annotations

import argparse
import calendar
import csv
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
from zoneinfo import ZoneInfo


PUBLISH_FIELDS = (
    "monthly_rent",
    "taxes",
    "insurance",
    "management_fees",
    "utilities",
    "utilities_water_sewer",
    "llc_admin_fee_yearly",
    "or_replenishment",
    "cash_flow",
    "notes",
)


def norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def money(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def row_in_month(raw: Any, run_month: str) -> bool:
    value = str(raw or "").strip()
    if value.startswith(run_month):
        return True
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m") == run_month
        except ValueError:
            pass
    return False


def ledger_bucket(row: dict[str, Any]) -> str | None:
    notes = str(row.get("Notes") or "").lower()
    if "aops-pnl-accrual|retained_capital|" in notes:
        return "retained_capital"
    transaction = norm(" ".join(str(row.get(key) or "") for key in ("Merchant", "Description")))
    if "internal transfer" in transaction:
        return None
    typed = norm(" ".join(str(row.get(key) or "") for key in ("Type", "Category", "Sub-category")))
    category = norm(" ".join(str(row.get(key) or "") for key in ("Category", "Sub-category")))
    if "insurance" in typed or "rental dwelling" in typed:
        return "insurance"
    if "revenue" in typed or "rent" in typed:
        return "rents"
    if "capex" in category or "capital expenditure" in category or "remodel" in category:
        return "capex"
    if "loan payments" in typed or "mortgage payment" in typed:
        return "debt_service"
    if "utility" in typed:
        return "utilities"
    if "tax" in typed:
        return "taxes"
    if "management" in typed or "pm fee" in typed:
        return "property_mgmt_fee"
    if "software" in typed or "subscription" in typed:
        return "other_opex"
    if "legal" in typed or "professional" in typed:
        return "other_opex"
    if any(word in typed for word in ("clean", "maintenance", "repair", "supply", "expense")):
        return "other_opex"
    return None


def ledger_buckets(path: Path, run_month: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        for row in csv.DictReader(handle):
            if not row_in_month(row.get("Date") or row.get("date"), run_month):
                continue
            amount = money(row.get("Amount") if "Amount" in row else row.get("amount"))
            bucket = ledger_bucket(row)
            if amount is not None and bucket:
                totals[bucket] = totals.get(bucket, 0.0) + amount
    return {key: round(value, 2) for key, value in totals.items()}


def build_pl_entry(
    record: dict[str, Any],
    run_month: str,
    distribution_eligibility: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    snapshot = record.get("financial_candidate_snapshot") or {}
    issues = list(record.get("financial_candidate_gate_issues") or [])
    ledger_path = Path(str(snapshot.get("ledger_path") or ""))
    if snapshot.get("status") != "ok":
        issues.append("financial candidate snapshot is not ok")
    if not ledger_path.is_file():
        issues.append(f"canonical ledger missing: {ledger_path}")
    if issues:
        return None, issues

    totals = ledger_buckets(ledger_path, run_month)
    revenue = round(float(snapshot.get("revenue") or 0), 2)
    noi = round(float(snapshot.get("noi") or 0), 2)
    nocf = round(float(snapshot.get("net_operating_cashflow") or 0), 2)
    below_line = round(noi - nocf, 2)
    if below_line < -0.01:
        return None, ["positive below-the-line activity cannot be represented by Lofty's P&L schema"]

    def annual_expense(bucket: str) -> float:
        return round(max(-totals.get(bucket, 0.0), 0.0) * 12, 2)

    represented = sum(totals.get(key, 0.0) for key in ("taxes", "insurance", "property_mgmt_fee", "utilities"))
    total_opex = round(float(snapshot.get("operating_expenses") or 0), 2)
    other_opex = round(total_opex - represented, 2)
    if other_opex > 0.01:
        return None, [f"operating expense sign is invalid for Lofty staging: {other_opex:.2f}"]

    source_name = ledger_path.name
    distribution_eligible = distribution_eligibility is None or distribution_eligibility.get("eligible") is not False
    retained_nocf = max(nocf, 0.0) if not distribution_eligible else 0.0
    eligibility_note = ""
    if not distribution_eligible:
        eligibility_note = (
            " Positive Net Operating Cashflow is retained rather than distributed under the "
            f"property distribution policy: {distribution_eligibility.get('reason') or 'distribution ineligible'}."
        )
    notes = (
        f"ECO ledger close for {run_month}. NOI excludes debt service, CapEx, and retained capital. "
        f"Lofty's limited P&L schema displays utilities plus other operating expenses in its Utilities grouping; "
        f"the canonical source is {source_name}. Net Operating Cashflow is annualized after below-the-line activity."
        f"{eligibility_note}"
    )
    entry = {
        "monthly_rent": revenue,
        "taxes": annual_expense("taxes"),
        "insurance": annual_expense("insurance"),
        "management_fees": annual_expense("property_mgmt_fee"),
        "utilities": round(max(-(totals.get("utilities", 0.0) + other_opex), 0.0) * 12, 2),
        "utilities_water_sewer": 0.0,
        "llc_admin_fee_yearly": 0.0,
        "or_replenishment": round((max(below_line, 0.0) + retained_nocf) * 12, 2),
        "cash_flow": round((max(nocf, 0.0) - retained_nocf) * 12, 2),
        "notes": notes,
    }
    calculated_noi = round(entry["monthly_rent"] * 12 - sum(entry[key] for key in (
        "taxes", "insurance", "management_fees", "utilities", "utilities_water_sewer", "llc_admin_fee_yearly"
    )), 2)
    calculated_cash_flow = round(max(calculated_noi - entry["or_replenishment"], 0.0), 2)
    if abs(calculated_noi - round(noi * 12, 2)) > 0.02:
        issues.append(f"Lofty P&L NOI does not reconcile: {calculated_noi:.2f} != {noi * 12:.2f}")
    if abs(calculated_cash_flow - entry["cash_flow"]) > 0.02:
        issues.append(f"Lofty P&L cash flow does not reconcile: {calculated_cash_flow:.2f} != {entry['cash_flow']:.2f}")
    return (entry if not issues else None), issues


def upcoming_month(config: dict[str, Any], now: datetime) -> tuple[int, int]:
    zone = ZoneInfo(str(config.get("timezone") or "America/Chicago"))
    local = now.astimezone(zone)
    cutoff_day = min(int(config.get("cutoffDay") or 15), calendar.monthrange(local.year, local.month)[1])
    hour, minute = (int(part) for part in str(config.get("cutoffTimeCt") or "12:00").split(":", 1))
    before = local < local.replace(day=cutoff_day, hour=hour, minute=minute, second=0, microsecond=0)
    year, month = local.year, local.month
    if before:
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return year, month - 1  # Lofty API month is zero-based.


def comparable(entry: dict[str, Any] | None) -> dict[str, Any]:
    entry = entry or {}
    return {key: entry.get(key) for key in PUBLISH_FIELDS}


def entries_equal(actual: dict[str, Any] | None, expected: dict[str, Any]) -> bool:
    actual_values = comparable(actual)
    expected_values = comparable(expected)
    for key in PUBLISH_FIELDS:
        left, right = actual_values.get(key), expected_values.get(key)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            if abs(float(left) - float(right)) > 0.005:
                return False
        elif left != right:
            return False
    return True


def response_data(value: Any) -> Any:
    if isinstance(value, dict) and isinstance(value.get("data"), dict):
        return value["data"]
    return value


def load_updater(path: Path):
    spec = importlib.util.spec_from_file_location("lofty_runtime_updater", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load Lofty runtime helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bridge_call(updater: Any, target_id: str, method: str, payload: dict[str, Any] | None = None) -> Any:
    argument = "" if payload is None else json.dumps(payload)
    expression = f"""(async () => {{
      const bridge = globalThis.__openclawLoftyBridge;
      if (!bridge?.ok || typeof bridge[{json.dumps(method)}] !== 'function')
        return {{ok:false,error:'missing bridge method {method}'}};
      try {{ return {{ok:true,result:await bridge[{json.dumps(method)}]({argument})}}; }}
      catch (err) {{ return {{ok:false,error:String(err)}}; }}
    }})()"""
    raw = updater.runtime_eval(target_id, expression, await_promise=True, timeout=60)
    value = raw.get("result", {}).get("result", {}).get("value") or {}
    if value.get("ok") is not True:
        raise RuntimeError(value.get("error") or f"{method} failed")
    return value.get("result")


def runtime_index(runtime_map: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for item in runtime_map.get("properties") or runtime_map.get("records") or []:
        for key in ("property_name", "full_address", "managed_name"):
            if item.get(key):
                index[norm(item[key])] = item
    return index


def load_distribution_eligibility(path: Path | None, run_month: str) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    index: dict[str, dict[str, Any]] = {}
    for record in payload.get("records") or []:
        if not isinstance(record, dict):
            continue
        record_month = str(record.get("run_month") or "").strip()
        if record_month and record_month != run_month:
            continue
        for key in ("property_name", "managed_name", "input_property_name"):
            name = norm(record.get(key))
            if name:
                index[name] = record
    return index


def distribution_eligibility_for(
    source: dict[str, Any], index: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    for key in ("property_name", "managed_name", "input_property_name"):
        name = norm(source.get(key))
        if not name:
            continue
        if name in index:
            return index[name]
        for candidate_name, record in index.items():
            if name.startswith(candidate_name) or candidate_name.startswith(name):
                return record
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-packet", required=True, type=Path)
    parser.add_argument("--runtime-map", required=True, type=Path)
    parser.add_argument("--runtime-helper", required=True, type=Path)
    parser.add_argument("--run-month", required=True)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--distribution-eligibility-overrides", type=Path)
    parser.add_argument("--property-id", help="Limit staging to one Lofty property ID")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--close-extra-tabs", action="store_true")
    args = parser.parse_args(argv)

    packet = json.loads(args.candidate_packet.read_text(encoding="utf-8"))
    runtime_map = json.loads(args.runtime_map.read_text(encoding="utf-8"))
    mapped = runtime_index(runtime_map)
    eligibility_index = load_distribution_eligibility(args.distribution_eligibility_overrides, args.run_month)
    records: list[dict[str, Any]] = []
    for source in packet.get("records") or []:
        runtime = next((mapped.get(norm(source.get(key))) for key in ("property_name", "managed_name", "input_property_name") if source.get(key) and mapped.get(norm(source.get(key)))), None)
        if args.property_id and (not runtime or runtime.get("lofty_property_id") != args.property_id):
            continue
        eligibility = distribution_eligibility_for(source, eligibility_index)
        entry, issues = build_pl_entry(source, args.run_month, eligibility)
        records.append({
            "property_name": source.get("property_name"),
            "lofty_property_id": runtime.get("lofty_property_id") if runtime else None,
            "desired_entry": entry,
            "distribution_eligibility": eligibility,
            "issues": issues + ([] if runtime else ["property missing from Lofty runtime map"]),
            "status": "planned" if entry and runtime else "blocked",
        })

    cutoff_config = None
    target_year = target_month = None
    live_error = None
    if args.apply:
        try:
            updater = load_updater(args.runtime_helper)
            context = updater.ensure_lofty_cdp_context(mode="list", close_extras=args.close_extra_tabs)
            bridge = updater.install_turbopack_bridge(context["targetId"])
            if bridge.get("ok") is not True:
                raise RuntimeError(f"Lofty bridge unavailable: {bridge}")
            cutoff_response = response_data(bridge_call(updater, context["targetId"], "getPlCutoffConfig"))
            cutoff_config = (cutoff_response or {}).get("config") if isinstance(cutoff_response, dict) else None
            if not isinstance(cutoff_config, dict):
                raise RuntimeError("live Lofty cutoff config unavailable")
            target_year, target_month = upcoming_month(cutoff_config, datetime.now(timezone.utc))
            expected = f"{target_year:04d}-{target_month + 1:02d}"
            if expected != args.run_month:
                raise RuntimeError(f"run month {args.run_month} is not Lofty's upcoming pay-period month {expected}")
            for record in records:
                if record["status"] == "blocked":
                    continue
                property_id = record["lofty_property_id"]
                query = {"propertyId": property_id, "year": target_year, "month": target_month}
                got = response_data(bridge_call(updater, context["targetId"], "getPlEntryForProperty", query))
                existing = got.get("plEntry") if isinstance(got, dict) else None
                if isinstance(existing, dict) and existing.get("status") == "processed":
                    record["status"] = "skipped_processed"
                    continue
                if existing is None:
                    bridge_call(updater, context["targetId"], "createPlEntryForProperty", query)
                    got = response_data(bridge_call(updater, context["targetId"], "getPlEntryForProperty", query))
                    existing = got.get("plEntry") if isinstance(got, dict) else None
                desired = record["desired_entry"]
                if entries_equal(existing, desired) and existing.get("status") == "ready":
                    record["status"] = "unchanged_ready"
                    continue
                base = dict(existing or {})
                base.update(desired)
                draft = {**base, "status": "draft", "submittedAt": None}
                bridge_call(updater, context["targetId"], "updatePlEntryForProperty", {**query, "data": draft})
                ready = {**draft, "status": "ready", "submittedAt": int(datetime.now(timezone.utc).timestamp() * 1000)}
                bridge_call(updater, context["targetId"], "updatePlEntryForProperty", {**query, "data": ready})
                verified = None
                for attempt in range(4):
                    verified_response = response_data(bridge_call(updater, context["targetId"], "getPlEntryForProperty", query))
                    verified = verified_response.get("plEntry") if isinstance(verified_response, dict) else None
                    if isinstance(verified, dict) and entries_equal(verified, desired) and verified.get("status") == "ready":
                        break
                    if attempt < 3:
                        time.sleep(1)
                if not isinstance(verified, dict) or not entries_equal(verified, desired) or verified.get("status") != "ready":
                    raise RuntimeError(f"exact staged readback failed for {record['property_name']}")
                record["status"] = "applied_ready"
        except Exception as exc:
            live_error = f"{type(exc).__name__}: {exc}"

    blocked = sum(1 for record in records if record["status"] == "blocked")
    status = "review" if blocked or live_error else "ok"
    report = {
        "status": status,
        "mode": "apply" if args.apply else "dry_run",
        "run_month": args.run_month,
        "source": "canonical ECO property ledgers via review candidate packet",
        "distribution_eligibility_overrides": str(args.distribution_eligibility_overrides) if args.distribution_eligibility_overrides else None,
        "current_live_listing_financials_mutated": False,
        "cutoff_config": cutoff_config,
        "target_year": target_year,
        "target_month_zero_based": target_month,
        "live_error": live_error,
        "record_count": len(records),
        "blocked_count": blocked,
        "records": records,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if status == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
