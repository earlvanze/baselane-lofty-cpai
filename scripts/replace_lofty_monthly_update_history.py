#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATE_HEADING = re.compile(r"(?m)^## (\d{4}-\d{2}-\d{2})\s*$")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def dated_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    matches = list(DATE_HEADING.finditer(text))
    prefix = text[: matches[0].start()] if matches else text
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append((match.group(1), text[match.start() : end].strip()))
    return prefix, sections


def normalized_candidate(text: str, entry_date: str) -> str:
    candidate = text.strip()
    expected = f"## {entry_date}"
    if not candidate.startswith(expected):
        raise ValueError(f"candidate does not start with {expected!r}")
    _, sections = dated_sections(candidate)
    if len(sections) != 1 or sections[0][0] != entry_date:
        raise ValueError("candidate must contain exactly one dated section")
    return sections[0][1]


def replace_dates(
    current: str,
    candidate: str,
    *,
    entry_date: str,
    remove_dates: set[str],
) -> tuple[str, list[str]]:
    prefix, sections = dated_sections(current)
    if not sections:
        raise ValueError("UPDATES.md contains no dated sections")
    kept = [(date, section) for date, section in sections if date not in remove_dates]
    removed = [date for date, _ in sections if date in remove_dates]
    header = prefix.strip() or "# Property Updates"
    rendered = "\n\n".join([header, candidate, *(section for _, section in kept)]).rstrip() + "\n"
    _, after_sections = dated_sections(rendered)
    after_dates = [date for date, _ in after_sections]
    if after_dates.count(entry_date) != 1:
        raise ValueError(f"expected exactly one {entry_date} section after replacement")
    for date in remove_dates - {entry_date}:
        if date in after_dates:
            raise ValueError(f"date {date} remains after replacement")
    before_kept = [(date, sha256_text(section)) for date, section in kept]
    after_kept = [
        (date, sha256_text(section))
        for date, section in after_sections
        if date != entry_date
    ]
    if before_kept != after_kept:
        raise ValueError("non-target dated sections changed during replacement")
    return rendered, removed


def atomic_write(path: Path, text: str) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replace selected dated Lofty history sections with an approved monthly candidate."
    )
    parser.add_argument("--target-map", type=Path, required=True)
    parser.add_argument("--candidate-packet", type=Path, required=True)
    parser.add_argument("--entry-date", required=True)
    parser.add_argument("--remove-date", action="append", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    targets = load_json(args.target_map).get("records") or []
    candidates = load_json(args.candidate_packet).get("records") or []
    candidates_by_path = {str(row.get("property_path") or ""): row for row in candidates}
    remove_dates = set(args.remove_date) | {args.entry_date}
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

    for target in targets:
        property_path = str(target.get("property_path") or "")
        property_name = str(target.get("property_name") or property_path)
        updates_md = Path(str(target.get("updates_md") or ""))
        try:
            record = candidates_by_path[property_path]
            candidate_path = Path(str(record.get("update_candidate") or ""))
            current = updates_md.read_text(encoding="utf-8")
            candidate = normalized_candidate(
                candidate_path.read_text(encoding="utf-8"), args.entry_date
            )
            rendered, removed = replace_dates(
                current,
                candidate,
                entry_date=args.entry_date,
                remove_dates=remove_dates,
            )
            changed = rendered != current
            if args.apply and changed:
                atomic_write(updates_md, rendered)
            readback = updates_md.read_text(encoding="utf-8") if args.apply else rendered
            if readback != rendered:
                raise ValueError("post-write readback differs from prepared history")
            rows.append(
                {
                    "property_name": property_name,
                    "property_path": property_path,
                    "lofty_property_id": target.get("lofty_property_id"),
                    "updates_md": str(updates_md),
                    "candidate": str(candidate_path),
                    "removed_dates": removed,
                    "changed": changed,
                    "applied": bool(args.apply and changed),
                    "before_sha256": sha256_text(current),
                    "after_sha256": sha256_text(readback),
                }
            )
        except Exception as exc:  # noqa: BLE001
            failures.append({"property_name": property_name, "error": f"{type(exc).__name__}: {exc}"})

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": "apply" if args.apply else "preview",
        "entry_date": args.entry_date,
        "remove_dates": sorted(remove_dates),
        "target_count": len(targets),
        "success_count": len(rows),
        "failure_count": len(failures),
        "changed_count": sum(1 for row in rows if row["changed"]),
        "applied_count": sum(1 for row in rows if row["applied"]),
        "properties": rows,
        "failures": failures,
        "status": "ok" if len(rows) == len(targets) and not failures else "review",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
