#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import shutil
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from lofty_index_status import is_active_index_status
from lofty_monthly_exclusions import (
    DEFAULT_MANUAL_EXCLUDED_PROPERTIES,
    match_exclusion_guard,
    monthly_exclusion_guards,
)
from lofty_property_paths import public_dir_for_property, resolve_index_property_path


OWNER_STATEMENTS_DIR = "07 - P&L & Owner Statements"
SNAPSHOT_DIR = "00 - README & Property Snapshot"
LISTING_DIR = "13 - Listing"
LEGACY_UPDATES_DIR = "Updates"


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def newest_file(paths: list[Path]) -> Path | None:
    existing = [path for path in paths if path.is_file()]
    if not existing:
        return None
    return max(existing, key=lambda path: (path.stat().st_mtime, str(path)))


def markdown_candidates(directory: Path, patterns: list[str]) -> list[Path]:
    candidates: list[Path] = []
    if not directory.is_dir():
        return candidates
    for pattern in patterns:
        candidates.extend(path for path in directory.glob(pattern) if path.is_file())
    return candidates


def usable_local_file(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    if stat.st_size <= 0:
        return False
    if stat.st_size > 0 and getattr(stat, "st_blocks", 1) == 0:
        return False
    return True


def find_verified_financial_source(public_dir: Path, canonical_financials: Path) -> tuple[Path | None, str | None]:
    owner_dir = public_dir / OWNER_STATEMENTS_DIR
    snapshot_dir = public_dir / SNAPSHOT_DIR
    listing_dir = public_dir / LISTING_DIR

    source_groups: list[tuple[str, list[Path]]] = [
        (
            "canonical_owner_statement_markdown",
            markdown_candidates(
                owner_dir,
                [
                    "Financials Summary*.md",
                    "YTD P&L Summary*.md",
                    "P&L*.md",
                    "Profit*.md",
                    "Cash Flow*.md",
                ],
            ),
        ),
        (
            "snapshot_financials_markdown",
            [
                snapshot_dir / "FINANCIALS.md",
                *markdown_candidates(snapshot_dir, ["Financials Summary*.md", "P&L*.md"]),
            ],
        ),
        (
            "listing_financials_markdown",
            [
                listing_dir / "FINANCIALS.md",
                *markdown_candidates(listing_dir, ["Financials Summary*.md", "P&L*.md"]),
            ],
        ),
        (
            "public_root_financials_markdown",
            [
                public_dir / "FINANCIALS.md",
                *markdown_candidates(public_dir, ["Financials Summary*.md", "P&L*.md"]),
            ],
        ),
    ]
    for source_kind, candidates in source_groups:
        source = newest_file([path for path in candidates if path.resolve() != canonical_financials.resolve() and usable_local_file(path)])
        if source:
            return source, source_kind
    return None, None


def find_legacy_updates_source(public_dir: Path, canonical_updates: Path) -> Path | None:
    legacy_updates = public_dir / LEGACY_UPDATES_DIR / "UPDATES.md"
    if legacy_updates.is_file() and legacy_updates.resolve() != canonical_updates.resolve():
        return legacy_updates
    return None


def record_write_failure(record: dict[str, Any], target: str, source: Path | None, exc: Exception) -> None:
    record["remaining"].append(
        {
            "target": target,
            "status": "write_failed",
            "source": str(source) if source else None,
            "error": str(exc),
        }
    )


def structured_financial_candidates(property_path: Path, public_dir: Path) -> list[tuple[Path, str]]:
    owner_dir = public_dir / OWNER_STATEMENTS_DIR
    candidates: list[tuple[Path, str]] = []
    for directory in [owner_dir]:
        if directory.is_dir():
            for path in sorted(directory.glob("ECO Systems General Ledger*.csv")):
                candidates.append((path, "eco_general_ledger_csv"))
    pnl_dirs = [owner_dir]
    for directory in pnl_dirs:
        if directory.is_dir():
            for path in sorted(directory.glob("P&L Statement*.xlsx")):
                candidates.append((path, "pnl_statement_xlsx"))
    return candidates


def money(value: Decimal) -> str:
    sign = "-" if value < 0 else ""
    absolute = abs(value)
    return f"{sign}${absolute:,.2f}"


def parse_decimal(value: str) -> Decimal:
    try:
        return Decimal(str(value or "0").replace(",", "").replace("$", "").strip() or "0")
    except InvalidOperation:
        return Decimal("0")


def render_csv_ledger_markdown(source: Path, property_name: str) -> str:
    rows: list[dict[str, str]] = []
    with source.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows.append(row)
    total_inflow = Decimal("0")
    total_outflow = Decimal("0")
    by_type: dict[str, Decimal] = {}
    by_category: dict[str, Decimal] = {}
    dated_rows = 0
    for row in rows:
        amount = parse_decimal(row.get("Amount", "0"))
        if amount >= 0:
            total_inflow += amount
        else:
            total_outflow += amount
        type_key = (row.get("Type") or "Unclassified").strip() or "Unclassified"
        category_key = (row.get("Category") or "Unclassified").strip() or "Unclassified"
        by_type[type_key] = by_type.get(type_key, Decimal("0")) + amount
        by_category[category_key] = by_category.get(category_key, Decimal("0")) + amount
        if row.get("Date"):
            dated_rows += 1

    def table(title: str, data: dict[str, Decimal], limit: int = 20) -> list[str]:
        lines = [f"## {title}", "", "| Line item | Amount |", "|---|---:|"]
        for key, value in sorted(data.items(), key=lambda item: (item[0] == "Unclassified", item[0].lower()))[:limit]:
            lines.append(f"| {key} | {money(value)} |")
        lines.append("")
        return lines

    generated = iso_z()
    lines = [
        "# Financial Data",
        "",
        f"**Property:** {property_name}",
        f"**Source:** `{source}`",
        f"**Generated:** {generated}",
        "",
        "This canonical file was generated from a local verified ECO Systems general ledger CSV because no reviewed Markdown `FINANCIALS.md` source existed yet.",
        "",
        "## Ledger Summary",
        "",
        "| Metric | Amount |",
        "|---|---:|",
        f"| Total inflows | {money(total_inflow)} |",
        f"| Total outflows | {money(total_outflow)} |",
        f"| Net cash flow | {money(total_inflow + total_outflow)} |",
        f"| Ledger rows | {len(rows)} |",
        f"| Rows with dates | {dated_rows} |",
        "",
    ]
    lines.extend(table("By Type", by_type))
    lines.extend(table("By Category", by_category))
    lines.extend(
        [
            "## Review Notes",
            "",
            "- Generated deterministically from local ledger data; review before investor email/publish.",
            "- No tenant ledger rows are included in this summary.",
            "",
        ]
    )
    return "\n".join(lines)


def render_xlsx_markdown(source: Path, property_name: str) -> str:
    try:
        import openpyxl  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"openpyxl unavailable for {source}: {exc}") from exc
    workbook = openpyxl.load_workbook(source, read_only=True, data_only=True)
    sheet = workbook.worksheets[0]
    rows: list[list[Any]] = []
    for row in sheet.iter_rows(values_only=True):
        values = [cell for cell in row]
        if any(value not in (None, "") for value in values):
            rows.append(values)
        if len(rows) >= 120:
            break
    max_cols = min(max((len(row) for row in rows), default=0), 12)
    lines = [
        "# Financial Data",
        "",
        f"**Property:** {property_name}",
        f"**Source:** `{source}`",
        f"**Generated:** {iso_z()}",
        "",
        "This canonical file was generated from a local verified P&L workbook because no reviewed Markdown `FINANCIALS.md` source existed yet.",
        "",
        f"## Workbook Sheet: {sheet.title}",
        "",
    ]
    if rows and max_cols:
        lines.append("| " + " | ".join(str(value if value is not None else "") for value in rows[0][:max_cols]) + " |")
        lines.append("|" + "|".join("---" for _ in range(max_cols)) + "|")
        for row in rows[1:]:
            padded = [row[index] if index < len(row) else "" for index in range(max_cols)]
            lines.append("| " + " | ".join(str(value if value is not None else "") for value in padded) + " |")
    lines.extend(["", "## Review Notes", "", "- Generated deterministically from local workbook data; review before investor email/publish.", ""])
    return "\n".join(lines)


