#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path


PROPERTY_UPDATE_RE = re.compile(r"(?mi)^\s*-\s+\*{0,2}\s*Property Update\s*\((\d{2})/(\d{2})/(\d{4})\):")
DATED_HEADING_RE = re.compile(r"(?m)^##\s+(\d{4}-\d{2}-\d{2})\s*$")
PROPERTY_UPDATES_HEADER_RE = re.compile(r"(?mi)^#\s+Property Updates\s*$")
MAX_LISTING_UPDATE_CHARS = 3500
MAX_LISTING_UPDATE_LINES = 80


def load_ready_names(path: Path | None) -> set[str] | None:
    if path is None:
        return None
    names: set[str] = set()
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            name = (row.get("property_name") or "").strip()
            if name:
                names.add(name)
    return names


def is_bounded_latest_listing_update(text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    if len(text) > MAX_LISTING_UPDATE_CHARS:
        return False
    if len(text.splitlines()) > MAX_LISTING_UPDATE_LINES:
        return False
    if "# Property Updates" in text:
        return False
    if DATED_HEADING_RE.search(text):
        return False
    return len(PROPERTY_UPDATE_RE.findall(text)) == 1


def update_date_from_listing_text(text: str) -> str | None:
    match = PROPERTY_UPDATE_RE.search(text)
    if not match:
        return None
    month, day, year = match.groups()
    return f"{year}-{month}-{day}"


def replace_or_prepend_section(existing: str, update_date: str, listing_text: str) -> str:
    existing = existing.replace("\r\n", "\n")
    listing_text = listing_text.strip()
    section = f"## {update_date}\n\n{listing_text}\n"
    pattern = re.compile(rf"(?ms)^##\s+{re.escape(update_date)}\s*$.*?(?=^##\s+\d{{4}}-\d{{2}}-\d{{2}}\s*$|\Z)")
    without_existing_date = pattern.sub("", existing).strip()
    without_headers = PROPERTY_UPDATES_HEADER_RE.sub("", without_existing_date).strip()
    return ("# Property Updates\n\n" + section + ("\n" + without_headers if without_headers else "")).rstrip() + "\n"


def reconcile(args: argparse.Namespace) -> dict:
    capture = json.loads(args.capture_report.read_text(encoding="utf-8"))
    ready_names = load_ready_names(args.ready_csv)
    records = []
    changed = 0
    skipped = 0

    for record in capture.get("records", []):
        property_name = str(record.get("property_name") or "").strip()
        if ready_names is not None and property_name not in ready_names:
            skipped += 1
            records.append({"property_name": property_name, "status": "skipped_not_in_ready_csv"})
            continue

        updates_md = Path(str(record.get("updates_md") or ""))
        snapshot_path = Path(str(record.get("snapshot_path") or ""))
        if not updates_md.is_file() or not snapshot_path.is_file():
            skipped += 1
            records.append({"property_name": property_name, "status": "skipped_missing_file"})
            continue

        listing_text = snapshot_path.read_text(encoding="utf-8", errors="replace").strip()
        if not is_bounded_latest_listing_update(listing_text):
            skipped += 1
            records.append({"property_name": property_name, "status": "skipped_unsafe_live_snapshot"})
            continue

        update_date = update_date_from_listing_text(listing_text)
        if not update_date or (args.month and not update_date.startswith(args.month + "-")):
            skipped += 1
            records.append(
                {
                    "property_name": property_name,
                    "status": "skipped_wrong_month",
                    "update_date": update_date,
                }
            )
            continue

        existing = updates_md.read_text(encoding="utf-8", errors="replace")
        updated = replace_or_prepend_section(existing, update_date, listing_text)
        record_status = "unchanged" if updated == existing else "would_update"
        if args.apply and updated != existing:
            updates_md.write_text(updated, encoding="utf-8")
            record_status = "updated"
            changed += 1
        elif updated == existing:
            skipped += 1
        else:
            changed += 1
        records.append(
            {
                "property_name": property_name,
                "status": record_status,
                "updates_md": str(updates_md),
                "snapshot_path": str(snapshot_path),
                "update_date": update_date,
                "listing_update_chars": len(listing_text),
                "listing_update_lines": len(listing_text.splitlines()),
            }
        )

    status = "ok" if changed or all(r["status"].startswith(("unchanged", "skipped")) for r in records) else "review"
    return {
        "status": status,
        "mode": "apply" if args.apply else "dry_run",
        "capture_report": str(args.capture_report),
        "ready_csv": str(args.ready_csv) if args.ready_csv else None,
        "month": args.month,
        "changed_or_would_change_count": changed,
        "skipped_or_unchanged_count": skipped,
        "record_count": len(records),
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile local UPDATES.md latest section from bounded live Lofty listing snapshots.")
    parser.add_argument("--capture-report", type=Path, required=True)
    parser.add_argument("--ready-csv", type=Path)
    parser.add_argument("--month", help="Require listing update date to be in YYYY-MM.")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    result = reconcile(args)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("status", "mode", "changed_or_would_change_count", "skipped_or_unchanged_count", "record_count")}, indent=2))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
