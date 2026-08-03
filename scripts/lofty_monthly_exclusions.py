from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path
from typing import Any

YHOME_EXCLUDE_MARKERS = ("sold", "closed", "delisted")
DEFAULT_MANUAL_EXCLUDED_PROPERTIES = (
    "3560 Saint Albans Rd",
    "1935 S Glen Rd",
    "402 N Wild Olive Ave",
    "9919 S Oglesby",
)


def default_listing_update_policy_path() -> Path:
    candidates = [
        Path(os.environ["LOFTY_LISTING_UPDATE_POLICY"]) if os.environ.get("LOFTY_LISTING_UPDATE_POLICY") else None,
        Path(os.environ["WORKSPACE_ROOT"]) / "config" / "lofty_listing_update_policy.json"
        if os.environ.get("WORKSPACE_ROOT")
        else None,
        Path(__file__).resolve().parents[1] / "config" / "lofty_listing_update_policy.json",
        Path.home() / ".openclaw" / "workspace" / "config" / "lofty_listing_update_policy.json",
        Path("/home/digit/.openclaw/workspace/config/lofty_listing_update_policy.json"),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    return Path(__file__).resolve().parents[1] / "config" / "lofty_listing_update_policy.json"


DEFAULT_LISTING_UPDATE_POLICY_PATH = default_listing_update_policy_path()


def normalize(value: str) -> str:
    text = value.lower()
    text = re.sub(r"\blfty\d+\b", " ", text)
    text = text.replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\bavenue\b", "ave", text)
    text = re.sub(r"\bstreet\b", "st", text)
    text = re.sub(r"\beast\b", "e", text)
    text = re.sub(r"\bwest\b", "w", text)
    text = re.sub(r"\bnorth\b", "n", text)
    text = re.sub(r"\bsouth\b", "s", text)
    text = re.sub(r"\bohio\b", "oh", text)
    return re.sub(r"\s+", " ", text).strip()


def policy_name_matches(target: str, key: str) -> bool:
    if not target or not key:
        return False
    if key == target or key in target or target in key:
        return True
    target_tokens = [token for token in target.split() if token != "public"]
    key_tokens = key.split()
    return bool(target_tokens and key_tokens[: len(target_tokens)] == target_tokens)


def load_yhome_transition_exclusions(yhome_csv: Path | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not yhome_csv:
        return [], {"status": "not_configured", "path": None, "excluded_count": 0}
    if not yhome_csv.is_file():
        return [], {"status": "missing", "path": str(yhome_csv), "excluded_count": 0}
    excluded: list[dict[str, Any]] = []
    row_count = 0
    column_b_header = ""
    with yhome_csv.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        column_b_header = str(header[1] if len(header) > 1 else "").strip()
        property_index = next((idx for idx, name in enumerate(header) if normalize(str(name)) == "property"), 0)
        if len(header) < 2:
            return [], {
                "status": "missing_column_b",
                "path": str(yhome_csv),
                "row_count": 0,
                "excluded_count": 0,
                "column_b_index": 1,
                "column_b_header": column_b_header,
                "column_b_rule": "Yhome Transition Reconciliation column B marks sold/closed/delisted properties",
                "column_b_rule_ok": False,
            }
        for values in reader:
            row_count += 1
            property_name = str(values[property_index] if len(values) > property_index else "").strip()
            new_pm = str(values[1] if len(values) > 1 else "").strip()
            new_pm_tokens = normalize(new_pm).split()
            if not property_name or not any(marker in new_pm_tokens for marker in YHOME_EXCLUDE_MARKERS):
                continue
            excluded.append(
                {
                    "source": "yhome_transition_reconciliation",
                    "property_name": property_name,
                    "normalized_property": normalize(property_name),
                    "yhome_column_b": new_pm,
                    "exclude_reason": "Yhome Transition Reconciliation column B marks property as sold/closed/delisted",
                }
            )
    return excluded, {
        "status": "ok",
        "path": str(yhome_csv),
        "row_count": row_count,
        "excluded_count": len(excluded),
        "column_b_index": 1,
        "column_b_header": column_b_header,
        "column_b_rule": "Yhome Transition Reconciliation column B marks sold/closed/delisted properties",
        "column_b_marker_count": len(excluded),
        "column_b_rule_ok": len(excluded) > 0,
        "excluded_property_names": [row["property_name"] for row in excluded],
    }


def manual_exclusion_records(names: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
    return [
        {
            "source": "manual_exclusion",
            "property_name": name.strip(),
            "normalized_property": normalize(name),
            "exclude_reason": "manual do-not-update/do-not-email property exclusion",
        }
        for name in names
        if name.strip()
    ]


def sold_policy_exclusion_records(policy_path: Path | None = DEFAULT_LISTING_UPDATE_POLICY_PATH) -> list[dict[str, Any]]:
    if not policy_path or not policy_path.is_file():
        return []
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    records: list[dict[str, Any]] = []
    for field, default_reason in {
        "sold_ignore_listing_updates": "Lofty listing update policy marks property as sold/offboarded",
        "operational_ignore_listing_updates": "Lofty listing update policy marks property operationally excluded",
    }.items():
        values = policy.get(field) if isinstance(policy, dict) else None
        if not isinstance(values, list):
            continue
        for value in values:
            raw_value = value if isinstance(value, dict) else {}
            full_address = str(raw_value.get("address") or raw_value.get("property_name") or value or "").strip()
            property_name = full_address.split(",", 1)[0].strip()
            if not property_name:
                continue
            records.append(
                {
                    "source": field,
                    "property_name": property_name,
                    "full_address": full_address,
                    "normalized_property": normalize(property_name),
                    "exclude_reason": str(raw_value.get("reason") or default_reason),
                }
            )
    return records


def financial_hold_exclusion_records(report_path: Path | None) -> list[dict[str, Any]]:
    if not report_path or not report_path.is_file():
        return []
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    details = report.get("property_cash_review_details") if isinstance(report, dict) else None
    if not isinstance(details, list):
        return []
    records: list[dict[str, Any]] = []
    for detail in details:
        if not isinstance(detail, dict) or str(detail.get("source_clean_status") or "").strip().lower() == "ok":
            continue
        property_name = str(detail.get("property") or detail.get("property_name") or "").strip()
        if property_name:
            records.append(
                {
                    "source": "transfer_reconciliation_financial_hold",
                    "property_name": property_name,
                    "normalized_property": normalize(property_name),
                    "exclude_reason": "property financial truth is held pending source-cash review",
                }
            )
    return records


def guarded_apply_exclusion_records(report_path: Path | None) -> list[dict[str, Any]]:
    if not report_path or not report_path.is_file():
        return []
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    guarded_records = report.get("records") if isinstance(report, dict) else None
    if not isinstance(guarded_records, list):
        return []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    skipped_statuses = {"skipped_sold", "skipped_closed", "excluded_no_live_update_or_email"}
    for record in guarded_records:
        if not isinstance(record, dict):
            continue
        update_status = str(((record.get("updates") or {}).get("status")) or "").strip().lower()
        financial_status = str(((record.get("financials") or {}).get("status")) or "").strip().lower()
        if update_status not in skipped_statuses and financial_status not in skipped_statuses:
            continue
        property_path_text = str(record.get("property_path") or record.get("input_property_path") or "").strip()
        property_name = str(record.get("property_name") or "").strip()
        if not property_name and property_path_text:
            property_name = Path(property_path_text).name
            if property_name.lower() == "public":
                property_name = Path(property_path_text).parent.name
        normalized_property = normalize(property_name or property_path_text)
        if not normalized_property or normalized_property in seen:
            continue
        seen.add(normalized_property)
        notes = " ".join(
            str(((record.get(section) or {}).get("notes")) or "")
            for section in ("updates", "financials")
            if isinstance(record.get(section), dict)
        ).lower()
        source = "guarded_apply_exclusion"
        if "source=manual_exclusion" in notes:
            source = "manual_exclusion"
        elif "source=yhome_transition_reconciliation" in notes:
            source = "yhome_transition_reconciliation"
        elif "source=sold_ignore_listing_updates" in notes:
            source = "sold_ignore_listing_updates"
        elif update_status.startswith("skipped_") or financial_status.startswith("skipped_"):
            source = "monthly_index_skipped"
        records.append(
            {
                "source": source,
                "property_name": property_name or property_path_text,
                "property_path": property_path_text,
                "normalized_property": normalized_property,
                "index_status": record.get("index_status"),
                "exclude_reason": "guarded apply marked property skipped/excluded; no live update or owner email",
            }
        )
    return records


def match_exclusion_guard(property_path: Path, guards: list[dict[str, Any]]) -> dict[str, Any] | None:
    target_names = [property_path.name]
    if property_path.name.lower() == "public":
        target_names.append(property_path.parent.name)
    targets = [normalize(name) for name in target_names]
    targets = [target for target in targets if target]
    if not targets:
        return None
    matches: list[tuple[int, dict[str, Any]]] = []
    for guard in guards:
        key = str(guard.get("normalized_property") or "").strip()
        # A terminal Public directory is a document-root alias, not a property
        # identity. Never let a malformed guarded-apply row exclude the portfolio.
        if not key or key == "public":
            continue
        for target in targets:
            if policy_name_matches(target, key):
                matches.append((len(key) + (1000 if key == target else 0), guard))
    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1] if matches else None


def append_unmapped_exclusion_records(
    records: list[dict[str, Any]],
    exclusions: list[dict[str, Any]],
    represented_records: list[dict[str, Any]] | None = None,
) -> None:
    existing_keys = {
        normalize(str(record.get("property_name") or record.get("property_path") or ""))
        for record in [*records, *(represented_records or [])]
        if isinstance(record, dict)
    }
    for exclusion in exclusions:
        key = str(exclusion.get("normalized_property") or "").strip()
        if not key:
            continue
        if any(policy_name_matches(existing, key) for existing in existing_keys if existing):
            continue
        property_name = str(exclusion.get("property_name") or exclusion.get("property_path") or "").strip()
        records.append(
            {
                "status": "excluded_no_live_update_or_email",
                "raw_status": str(exclusion.get("index_status") or ""),
                "property_path": str(exclusion.get("property_path") or ""),
                "property_name": property_name,
                "exclude_source": exclusion.get("source"),
                "exclude_reason": exclusion.get("exclude_reason"),
                "matched_exclusion_property": exclusion.get("property_name"),
                "yhome_column_b": exclusion.get("yhome_column_b"),
                "path_resolution_status": "guarded_apply_exclusion_only",
            }
        )
        existing_keys.add(key)


def monthly_exclusion_guards(
    yhome_csv: Path | None,
    manual_property_names: list[str] | tuple[str, ...] = DEFAULT_MANUAL_EXCLUDED_PROPERTIES,
    policy_path: Path | None = DEFAULT_LISTING_UPDATE_POLICY_PATH,
    include_sold_policy: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    yhome_exclusions, yhome_guard = load_yhome_transition_exclusions(yhome_csv)
    manual_exclusions = manual_exclusion_records(manual_property_names)
    sold_policy_exclusions = sold_policy_exclusion_records(policy_path) if include_sold_policy else []
    return [*yhome_exclusions, *manual_exclusions, *sold_policy_exclusions], yhome_guard, manual_exclusions