def generate_structured_financials(property_path: Path, public_dir: Path) -> tuple[str | None, Path | None, str | None, str | None]:
    for source, source_kind in structured_financial_candidates(property_path, public_dir):
        try:
            if source_kind == "eco_general_ledger_csv":
                return render_csv_ledger_markdown(source, property_path.name), source, source_kind, None
            if source_kind == "pnl_statement_xlsx":
                return render_xlsx_markdown(source, property_path.name), source, source_kind, None
        except Exception as exc:  # noqa: BLE001
            last_error = f"{source_kind} failed for {source}: {exc}"
            continue
    return None, None, None, locals().get("last_error")


def bootstrap_record(row: dict[str, str], apply: bool, exclusion_guards: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    property_path, path_resolution = resolve_index_property_path(row)
    public_dir = public_dir_for_property(property_path)
    updates_md = public_dir / SNAPSHOT_DIR / "UPDATES.md"
    canonical_financials = public_dir / OWNER_STATEMENTS_DIR / "FINANCIALS.md"
    record: dict[str, Any] = {
        "property_path": str(property_path),
        "property_name": property_path.name,
        "updates_md": str(updates_md),
        "financials_md": str(canonical_financials),
        "actions": [],
        "remaining": [],
        **path_resolution,
    }
    exclusion = match_exclusion_guard(property_path, exclusion_guards or [])
    if exclusion:
        record.update(
            {
                "excluded": True,
                "exclude_source": exclusion.get("source"),
                "exclude_reason": exclusion.get("exclude_reason"),
                "matched_exclusion_property": exclusion.get("property_name"),
            }
        )
        record["actions"].append(
            {
                "target": "property",
                "status": "skipped_excluded_property",
                "source": exclusion.get("source"),
                "apply": False,
            }
        )
        return record

    if updates_md.is_file():
        record["actions"].append({"target": "updates", "status": "exists"})
    else:
        legacy_updates = find_legacy_updates_source(public_dir, updates_md)
        if legacy_updates:
            record["actions"].append(
                {
                    "target": "updates",
                    "status": "copy_from_legacy_updates_source",
                    "source": str(legacy_updates),
                    "apply": apply,
                }
            )
            if apply:
                try:
                    updates_md.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(legacy_updates, updates_md)
                except OSError as exc:
                    record_write_failure(record, "updates", legacy_updates, exc)
        else:
            record["actions"].append({"target": "updates", "status": "create_empty", "apply": apply})
            if apply:
                try:
                    updates_md.parent.mkdir(parents=True, exist_ok=True)
                    updates_md.write_text("", encoding="utf-8")
                except OSError as exc:
                    record_write_failure(record, "updates", None, exc)

    if canonical_financials.is_file() and usable_local_file(canonical_financials):
        record["actions"].append({"target": "financials", "status": "exists"})
    else:
        if canonical_financials.is_file():
            record["actions"].append({"target": "financials", "status": "replace_unusable_existing", "apply": apply})
        source, source_kind = find_verified_financial_source(public_dir, canonical_financials)
        if source:
            record["actions"].append(
                {
                    "target": "financials",
                    "status": "copy_from_existing_local_source",
                    "source": str(source),
                    "source_kind": source_kind,
                    "apply": apply,
                }
            )
            if apply:
                try:
                    canonical_financials.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, canonical_financials)
                except OSError as exc:
                    record_write_failure(record, "financials", source, exc)
        else:
            generated_text, generated_source, generated_source_kind, generated_error = generate_structured_financials(property_path, public_dir)
            if generated_text and generated_source:
                record["actions"].append(
                    {
                        "target": "financials",
                        "status": "generate_from_structured_local_source",
                        "source": str(generated_source),
                        "source_kind": generated_source_kind,
                        "apply": apply,
                    }
                )
                if apply:
                    try:
                        canonical_financials.parent.mkdir(parents=True, exist_ok=True)
                        canonical_financials.write_text(generated_text, encoding="utf-8")
                    except OSError as exc:
                        record_write_failure(record, "financials", generated_source, exc)
            else:
                record["remaining"].append(
                    {
                        "target": "financials",
                        "status": "missing_verified_local_source",
                        "note": "Requires Lofty live extraction or Baselane/Hemlane verified source before creating FINANCIALS.md.",
                        "structured_source_error": generated_error,
                    }
                )

    return record


def summarize(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        for action in record["actions"]:
            key = f"{action['target']}.{action['status']}"
            counts[key] = counts.get(key, 0) + 1
        for remaining in record["remaining"]:
            key = f"{remaining['target']}.{remaining['status']}"
            counts[key] = counts.get(key, 0) + 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Idempotently bootstrap local public Lofty doc targets for guarded monthly updates.")
    parser.add_argument("--index-csv", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--yhome-transition-csv", type=Path)
    parser.add_argument("--exclude-property", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    issues: list[str] = []
    records: list[dict[str, Any]] = []
    exclusion_guards, yhome_guard, manual_exclusions = monthly_exclusion_guards(
        args.yhome_transition_csv,
        [*DEFAULT_MANUAL_EXCLUDED_PROPERTIES, *args.exclude_property],
    )
    if not args.index_csv.is_file():
        issues.append(f"monthly index missing: {args.index_csv}")
    else:
        with args.index_csv.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if not is_active_index_status(row.get("status")):
                    continue
                records.append(bootstrap_record(row, args.apply, exclusion_guards))

    counts = summarize(records)
    status = "failed" if issues else "review" if any(key.endswith(".missing_verified_local_source") or key.endswith(".write_failed") for key in counts) else "ok"
    excluded_records = [record for record in records if record.get("excluded")]
    report = {
        "generated_at": iso_z(),
        "apply": args.apply,
        "status": status,
        "issues": issues,
        "counts": counts,
        "record_count": len(records),
        "excluded_property_count": len(excluded_records),
        "excluded_property_names": [record["property_name"] for record in excluded_records],
        "manual_excluded_property_names": [record["property_name"] for record in manual_exclusions],
        "yhome_transition_guard": yhome_guard,
        "records": records,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("status", "counts", "record_count", "issues")}, indent=2, sort_keys=True))
    return 0 if status == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
