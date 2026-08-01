#!/usr/bin/env python3
"""Audit CF balance-sheet rows and prepare the non-authoritative Yhome work product.

This is intentionally report-first:
- CF workbook cells are never modified here.
- Google Sheet writes are not performed here.
- The Yhome output is a deterministic update plan for the two approved columns:
  Lofty Operating Cash and ECO Net DAO Funds.
- Yhome issues are reported separately and never change the authoritative
  CF/GL audit status.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import zipfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from canonical_property_ledger import DivergentCanonicalLedgerError, resolve_equivalent_ledgers
try:
    from coownership_reserve_policy import LOCAL_FINANCIALS_ONLY_PROPERTIES, canonical_property as canonical_reserve_property
    from lofty_property_paths import resolve_property_path
    from lofty_monthly_exclusions import (
        DEFAULT_MANUAL_EXCLUDED_PROPERTIES,
        match_exclusion_guard,
        monthly_exclusion_guards,
    )
except ImportError:
    LOCAL_FINANCIALS_ONLY_PROPERTIES = ()
    canonical_reserve_property = lambda value: None
    resolve_property_path = lambda path: (path, {})
    DEFAULT_MANUAL_EXCLUDED_PROPERTIES = (
        "3560 Saint Albans Rd",
        "1935 S Glen Rd",
        "402 N Wild Olive Ave",
        "9919 S Oglesby",
    )
    match_exclusion_guard = None
    monthly_exclusion_guards = None



CONFLICT_THRESHOLD = 0.01
OWNER_STATEMENTS_DIR = "07 - P&L & Owner Statements"
LOFTY_OR_LABELS = (
    "Lofty Operating Reserve (OR) Balance",
    "Operating Reserve (OR) Balance",
)
ECO_CASH_LABEL = "ECO Operating Cash"
ECO_GL_LABEL = "ECO General Ledger (ECO GL Column E Total)"
ECO_GL_LABELS = (
    ECO_GL_LABEL,
    "ECO Operating Cash (ECO GL Column E Total)",
    "ECO GL Net Cash Balance (excl. EARLDAO Interest)",
)
RETAINED_EARNINGS_LABEL = "Retained Earnings"
# June 2026 cash flow for Umland was retained locally rather than distributed
# in July, so the corresponding Lofty reserve must remain blank.
RETAINED_EARNINGS_LOFTY_EXEMPTIONS = {
    ("22164 umland cir jenner ca 95450", "2026-06"),
}
ECO_CASH_LABELS = (ECO_CASH_LABEL,)
YHOME_LOFTY_CASH_COLUMN = "Lofty Operating Cash"
YHOME_ECO_CASH_COLUMN = "ECO Net DAO Funds"
YHOME_NEW_PM_COLUMN = "New PM"
YHOME_PROPERTY_COLUMN = "Property"
YHOME_SHEET_TITLE_COLUMN = "__yhome_sheet_title"
YHOME_SHEET_GID_COLUMN = "__yhome_sheet_gid"
YHOME_SHEET_ROW_COLUMN = "__yhome_sheet_row_number"
# The refreshed Yhome Transition Reconciliation export currently contains
# property rows for Ohio and Illinois.  Other active DAOs are reconciled by
# their own source lanes and must not be forced into this sheet by state alone.
YHOME_REQUIRED_STATES = {"OH", "IL"}
INACTIVE_STATUS_MARKERS = ("sold", "selling", "closed", "delisted")
DEFAULT_SOURCE_CASH_REPORT = Path("reports/baselane_daily_source_cash_balance_report.json")
XML_NS = {
    "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def parse_month(value: str | None) -> tuple[int, int]:
    raw = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}", raw):
        raise argparse.ArgumentTypeError("month must be YYYY-MM")
    year_text, month_text = raw.split("-")
    return int(year_text), int(month_text)


def source_cash_mode_for_month(year: int, month: int, today: date | None = None) -> str:
    # The full canonical property GL is an internal accounting control,
    # including accruals, regardless of report month. It is not custody cash.
    return "full_column_e"


def generated_at() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_header(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def normalize_property_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\blfty\d+\b", " ", text)
    text = re.sub(r"\bpublic\b", " ", text)
    text = text.replace("&", " and ")
    replacements = {
        "avenue": "ave",
        "street": "st",
        "road": "rd",
        "lane": "ln",
        "drive": "dr",
        "place": "pl",
        "north": "n",
        "south": "s",
        "east": "e",
        "west": "w",
    }
    for source, target in replacements.items():
        text = re.sub(rf"\b{source}\b", target, text)
    street_match = re.match(
        r"^\s*(.*?\b(?:st|ave|rd|ln|dr|blvd|pl|ct|pkwy|ter)\b)",
        text,
        flags=re.IGNORECASE,
    )
    if street_match:
        text = street_match.group(1)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def property_state_from_path(path: Path) -> str | None:
    parts = {part.upper() for part in path.parts}
    states = ("AL", "AR", "CA", "CO", "FL", "GA", "HI", "IA", "IL", "MI", "MO", "NY", "OH", "SC", "TN", "TX", "UT", "WA")
    for state in states:
        if state in parts:
            return state
    # A few canonical Dropbox property roots omit the state directory. Their
    # address-bearing directory name remains authoritative for Yhome scope.
    path_text = " / ".join(path.parts)
    address_state = re.search(r",\s*([A-Z]{2})\s+\d{5}(?:-\d{4})?(?:\b|$)", path_text, flags=re.IGNORECASE)
    if address_state and address_state.group(1).upper() in states:
        return address_state.group(1).upper()
    return None


def is_canonical_property_split_source(path: Path) -> bool:
    parts = [part.casefold() for part in path.parts]
    filename = path.name.casefold()
    return (
        path.is_file()
        and filename.startswith("eco systems general ledger")
        and "real estate" in parts
        and any(part == "public" or part.endswith(" public") for part in parts)
        and "07 - p&l & owner statements" in parts
    )


def ledger_component_key(path: Path) -> str:
    """Return the property component represented by a property-split ledger."""
    prefix = "eco systems general ledger - "
    stem = path.stem.casefold()
    tail = stem[len(prefix) :] if stem.startswith(prefix) else stem
    return normalize_property_name(tail.split(" - ", 1)[0].strip(" ."))


def canonical_nonpackage_ledger_path(source_path: Path) -> Path:
    """Choose one current split ledger for a single-property DAO.

    Baselane exports can leave an older account-column CSV beside the newer
    property-split CSV.  Both are Dropbox Public/07 files, so selecting the
    candidate-packet path directly lets file creation order make CF workbook
    values diverge from transfer reporting. Prefer the most complete
    same-property ledger, then use recency only as a tie-breaker, so a freshly
    written one-row accrual fragment cannot replace a complete ledger.
    """
    if not is_canonical_property_split_source(source_path):
        return source_path
    component = ledger_component_key(source_path)
    candidates = [
        path
        for path in source_path.parent.glob("ECO Systems General Ledger*.csv")
        if is_canonical_property_split_source(path)
        and ledger_component_key(path) == component
        and not any(marker in path.name.casefold() for marker in (".bak", "backup", "conflict"))
    ]
    if not candidates:
        return source_path
    return resolve_equivalent_ledgers(candidates)


def property_name_from_cf_file(path: Path) -> str:
    name = Path(path).stem
    prefix = "Cash Flow Statement - "
    if name.startswith(prefix):
        name = name[len(prefix):]
    return name.strip()


def property_tokens(value: Any) -> set[str]:
    return set(normalize_property_name(value).split())


def is_manually_excluded_property(property_name: Any) -> bool:
    normalized = normalize_property_name(property_name)
    return any(
        normalize_property_name(excluded) in normalized
        for excluded in DEFAULT_MANUAL_EXCLUDED_PROPERTIES
        if normalize_property_name(excluded)
    )


def exclusion_guard_for_record(record: dict[str, Any], guards: list[dict[str, Any]]) -> dict[str, Any] | None:
    property_path = Path(str(record.get("property_path") or record.get("input_property_path") or ""))
    property_name = str(record.get("property_name") or record.get("input_property_name") or "")
    target = property_path if property_path.name else Path(property_name)
    return match_exclusion_guard(target, guards) if match_exclusion_guard else None


def financial_audit_exclusion_guard(record: dict[str, Any], guards: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Exclude only disposed properties from balance-sheet reconciliation.

    Listing-update exclusions are broader than financial scope. In particular,
    a transitioned package can be out of Lofty/Discord/email publication while
    its active DAO still requires Cash Flow and ECO cash reconciliation.
    """
    guard = exclusion_guard_for_record(record, guards)
    if guard and str(guard.get("source") or "") == "sold_ignore_listing_updates":
        return guard
    return None


def cf_candidate_priority_for_property(path: Path, prop_dir_name: str) -> tuple[Any, ...]:
    filename = normalize_property_name(property_name_from_cf_file(path))
    folder = normalize_property_name(prop_dir_name)
    filename_tokens = property_tokens(filename)
    folder_tokens = property_tokens(folder)
    shared_tokens = filename_tokens & folder_tokens
    exact_match = bool(filename and folder and filename == folder)
    contained_match = bool(filename and folder and (filename in folder or folder in filename))
    has_city_context = bool(folder_tokens and len(shared_tokens) >= min(len(folder_tokens), 3))
    return (
        0 if exact_match else 1,
        0 if contained_match else 1,
        0 if has_city_context else 1,
        -len(shared_tokens),
        str(path).lower(),
    )


def parse_money(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = str(value).strip()
    if not text or text in {"-", "—"}:
        return None
    negative = "(" in text and ")" in text
    text = text.replace("$", "").replace(",", "").replace("(", "").replace(")", "").strip()
    if not text or text in {"-", "—"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return round(-number if negative else number, 2)


def parse_amount_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"-", "—"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    normalized = text.replace("$", "").replace(",", "").replace("(", "").replace(")", "").strip()
    if not normalized or normalized in {"-", "—"}:
        return None
    try:
        amount = Decimal(normalized)
    except InvalidOperation:
        return None
    return -amount if negative else amount


def invalidate_eco_source(source: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
    source["eco_gl_column_e_sum"] = None
    source["eco_general_ledger_sum"] = None
    source["eco_operating_cash"] = None
    source["eco_gl_column_e_status"] = status
    source["eco_gl_column_e_scope"] = None
    source["eco_gl_column_e_source_mode"] = "canonical_source_invalid"
    source["eco_gl_column_e_source_error"] = reason
    return source


def money(value: float | int | None) -> str:
    if value is None:
        return ""
    number = round(float(value), 2)
    if number < 0:
        return f"$({abs(number):,.2f})"
    return f"${number:,.2f}"


def diff_amount(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return round(float(left) - float(right), 2)


def is_formula(value: Any) -> bool:
    return isinstance(value, str) and value.strip().startswith("=")


def parse_month_header(value: Any) -> tuple[int, int] | None:
    if isinstance(value, datetime):
        return value.year, value.month
    if isinstance(value, (int, float)) and 20000 <= float(value) <= 80000:
        parsed = datetime(1899, 12, 30) + timedelta(days=float(value))
        return parsed.year, parsed.month
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%b-%y", "%B-%y", "%b %y", "%B %y", "%b-%Y", "%B-%Y", "%b %Y", "%B %Y", "%Y-%m"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.year, parsed.month
    return None


def column_letters_to_number(column_letters: str) -> int:
    number = 0
    for char in column_letters:
        if not char.isalpha():
            continue
        number = number * 26 + (ord(char.upper()) - ord("A") + 1)
    return number


def column_number_to_letters(column_number: int) -> str:
    letters = ""
    while column_number:
        column_number, remainder = divmod(column_number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def split_coordinate(coordinate: str) -> tuple[str, int]:
    column = "".join(char for char in coordinate if char.isalpha())
    row_text = "".join(char for char in coordinate if char.isdigit())
    return column, int(row_text or "0")


def load_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    values = []
    for item in root.findall("a:si", XML_NS):
        pieces = [text_node.text or "" for text_node in item.findall(".//a:t", XML_NS)]
        values.append("".join(pieces))
    return values


def workbook_sheet_targets(archive: zipfile.ZipFile) -> dict[str, str]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    relationship_map = {
        relationship.attrib["Id"]: relationship.attrib["Target"]
        for relationship in relationships.findall("rel:Relationship", XML_NS)
    }
    targets = {}
    for sheet in workbook.findall("a:sheets/a:sheet", XML_NS):
        name = sheet.attrib["name"]
        relationship_id = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
        target = relationship_map[relationship_id]
        if not target.startswith("xl/"):
            target = "xl/" + target.lstrip("/")
        target = target.replace("xl//", "xl/").replace("xl/xl/", "xl/")
        if target not in archive.namelist():
            target = "xl/worksheets/" + Path(target).name
        targets[name] = target
    return targets


def sheet_target_for_year(targets: dict[str, str], year: int) -> str | None:
    if str(year) in targets:
        return targets[str(year)]
    for sheet_name, target in targets.items():
        if str(year) in sheet_name:
            return target
    return None


def raw_cell_value(cell: ET.Element, shared_strings: list[str]) -> Any:
    formula = cell.find("a:f", XML_NS)
    if formula is not None:
        return "=" + (formula.text or "")
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        inline = cell.find("a:is/a:t", XML_NS)
        return inline.text if inline is not None else ""
    value = cell.find("a:v", XML_NS)
    if value is None:
        return ""
    raw = value.text or ""
    if cell_type == "s":
        try:
            return shared_strings[int(raw)]
        except Exception:
            return raw
    try:
        number = float(raw)
    except ValueError:
        return raw
    return int(number) if number.is_integer() else number


def load_year_sheet_cells(path: Path, year: int) -> dict[str, Any] | None:
    with zipfile.ZipFile(path) as archive:
        targets = workbook_sheet_targets(archive)
        target = sheet_target_for_year(targets, year)
        if target is None:
            return None
        shared_strings = load_shared_strings(archive)
        root = ET.fromstring(archive.read(target))
        cells: dict[str, Any] = {}
        for cell in root.findall(".//a:sheetData/a:row/a:c", XML_NS):
            coordinate = cell.attrib.get("r")
            if not coordinate:
                continue
            cells[coordinate] = raw_cell_value(cell, shared_strings)
        return cells


def get_month_column(cells: dict[str, Any], year: int, month: int) -> int | None:
    for header_row in (1, 4):
        for column_number in range(2, 41):
            coordinate = f"{column_number_to_letters(column_number)}{header_row}"
            if parse_month_header(cells.get(coordinate)) == (year, month):
                return column_number
    return None


def find_row(cells: dict[str, Any], labels: tuple[str, ...]) -> int | None:
    wanted = {label.strip() for label in labels}
    max_row = max((split_coordinate(coordinate)[1] for coordinate in cells), default=0)
    for row_number in range(1, max_row + 1):
        label = str(cells.get(f"A{row_number}") or "").strip()
        if label in wanted:
            return row_number
    return None


def cf_workbook_schema_priority(path: Path) -> tuple[int, str]:
    labels = set()
    for year in (2026, 2025):
        try:
            cells = load_year_sheet_cells(path, year)
        except Exception:
            return (3, "unreadable")
        if not cells:
            continue
        max_row = max((split_coordinate(coordinate)[1] for coordinate in cells), default=0)
        for row_number in range(1, min(max_row, 80) + 1):
            label = str(cells.get(f"A{row_number}") or "").strip()
            if label:
                labels.add(label)
        break
    has_eco_cash = any(label in labels for label in ECO_CASH_LABELS)
    has_eco_gl = any(label in labels for label in ECO_GL_LABELS)
    has_lofty_distribution = "Sent to Lofty (Distributions)" in labels
    has_generic_owner_contribution = "Owner Contributions/Distributions" in labels
    if (has_eco_cash or has_eco_gl) and has_lofty_distribution:
        return (0, "dao_eco_template")
    if has_generic_owner_contribution:
        return (2, "generic_owner_contribution_template")
    if has_eco_cash or has_eco_gl:
        return (1, "eco_template")
    return (2, "unknown_template")


def find_cf_workbook(property_path: Path) -> Path | None:
    owner_dirs = [
        property_path / OWNER_STATEMENTS_DIR,
        property_path / "Public" / OWNER_STATEMENTS_DIR,
    ]
    candidates = []
    for owner_dir in owner_dirs:
        if not owner_dir.is_dir():
            continue
        for candidate_root in (owner_dir, owner_dir / "Statements"):
            if not candidate_root.is_dir():
                continue
            candidates.extend(
                candidate
                for candidate in candidate_root.glob("Cash Flow Statement*.xlsx")
                if not any(
                    marker in candidate.name.lower()
                    for marker in (
                        "conflict",
                        "conflicted copy",
                        ".before-",
                        ".backup",
                        " backup",
                        "-backup",
                    )
                )
            )
    if not candidates:
        return None
    schema_priorities = {candidate: cf_workbook_schema_priority(candidate) for candidate in candidates}
    return sorted(
        candidates,
        key=lambda candidate: (
            schema_priorities[candidate][0],
            cf_candidate_priority_for_property(candidate, property_path.name),
        ),
    )[0]


def probe_workbook_payload(path: Path, year: int, month: int) -> dict[str, Any]:
    cells = load_year_sheet_cells(path, year)
    if cells is None:
        return {"status": "year_sheet_missing"}
    column_number = get_month_column(cells, year, month)
    if column_number is None:
        return {"status": "month_column_missing"}
    values = {}
    for labels, source_name in (
        (LOFTY_OR_LABELS, "Lofty Operating Cash"),
        (ECO_CASH_LABELS, "ECO Operating Cash"),
        (ECO_GL_LABELS, "ECO General Ledger"),
        ((RETAINED_EARNINGS_LABEL,), RETAINED_EARNINGS_LABEL),
    ):
        row_number = find_row(cells, labels)
        if row_number is None:
            values[source_name] = {"status": "row_missing", "label": labels[0]}
            continue
        coordinate = f"{column_number_to_letters(column_number)}{row_number}"
        values[source_name] = {
            "status": "ok",
            "label": labels[0],
            "cell": coordinate,
            "value": cells.get(coordinate),
        }
    return {"status": "ok", "values": values}


def probe_property_workbook_payload(property_path: Path, year: int, month: int) -> dict[str, Any]:
    workbook_path = find_cf_workbook(property_path)
    if not workbook_path:
        return {"status": "cf_workbook_missing"}
    payload = probe_workbook_payload(workbook_path, year, month)
    payload["workbook"] = str(workbook_path)
    return payload


def probe_property_workbook(property_path: Path, year: int, month: int, timeout_seconds: float) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--probe-property-path",
        str(property_path),
        "--probe-year",
        str(year),
        "--probe-month",
        str(month),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "timeout_seconds": timeout_seconds}
    if completed.returncode != 0:
        return {
            "status": "probe_failed",
            "return_code": completed.returncode,
            "stderr": completed.stderr.strip()[:500],
        }
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {"status": "probe_unreadable", "error": str(exc), "stdout": completed.stdout[:500]}
    return payload


def probe_workbook(workbook_path: Path, year: int, month: int, timeout_seconds: float) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--probe-workbook",
        str(workbook_path),
        "--probe-year",
        str(year),
        "--probe-month",
        str(month),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "timeout_seconds": timeout_seconds, "workbook": str(workbook_path)}
    if completed.returncode != 0:
        return {
            "status": "probe_failed",
            "return_code": completed.returncode,
            "stderr": completed.stderr.strip()[:500],
            "workbook": str(workbook_path),
        }
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {"status": "probe_unreadable", "error": str(exc), "stdout": completed.stdout[:500], "workbook": str(workbook_path)}
    payload["workbook"] = str(workbook_path)
    return payload


def canonical_workbook_map_from_source_cash(path: Path) -> dict[str, Path]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    checked = payload.get("checked_workbooks_bounded") if isinstance(payload, dict) else None
    if not isinstance(checked, list):
        return {}
    mapping: dict[str, Path] = {}
    for item in checked:
        if not isinstance(item, dict):
            continue
        property_name = str(item.get("property") or "").strip()
        workbook = Path(str(item.get("file") or ""))
        if property_name and workbook.is_file():
            mapping.setdefault(normalize_property_name(property_name), workbook)
    return mapping


def load_candidate_records(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise ValueError(f"candidate packet has no records list: {path}")
    return [record for record in records if isinstance(record, dict)]


def csv_row_count(path: Path) -> int:
    try:
        with path.open(encoding="utf-8-sig", errors="ignore", newline="") as handle:
            return max(sum(1 for _ in handle) - 1, 0)
    except OSError:
        return 0


def package_component_paths(record: dict[str, Any]) -> list[Path]:
    """Select one complete canonical split GL per component of a package DAO."""
    property_name = normalize_property_name(record.get("property_name"))
    if "package" not in property_name:
        return []
    property_path = Path(str(record.get("property_path") or record.get("input_property_path") or ""))
    roots = [property_path / OWNER_STATEMENTS_DIR, property_path / "Public" / OWNER_STATEMENTS_DIR]
    candidates: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        candidates.extend(
            path
            for path in root.glob("ECO Systems General Ledger*.csv")
            if is_canonical_property_split_source(path)
            and not any(marker in path.name.lower() for marker in (".bak", "backup", "conflict"))
        )
    by_component: dict[str, Path] = {}
    for candidate in candidates:
        stem = candidate.stem
        prefix = "ECO Systems General Ledger - "
        component_text = stem[len(prefix):] if stem.startswith(prefix) else stem
        component_text = re.split(
            r"\s+-\s+.*\bproperty\s+package\b.*$",
            component_text,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()
        component = normalize_property_name(component_text)
        current = by_component.get(component)
        if current is None:
            by_component[component] = candidate
            continue
        candidate_is_exact = candidate.stem == f"{prefix}{component_text}"
        current_component_text = current.stem[len(prefix):] if current.stem.startswith(prefix) else current.stem
        current_component_text = re.split(
            r"\s+-\s+.*\bproperty\s+package\b.*$",
            current_component_text,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()
        current_is_exact = current.stem == f"{prefix}{current_component_text}"
        candidate_rank = (candidate_is_exact, csv_row_count(candidate), candidate.stat().st_mtime, len(candidate.name))
        current_rank = (current_is_exact, csv_row_count(current), current.stat().st_mtime, len(current.name))
        if candidate_rank > current_rank:
            by_component[component] = candidate
    return sorted(by_component.values(), key=lambda path: str(path).lower())


def package_component_key(path: Path) -> str:
    prefix = "ECO Systems General Ledger - "
    component_text = path.stem
    if component_text.startswith(prefix):
        component_text = component_text[len(prefix):]
    component_text = re.split(
        r"\s+-\s+.*\bproperty\s+package\b.*$",
        component_text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()
    return normalize_property_name(component_text)


def composite_scope_exclusion_reason(
    record: dict[str, Any],
    indexed_yhome_rows: dict[str, tuple[int, dict[str, Any]]],
) -> str | None:
    """Return a scope reason for a package with component rows but no group row.

    A package listing is not interchangeable with one component's Yhome row.
    Keeping it out of the update plan prevents a partial component value from
    being written into a group row that does not exist.
    """
    property_name = normalize_property_name(record.get("property_name"))
    if "package" not in property_name:
        return None
    component_paths = package_component_paths(record)
    component_keys = [package_component_key(path) for path in component_paths]
    if component_keys and all(key in indexed_yhome_rows for key in component_keys):
        return "composite_listing_components_have_yhome_rows_but_no_group_row"
    return None


def parse_source_row_date(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def read_canonical_gl_totals(
    path: Path,
    cutoff: datetime | None = None,
    *,
    cash_basis: bool = False,
) -> tuple[Decimal | None, int, Decimal | None, int, str | None]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            amount_header = next(
                (header for header in fieldnames if normalize_header(header) == "amount"),
                None,
            )
            if not amount_header:
                return None, 0, None, 0, "Amount column missing"
            date_header = next(
                (header for header in fieldnames if normalize_header(header) in {"date", "transactiondate", "posteddate"}),
                None,
            )
            notes_header = next(
                (header for header in fieldnames if normalize_header(header) == "notes"),
                None,
            )
            total = Decimal("0")
            row_count = 0
            cutoff_total = Decimal("0")
            cutoff_count = 0
            for row in reader:
                if cash_basis and "AOPS-PNL-ACCRUAL" in str(row.get(notes_header) or ""):
                    continue
                amount_text = str(row.get(amount_header) or "").strip()
                if not amount_text:
                    continue
                amount = parse_amount_decimal(amount_text)
                if amount is None:
                    return None, 0, None, 0, f"invalid Amount value: {amount_text}"
                total += amount
                row_count += 1
                if cutoff is None:
                    cutoff_total += amount
                    cutoff_count += 1
                elif date_header:
                    row_date = parse_source_row_date(row.get(date_header))
                    if row_date is None:
                        return None, 0, None, 0, f"invalid {date_header} value: {row.get(date_header)}"
                    if row_date <= cutoff:
                        cutoff_total += amount
                        cutoff_count += 1
    except (OSError, InvalidOperation, csv.Error) as exc:
        return None, 0, None, 0, f"{type(exc).__name__}: {exc}"
    if row_count == 0:
        return None, 0, None, 0, "no non-empty Amount values"
    if cutoff is not None and not date_header:
        return None, 0, None, 0, "Date column missing for month-end balance"
    return total.quantize(Decimal("0.01")), row_count, cutoff_total.quantize(Decimal("0.01")), cutoff_count, None


def read_canonical_gl_total(path: Path) -> tuple[Decimal | None, int, str | None]:
    total, row_count, _cutoff_total, _cutoff_count, error = read_canonical_gl_totals(path)
    return total, row_count, error


def validate_operating_cash_authority(
    source: dict[str, Any],
    *,
    requested_month: str | None,
    source_cash_mode: str,
) -> dict[str, Any]:
    output = dict(source)
    source_mode = str(output.get("eco_operating_cash_source_mode") or "")
    source_status = str(output.get("eco_operating_cash_status") or "")
    as_of_text = str(output.get("eco_operating_cash_as_of_date") or "")
    expected_as_of = None
    if source_cash_mode == "as_of_month_end" and re.fullmatch(r"\d{4}-\d{2}", str(requested_month or "")):
        year, month = (int(part) for part in str(requested_month).split("-"))
        next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
        expected_as_of = (next_month - timedelta(days=1)).isoformat()
    accepted_source_modes = {
        "verified_eco_cash_custody_reconciliation",
        "property_gl_cash_basis_net_open_obligations",
    }
    valid = (
        source_mode in accepted_source_modes
        and source_status == "ok"
        and bool(as_of_text)
        and (expected_as_of is None or as_of_text == expected_as_of)
    )
    if valid:
        output["eco_operating_cash_authority_status"] = "ok"
        return output
    output["eco_operating_cash"] = None
    output["eco_operating_cash_authority_status"] = (
        "historical_month_end_bank_snapshot_missing"
        if expected_as_of and as_of_text != expected_as_of
        else "eco_cash_custody_source_missing_or_invalid"
    )
    output["eco_operating_cash_required_as_of_date"] = expected_as_of
    return output


def candidate_source(
    record: dict[str, Any],
    requested_month: str | None = None,
    source_cash_mode: str = "full_column_e",
) -> dict[str, Any]:
    summary = record.get("monthly_financial_summary")
    source = dict(summary) if isinstance(summary, dict) else {}
    cutoff = None
    as_of_month = str(requested_month or source.get("as_of_month") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}", as_of_month):
        year, month = (int(part) for part in as_of_month.split("-"))
        if 1 <= month <= 12:
            next_month = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1)
            cutoff = next_month - timedelta(microseconds=1)
    if source.get("eco_general_ledger_sum") is None:
        source["eco_general_ledger_sum"] = source.get("eco_gl_column_e_sum")
    if source_cash_mode == "as_of_month_end" and source.get("eco_gl_column_e_sum_as_of_month") is not None:
        source["eco_gl_column_e_full_sum"] = source.get("eco_gl_column_e_sum")
        source["eco_gl_column_e_sum"] = source.get("eco_gl_column_e_sum_as_of_month")
        source["eco_general_ledger_sum"] = source["eco_gl_column_e_sum"]
    package_paths = package_component_paths(record)
    if package_paths:
        total = Decimal("0")
        row_count = 0
        cutoff_total = Decimal("0")
        cutoff_row_count = 0
        cash_total = Decimal("0")
        cash_row_count = 0
        cash_cutoff_total = Decimal("0")
        cash_cutoff_row_count = 0
        for package_path in package_paths:
            component_total, component_rows, component_cutoff_total, component_cutoff_rows, error = read_canonical_gl_totals(package_path, cutoff)
            if error:
                return invalidate_eco_source(source, "invalid_canonical_source", f"{package_path}: {error}")
            component_cash_total, component_cash_rows, component_cash_cutoff_total, component_cash_cutoff_rows, cash_error = read_canonical_gl_totals(
                package_path, cutoff, cash_basis=True
            )
            if cash_error:
                return invalidate_eco_source(source, "invalid_canonical_source", f"{package_path}: {cash_error}")
            total += component_total or Decimal("0")
            row_count += component_rows
            cutoff_total += component_cutoff_total or Decimal("0")
            cutoff_row_count += component_cutoff_rows
            cash_total += component_cash_total or Decimal("0")
            cash_row_count += component_cash_rows
            cash_cutoff_total += component_cash_cutoff_total or Decimal("0")
            cash_cutoff_row_count += component_cash_cutoff_rows
        full_amount = float(total.quantize(Decimal("0.01")))
        month_end_amount = float(cutoff_total.quantize(Decimal("0.01"))) if cutoff is not None else full_amount
        amount = month_end_amount if source_cash_mode == "as_of_month_end" else full_amount
        full_cash_amount = float(cash_total.quantize(Decimal("0.01")))
        month_end_cash_amount = float(cash_cutoff_total.quantize(Decimal("0.01"))) if cutoff is not None else full_cash_amount
        cash_amount = month_end_cash_amount if source_cash_mode == "as_of_month_end" else full_cash_amount
        source.update(
            {
                "eco_gl_column_e_source": str(package_paths[0]),
                "eco_gl_column_e_sources": [str(path) for path in package_paths],
                "eco_gl_column_e_source_mode": "canonical_aggregate_property_split_gl",
                "eco_gl_column_e_sum": amount,
                "eco_gl_column_e_full_sum": full_amount,
                "eco_general_ledger_sum": amount,
                "eco_cash_basis_amount": full_cash_amount,
                "eco_cash_basis_amount_as_of_month": month_end_cash_amount,
                "eco_cash_basis_row_count": cash_row_count,
                "eco_cash_basis_scope": "property_split_rows_excluding_manual_accrual_overlays",
                "eco_gl_column_e_row_count": row_count,
                "eco_gl_column_e_sum_as_of_month": month_end_amount,
                "eco_gl_column_e_row_count_as_of_month": cutoff_row_count if cutoff is not None else row_count,
                "eco_gl_column_e_status": "ok",
                "eco_gl_column_e_scope": "all_package_component_property_split_rows",
                "eco_gl_column_e_runtime_refreshed": True,
            }
        )
        return validate_operating_cash_authority(
            source,
            requested_month=as_of_month,
            source_cash_mode=source_cash_mode,
        )
    if "eco_gl_column_e_source" not in source:
        if "financial_candidate" in record or "financial_candidate_snapshot" in record:
            return invalidate_eco_source(
                source,
                "missing_canonical_source",
                "production candidate packet does not identify a canonical Dropbox Public/07 ECO GL file",
            )
        return validate_operating_cash_authority(
            source,
            requested_month=as_of_month,
            source_cash_mode=source_cash_mode,
        )
    ledger_path = Path(str(source.get("eco_gl_column_e_source") or ""))
    if not ledger_path.is_file():
        return invalidate_eco_source(source, "missing_canonical_source", "canonical ECO GL file is missing")
    if not is_canonical_property_split_source(ledger_path):
        return invalidate_eco_source(source, "noncanonical_source", "ECO GL source is outside Dropbox Public/07")
    if source.get("eco_gl_column_e_source_mode") == "source_ledger_zero_rows":
        return validate_operating_cash_authority(
            source,
            requested_month=as_of_month,
            source_cash_mode=source_cash_mode,
        )
    try:
        ledger_path = canonical_nonpackage_ledger_path(ledger_path)
    except DivergentCanonicalLedgerError as exc:
        return invalidate_eco_source(
            source,
            "ambiguous_canonical_source",
            f"multiple divergent property ledgers: {', '.join(path.name for path in exc.paths)}",
        )
    total, row_count, cutoff_total, cutoff_row_count, error = read_canonical_gl_totals(ledger_path, cutoff)
    if error:
        status = "unreadable_canonical_source" if error.startswith(("OSError", "Unicode", "csv")) else "invalid_canonical_source"
        return invalidate_eco_source(source, status, error)
    cash_total, cash_row_count, cash_cutoff_total, cash_cutoff_row_count, cash_error = read_canonical_gl_totals(
        ledger_path, cutoff, cash_basis=True
    )
    if cash_error:
        status = "unreadable_canonical_source" if cash_error.startswith(("OSError", "Unicode", "csv")) else "invalid_canonical_source"
        return invalidate_eco_source(source, status, cash_error)
    full_amount = float(total)
    month_end_amount = float(cutoff_total) if cutoff is not None else full_amount
    full_cash_amount = float(cash_total)
    month_end_cash_amount = float(cash_cutoff_total) if cutoff is not None else full_cash_amount
    source["eco_gl_column_e_full_sum"] = full_amount
    source["eco_gl_column_e_sum"] = month_end_amount if source_cash_mode == "as_of_month_end" else full_amount
    source["eco_gl_column_e_source"] = str(ledger_path)
    source["eco_gl_column_e_sources"] = [str(ledger_path)]
    source["eco_gl_column_e_source_mode"] = "canonical_property_split_gl"
    source["eco_general_ledger_sum"] = source["eco_gl_column_e_sum"]
    source["eco_cash_basis_amount"] = full_cash_amount
    source["eco_cash_basis_amount_as_of_month"] = month_end_cash_amount
    source["eco_cash_basis_row_count"] = cash_row_count
    source["eco_cash_basis_scope"] = "property_split_rows_excluding_manual_accrual_overlays"
    source["eco_gl_column_e_row_count"] = row_count
    source["eco_gl_column_e_sum_as_of_month"] = month_end_amount
    source["eco_gl_column_e_row_count_as_of_month"] = cutoff_row_count if cutoff is not None else row_count
    source["eco_gl_column_e_status"] = "ok"
    source["eco_gl_column_e_scope"] = "all_property_split_rows"
    source["eco_gl_column_e_runtime_refreshed"] = True
    return validate_operating_cash_authority(
        source,
        requested_month=as_of_month,
        source_cash_mode=source_cash_mode,
    )


def load_yhome_rows(path: Path) -> tuple[list[dict[str, Any]], dict[str, str]]:
    if not path.exists():
        return [], {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        header_map = {normalize_header(header): header for header in (reader.fieldnames or [])}
    return rows, header_map


def yhome_column(header_map: dict[str, str], wanted: str) -> str | None:
    return header_map.get(normalize_header(wanted))


def yhome_index(rows: list[dict[str, Any]], property_column: str | None) -> dict[str, tuple[int, dict[str, Any]]]:
    indexed: dict[str, tuple[int, dict[str, Any]]] = {}
    if not property_column:
        return indexed
    for index, row in enumerate(rows, start=2):
        key = normalize_property_name(row.get(property_column))
        if key and key not in indexed:
            try:
                source_row_number = int(str(row.get(YHOME_SHEET_ROW_COLUMN) or "").strip())
            except ValueError:
                source_row_number = index
            indexed[key] = (source_row_number, row)
    return indexed


def inactive_status(row: dict[str, Any] | None, new_pm_column: str | None) -> str | None:
    if not row or not new_pm_column:
        return None
    value = str(row.get(new_pm_column) or "").strip()
    lowered = value.lower()
    if any(marker in lowered for marker in INACTIVE_STATUS_MARKERS):
        return value or "inactive"
    return None


def compare_value(*, actual: Any, expected: float | None, label: str, property_name: str, file_path: Path, cell: str) -> dict[str, Any] | None:
    actual_numeric = parse_money(actual)
    if expected is None:
        return {
            "type": "source_missing",
            "property": property_name,
            "label": label,
            "file": str(file_path),
            "cell": cell,
            "actual": actual,
            "expected": None,
            "severity": "review",
        }
    if is_formula(actual):
        return {
            "type": "formula_not_source_value",
            "property": property_name,
            "label": label,
            "file": str(file_path),
            "cell": cell,
            "actual": actual,
            "expected": expected,
            "severity": "review",
        }
    if actual_numeric is None:
        return {
            "type": "non_numeric_balance_sheet_cell",
            "property": property_name,
            "label": label,
            "file": str(file_path),
            "cell": cell,
            "actual": actual,
            "expected": expected,
            "severity": "review",
        }
    difference = diff_amount(actual_numeric, expected)
    if difference is not None and abs(difference) > CONFLICT_THRESHOLD:
        return {
            "type": "balance_sheet_source_mismatch",
            "property": property_name,
            "label": label,
            "file": str(file_path),
            "cell": cell,
            "actual": actual_numeric,
            "expected": expected,
            "diff": difference,
            "severity": "review",
        }
    return None


STALE_LOFTY_RESERVE_SOURCE_MODES = frozenset(
    {
        "financials_md",
        "local_financials_md",
        "template",
        "local_template",
    }
)


def authoritative_lofty_reserve(source: dict[str, Any]) -> tuple[float | None, str]:
    source_mode = str(source.get("lofty_curr_maintenance_reserve_source_mode") or "").strip()
    if source_mode in STALE_LOFTY_RESERVE_SOURCE_MODES:
        return None, "missing_authoritative_live_source"
    return parse_money(source.get("lofty_curr_maintenance_reserve")), "ok"


def audit_workbook(
    record: dict[str, Any],
    year: int,
    month: int,
    workbook_timeout_seconds: float,
    workbook_override: Path | None = None,
    source_cash_mode: str = "full_column_e",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    property_name = str(record.get("property_name") or record.get("input_property_name") or "").strip()
    if property_name.endswith(" Public"):
        property_name = property_name.removesuffix(" Public").strip()
    input_property_path = Path(str(record.get("property_path") or record.get("input_property_path") or ""))
    property_path, _property_path_metadata = resolve_property_path(input_property_path)
    source = candidate_source(record, f"{year:04d}-{month:02d}", source_cash_mode)
    lofty_expected, lofty_authority_status = authoritative_lofty_reserve(source)
    report_month = f"{year:04d}-{month:02d}"
    retained_earnings_exemption = (normalize_property_name(property_name), report_month) in RETAINED_EARNINGS_LOFTY_EXEMPTIONS
    local_financials_only = canonical_reserve_property(property_name) in set(LOCAL_FINANCIALS_ONLY_PROPERTIES)
    lofty_pm_access_unavailable = bool(record.get("lofty_pm_access_unavailable_advisory"))
    if local_financials_only:
        lofty_expected = None
        lofty_authority_status = "not_required_local_financials_only"
    elif lofty_pm_access_unavailable and lofty_expected is None:
        lofty_authority_status = "not_verified_pm_access_unavailable"
    eco_expected = parse_money(source.get("eco_operating_cash"))
    eco_gl_expected = parse_money(source.get("eco_gl_column_e_sum"))
    if eco_gl_expected is None:
        eco_gl_expected = parse_money(source.get("eco_general_ledger_sum", source.get("eco_gl_column_e_sum")))
    summary: dict[str, Any] = {
        "property": property_name,
        "property_path": str(property_path),
        "workbook": None,
        "lofty_operating_cash_expected": lofty_expected,
        "lofty_operating_cash_source_mode": source.get("lofty_curr_maintenance_reserve_source_mode"),
        "lofty_operating_cash_authority_status": lofty_authority_status,
        "eco_operating_cash_expected": eco_expected,
        "eco_operating_cash_authority_status": source.get("eco_operating_cash_authority_status"),
        "eco_general_ledger_expected": eco_gl_expected,
        "eco_balance_semantics": (
            "canonical_property_general_ledger_net_position_as_of_requested_month_end"
            if source_cash_mode == "as_of_month_end"
            else "full_canonical_property_general_ledger_net_position_including_accruals"
        ),
        "eco_full_balance": parse_money(source.get("eco_gl_column_e_full_sum", source.get("eco_gl_column_e_sum"))),
        "eco_month_end_balance": parse_money(source.get("eco_gl_column_e_sum_as_of_month")),
        "issue_count": 0,
    }
    issues: list[dict[str, Any]] = []

    try:
        probe = (
            probe_workbook(workbook_override, year, month, workbook_timeout_seconds)
            if workbook_override is not None
            else probe_property_workbook(property_path, year, month, workbook_timeout_seconds)
        )
    except Exception as exc:  # noqa: BLE001
        issues.append(
            {
                "type": "cf_workbook_unreadable",
                "property": property_name,
                "property_path": str(property_path),
                "error": str(exc),
                "severity": "review",
            }
        )
        summary["issue_count"] = len(issues)
        return issues, summary

    workbook_path = Path(str(probe.get("workbook") or "")) if probe.get("workbook") else None
    summary["workbook"] = str(workbook_path) if workbook_path else None
    if probe.get("status") == "cf_workbook_missing":
        issues.append(
            {
                "type": "cf_workbook_missing",
                "property": property_name,
                "property_path": str(property_path),
                "severity": "review",
            }
        )
        summary["issue_count"] = len(issues)
        return issues, summary
    if probe.get("status") == "timeout":
        issues.append(
            {
                "type": "cf_workbook_probe_timeout",
                "property": property_name,
                "property_path": str(property_path),
                "file": str(workbook_path) if workbook_path else None,
                "timeout_seconds": probe.get("timeout_seconds"),
                "severity": "review",
            }
        )
        summary["issue_count"] = len(issues)
        return issues, summary
    if probe.get("status") == "probe_failed":
        issues.append(
            {
                "type": "cf_workbook_probe_failed",
                "property": property_name,
                "property_path": str(property_path),
                "file": str(workbook_path) if workbook_path else None,
                "return_code": probe.get("return_code"),
                "stderr": probe.get("stderr"),
                "severity": "review",
            }
        )
        summary["issue_count"] = len(issues)
        return issues, summary
    if probe.get("status") == "year_sheet_missing":
        issues.append({"type": "year_sheet_missing", "property": property_name, "file": str(workbook_path), "year": year, "severity": "review"})
        summary["issue_count"] = len(issues)
        return issues, summary
    if probe.get("status") == "month_column_missing":
        issues.append({"type": "month_column_missing", "property": property_name, "file": str(workbook_path), "month": f"{year}-{month:02d}", "severity": "review"})
        summary["issue_count"] = len(issues)
        return issues, summary
    if probe.get("status") != "ok":
        issues.append(
            {
                "type": "cf_workbook_probe_unreadable",
                "property": property_name,
                "file": str(workbook_path) if workbook_path else None,
                "probe_status": probe.get("status"),
                "severity": "review",
            }
        )
        summary["issue_count"] = len(issues)
        return issues, summary

    probe_values = probe.get("values") if isinstance(probe.get("values"), dict) else {}
    if retained_earnings_exemption:
        retained_probe = probe_values.get(RETAINED_EARNINGS_LABEL) if isinstance(probe_values.get(RETAINED_EARNINGS_LABEL), dict) else {}
        retained_value = retained_probe.get("value")
        retained_expected = -lofty_expected if lofty_expected is not None else None
        summary["lofty_operating_cash_retained_earnings_exemption"] = True
        summary["retained_earnings_actual"] = parse_money(retained_value)
        summary["retained_earnings_expected"] = retained_expected
        if retained_probe.get("status") == "row_missing":
            issues.append(
                {
                    "type": "retained_earnings_row_missing",
                    "property": property_name,
                    "file": str(workbook_path) if workbook_path else None,
                    "label": RETAINED_EARNINGS_LABEL,
                    "expected": retained_expected,
                    "severity": "review",
                }
            )
        elif retained_expected is not None:
            issue = compare_value(
                actual=retained_value,
                expected=retained_expected,
                label=RETAINED_EARNINGS_LABEL,
                property_name=property_name,
                file_path=workbook_path or property_path,
                cell=str(retained_probe.get("cell") or ""),
            )
            if issue:
                issue["source"] = "Undistributed Cash Flow Retained Earnings"
                issues.append(issue)
    row_specs = [
        ("Lofty Operating Cash", LOFTY_OR_LABELS[0], lofty_expected),
        ("ECO Operating Cash", ECO_CASH_LABEL, eco_expected),
        ("ECO General Ledger", ECO_GL_LABEL, eco_gl_expected),
    ]
    for source_name, fallback_label, expected in row_specs:
        if source_name == "Lofty Operating Cash" and (
            local_financials_only or retained_earnings_exemption or (lofty_pm_access_unavailable and expected is None)
        ):
            continue
        if source_name == "ECO Operating Cash" and expected is None:
            summary["eco_operating_cash_verification_status"] = "not_verified_no_mapped_bank_authority"
            continue
        source_probe = probe_values.get(source_name) if isinstance(probe_values.get(source_name), dict) else {}
        if source_probe.get("status") == "row_missing":
            issues.append(
                {
                    "type": "balance_sheet_row_missing",
                    "property": property_name,
                    "file": str(workbook_path) if workbook_path else None,
                    "label": source_probe.get("label") or fallback_label,
                    "source": source_name,
                    "expected": expected,
                    "severity": "review",
                }
            )
            continue
        coordinate = source_probe.get("cell")
        value = source_probe.get("value")
        issue = compare_value(
            actual=value,
            expected=expected,
            label=source_probe.get("label") or fallback_label,
            property_name=property_name,
            file_path=workbook_path or property_path,
            cell=str(coordinate or ""),
        )
        summary[f"{normalize_header(source_name).replace(' ', '_')}_actual"] = parse_money(value)
        summary[f"{normalize_header(source_name).replace(' ', '_')}_cell"] = coordinate
        if issue:
            issue["source"] = source_name
            issues.append(issue)

    summary["issue_count"] = len(issues)
    return issues, summary


def build_yhome_plan(
    records: list[dict[str, Any]],
    yhome_rows: list[dict[str, Any]],
    header_map: dict[str, str],
    *,
    requested_month: str | None = None,
    source_cash_mode: str = "full_column_e",
    require_all_yhome_rows: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], list[dict[str, str]]]:
    property_column = yhome_column(header_map, YHOME_PROPERTY_COLUMN)
    new_pm_column = yhome_column(header_map, YHOME_NEW_PM_COLUMN)
    lofty_column = yhome_column(header_map, YHOME_LOFTY_CASH_COLUMN)
    eco_column = yhome_column(header_map, YHOME_ECO_CASH_COLUMN)
    indexed = yhome_index(yhome_rows, property_column)
    plan: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    missing_candidates: list[str] = []
    excluded_candidates: list[dict[str, str]] = []

    missing_columns = [
        name
        for name, column in (
            (YHOME_PROPERTY_COLUMN, property_column),
            (YHOME_NEW_PM_COLUMN, new_pm_column),
            (YHOME_LOFTY_CASH_COLUMN, lofty_column),
            (YHOME_ECO_CASH_COLUMN, eco_column),
        )
        if not column
    ]
    if missing_columns:
        issues.append({"type": "yhome_required_columns_missing", "columns": missing_columns, "severity": "review"})
        return plan, issues, missing_candidates, excluded_candidates

    for record in records:
        property_name = str(record.get("property_name") or record.get("input_property_name") or "").strip()
        property_path = Path(str(record.get("property_path") or record.get("input_property_path") or ""))
        property_state = property_state_from_path(property_path)
        yhome_excluded_reason = None
        if property_state and property_state not in YHOME_REQUIRED_STATES:
            yhome_excluded_reason = f"state_excluded_from_yhome:{property_state}"
        if not yhome_excluded_reason:
            yhome_excluded_reason = composite_scope_exclusion_reason(record, indexed)
        source = candidate_source(record, requested_month, source_cash_mode)
        key = normalize_property_name(property_name)
        match = indexed.get(key)
        if not match:
            if yhome_excluded_reason:
                excluded_candidates.append(
                    {
                        "property": property_name,
                        "state": property_state or "",
                        "reason": yhome_excluded_reason,
                    }
                )
                continue
            missing_candidates.append(property_name)
            if require_all_yhome_rows:
                issues.append({"type": "yhome_property_row_missing", "property": property_name, "severity": "review"})
            continue

        row_number, row = match
        status = inactive_status(row, new_pm_column)
        for target_name, column_name, expected_raw in (
            (YHOME_LOFTY_CASH_COLUMN, lofty_column, source.get("lofty_curr_maintenance_reserve")),
            # ECO Net DAO Funds is spendable cash in ECO custody after accrued
            # obligations and other restrictions. The full GL remains a
            # separate accounting control and must never be presented as cash.
            (YHOME_ECO_CASH_COLUMN, eco_column, source.get("eco_operating_cash")),
        ):
            expected = parse_money(expected_raw)
            actual = parse_money(row.get(column_name))
            difference = diff_amount(actual, expected)
            action = "skip_inactive" if status else "no_change"
            if status:
                action = "skip_inactive"
            elif expected is None:
                action = "source_missing"
            elif actual is None or difference is None or abs(difference) > CONFLICT_THRESHOLD:
                action = "update"
            entry = {
                "property": property_name,
                "yhome_row_number": row_number,
                "column": target_name,
                "eco_cash_policy": (
                    "eco_held_unrestricted_cash_v1"
                    if target_name == YHOME_ECO_CASH_COLUMN
                    else "lofty_curr_maintenance_reserve_v1"
                ),
                "matched_header": column_name,
                "new_pm": row.get(new_pm_column),
                "current_value": actual,
                "target_value": expected,
                "target_value_formatted": money(expected),
                "diff": difference,
                "action": action,
            }
            if row.get(YHOME_SHEET_TITLE_COLUMN):
                entry["yhome_sheet_title"] = row.get(YHOME_SHEET_TITLE_COLUMN)
            if row.get(YHOME_SHEET_GID_COLUMN):
                entry["yhome_sheet_gid"] = row.get(YHOME_SHEET_GID_COLUMN)
            if row.get("__yhome_sheet_lofty_operating_cash_column_index"):
                entry["yhome_lofty_operating_cash_column_index"] = row.get(
                    "__yhome_sheet_lofty_operating_cash_column_index"
                )
            if row.get("__yhome_sheet_eco_net_dao_funds_column_index"):
                entry["yhome_eco_net_dao_funds_column_index"] = row.get(
                    "__yhome_sheet_eco_net_dao_funds_column_index"
                )
            if status:
                entry["skip_reason"] = f"Yhome {YHOME_NEW_PM_COLUMN} marks inactive: {status}"
            plan.append(entry)
            if action in {"source_missing", "update"}:
                issues.append(
                    {
                        "type": "yhome_target_column_update_required" if action == "update" else "yhome_source_missing",
                        "property": property_name,
                        "column": target_name,
                        "row": row_number,
                        "actual": actual,
                        "expected": expected,
                        "diff": difference,
                        "severity": "review",
                    }
                )
    return plan, issues, missing_candidates, excluded_candidates


def write_plan_csv(path: Path, plan: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "property",
        "yhome_row_number",
        "yhome_sheet_gid",
        "yhome_sheet_title",
        "yhome_lofty_operating_cash_column_index",
        "yhome_eco_net_dao_funds_column_index",
        "column",
        "eco_cash_policy",
        "matched_header",
        "new_pm",
        "current_value",
        "target_value",
        "target_value_formatted",
        "diff",
        "action",
        "skip_reason",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for entry in plan:
            writer.writerow({field: entry.get(field) for field in fieldnames})


def write_missing_candidates_csv(path: Path, missing_candidates: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["property", "reason"])
        writer.writeheader()
        for property_name in missing_candidates:
            writer.writerow(
                {
                    "property": property_name,
                    "reason": "candidate property not present in Yhome transition reconciliation",
                }
            )


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    year, month = parse_month(args.month)
    source_cash_mode = str(getattr(args, "source_cash_mode", "") or source_cash_mode_for_month(year, month)).strip()
    if source_cash_mode not in {"full_column_e", "as_of_month_end"}:
        raise ValueError(f"unsupported source cash mode: {source_cash_mode}")
    all_records = load_candidate_records(args.candidate_packet)
    exclusion_guards, _yhome_guard, _manual_exclusions = (
        monthly_exclusion_guards(args.yhome_csv) if monthly_exclusion_guards else ([], {}, [])
    )
    records = [
        record
        for record in all_records
        if not is_manually_excluded_property(
            record.get("property_name") or record.get("input_property_name") or ""
        )
        and not financial_audit_exclusion_guard(record, exclusion_guards)
    ]
    canonical_source_issues: list[dict[str, Any]] = []
    for record in records:
        source = candidate_source(record, f"{year:04d}-{month:02d}", source_cash_mode)
        source_status = str(source.get("eco_gl_column_e_status") or "")
        if source_status in {
            "missing_canonical_source",
            "noncanonical_source",
            "invalid_canonical_source",
            "unreadable_canonical_source",
            "empty_canonical_source",
        }:
            canonical_source_issues.append(
                {
                    "type": "canonical_eco_gl_source_invalid",
                    "property": str(record.get("property_name") or record.get("input_property_name") or "").strip(),
                    "source_status": source_status,
                    "source": source.get("eco_gl_column_e_source"),
                    "error": source.get("eco_gl_column_e_source_error"),
                    "severity": "review",
                }
            )
    yhome_rows, header_map = load_yhome_rows(args.yhome_csv)
    workbook_issues: list[dict[str, Any]] = []
    workbook_summaries: list[dict[str, Any]] = []
    workbook_timeout_seconds = float(getattr(args, "workbook_timeout_seconds", 3.0) or 3.0)
    audit_workbooks = bool(getattr(args, "audit_workbooks", False))
    source_cash_report = Path(getattr(args, "source_cash_report", DEFAULT_SOURCE_CASH_REPORT))
    canonical_workbooks = canonical_workbook_map_from_source_cash(source_cash_report)
    if audit_workbooks:
        for record in records:
            property_name = str(record.get("property_name") or record.get("input_property_name") or "").strip()
            issues, summary = audit_workbook(
                record,
                year,
                month,
                workbook_timeout_seconds,
                canonical_workbooks.get(normalize_property_name(property_name)),
                source_cash_mode,
            )
            workbook_issues.extend(issues)
            workbook_summaries.append(summary)
    else:
        for record in records:
            source = candidate_source(record, f"{year:04d}-{month:02d}", source_cash_mode)
            workbook_summaries.append(
                {
                    "property": str(record.get("property_name") or record.get("input_property_name") or "").strip(),
                    "property_path": str(record.get("property_path") or record.get("input_property_path") or ""),
                    "workbook_audit_status": "skipped_live_workbook_io_disabled",
                    "lofty_operating_cash_expected": parse_money(source.get("lofty_curr_maintenance_reserve")),
                    "eco_operating_cash_expected": parse_money(source.get("eco_operating_cash")),
                    "eco_general_ledger_expected": parse_money(source.get("eco_general_ledger_sum")),
                    "eco_balance_semantics": (
                        "canonical_property_general_ledger_net_position_as_of_requested_month_end"
                        if source_cash_mode == "as_of_month_end"
                        else "full_canonical_property_general_ledger_net_position_including_accruals"
                    ),
                    "eco_full_balance": parse_money(source.get("eco_gl_column_e_full_sum", source.get("eco_gl_column_e_sum"))),
                    "eco_month_end_balance": parse_money(source.get("eco_gl_column_e_sum_as_of_month")),
                    "issue_count": 0,
                }
            )
    yhome_plan, yhome_issues, yhome_missing_candidates, yhome_excluded_candidates = build_yhome_plan(
        records,
        yhome_rows,
        header_map,
        requested_month=f"{year:04d}-{month:02d}",
        # Yhome is a current accounting view even when workbook auditing targets
        # a closed reporting month.
        source_cash_mode="full_column_e",
        require_all_yhome_rows=bool(getattr(args, "require_all_yhome_rows", False)),
    )
    if args.yhome_plan_csv:
        write_plan_csv(args.yhome_plan_csv, yhome_plan)
    if getattr(args, "yhome_missing_candidates_csv", None):
        write_missing_candidates_csv(args.yhome_missing_candidates_csv, yhome_missing_candidates)

    authoritative_issues = canonical_source_issues + workbook_issues
    all_issues = authoritative_issues + yhome_issues
    issue_counts = Counter(issue.get("type") or "unknown" for issue in authoritative_issues)
    yhome_issue_counts = Counter(issue.get("type") or "unknown" for issue in yhome_issues)
    yhome_action_counts = Counter(entry.get("action") or "unknown" for entry in yhome_plan)
    return {
        "job": "baselane-cf-balance-sheet-consistency-audit",
        "generated_at": generated_at(),
        "run_month": args.month,
        "source_cash_balance_mode": source_cash_mode,
        "status": "review" if authoritative_issues else "ok",
        "issue_count": len(authoritative_issues),
        "issue_type_counts": dict(sorted(issue_counts.items())),
        "yhome_work_product_status": "review" if yhome_issues or yhome_action_counts.get("update", 0) else "ok",
        "yhome_issue_count": len(yhome_issues),
        "yhome_issue_type_counts": dict(sorted(yhome_issue_counts.items())),
        "candidate_packet": str(args.candidate_packet),
        "candidate_record_count": len(all_records),
        "audited_candidate_record_count": len(records),
        "manual_excluded_property_names": list(DEFAULT_MANUAL_EXCLUDED_PROPERTIES),
        "manual_excluded_candidate_count": sum(
            1
            for record in all_records
            if is_manually_excluded_property(record.get("property_name") or record.get("input_property_name") or "")
        ),
        "policy_excluded_candidate_count": sum(
            1 for record in all_records if financial_audit_exclusion_guard(record, exclusion_guards)
        ),
        "live_only_excluded_candidate_count": sum(
            1
            for record in all_records
            if exclusion_guard_for_record(record, exclusion_guards)
            and not financial_audit_exclusion_guard(record, exclusion_guards)
        ),
        "workbook_audit_enabled": audit_workbooks,
        "workbook_audit_status": "enabled" if audit_workbooks else "skipped_live_workbook_io_disabled",
        "source_cash_report": str(source_cash_report),
        "canonical_source_cash_workbook_count": len(canonical_workbooks),
        "yhome_csv": str(args.yhome_csv),
        "yhome_required_states": sorted(YHOME_REQUIRED_STATES),
        "yhome_scope_policy": (
            "Require exact Yhome rows for OH/IL candidates. Exclude other states from this sheet, "
            "and exclude package listings when every component has a Yhome row but no package row exists; "
            "these exclusions remain visible in yhome_excluded_candidates."
        ),
        "yhome_target_columns": [YHOME_LOFTY_CASH_COLUMN, YHOME_ECO_CASH_COLUMN],
        "yhome_update_plan_csv": str(args.yhome_plan_csv) if args.yhome_plan_csv else None,
        "yhome_missing_candidates_csv": (
            str(args.yhome_missing_candidates_csv) if getattr(args, "yhome_missing_candidates_csv", None) else None
        ),
        "yhome_update_plan_count": len(yhome_plan),
        "yhome_update_required_count": yhome_action_counts.get("update", 0),
        "yhome_skip_inactive_count": yhome_action_counts.get("skip_inactive", 0),
        "yhome_skip_excluded_count": len(yhome_excluded_candidates),
        "yhome_excluded_candidate_count": len(yhome_excluded_candidates),
        "yhome_excluded_candidates": yhome_excluded_candidates[:100],
        "yhome_missing_candidate_count": len(yhome_missing_candidates),
        "yhome_missing_candidates": yhome_missing_candidates[:100],
        "yhome_unmatched_candidate_count": len(yhome_missing_candidates),
        "yhome_unmatched_candidates": yhome_missing_candidates[:100],
        "yhome_action_counts": dict(sorted(yhome_action_counts.items())),
        "cf_balance_sheet_policy": (
            "Lofty Operating Cash comes from curr_maintenance_reserve. ECO Operating Cash comes from a dated live "
            "Baselane DAO bank snapshot, while ECO General Ledger comes from canonical property-split Column E. "
            "Closed-month workbooks require an exact month-end bank snapshot. Column E is an internal accounting "
            "control and must not be relabeled as custody or spendable cash."
        ),
        "yhome_weekly_policy": (
            "Weekly Yhome Transition Reconciliation cash updates use transaction-backed ECO custody less recorded "
            "unpaid obligations. Verified DAO A/P to ECO is reported separately with reciprocal ECO A/R; Column E "
            "is not a cash source. Rows marked sold/selling/closed/delisted in New PM are skipped. This sheet is a "
            "non-authoritative work product: missing rows, pending updates, and write failures do not block CF, "
            "Lofty, or investor outputs."
        ),
        "summaries": workbook_summaries,
        "issues": authoritative_issues[:200],
        "yhome_issues": yhome_issues[:200],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", default=datetime.now(timezone.utc).strftime("%Y-%m"))
    parser.add_argument("--candidate-packet", type=Path, default=Path("reports/baselane_financials_monthly_review_candidate_packet.json"))
    parser.add_argument("--yhome-csv", type=Path, default=Path("reports/yhome_transition_reconciliation.csv"))
    parser.add_argument("--report", type=Path, default=Path("reports/baselane_cf_balance_sheet_consistency_audit.json"))
    parser.add_argument("--yhome-plan-csv", type=Path, default=Path("reports/yhome_operating_cash_update_plan.csv"))
    parser.add_argument("--yhome-missing-candidates-csv", type=Path, default=Path("reports/yhome_missing_candidates.csv"))
    parser.add_argument("--source-cash-report", type=Path, default=DEFAULT_SOURCE_CASH_REPORT)
    parser.add_argument("--source-cash-mode", choices=("full_column_e", "as_of_month_end"), default=None)
    parser.add_argument("--audit-workbooks", action="store_true", help="Opt-in workbook cell probes; scheduled mode leaves this off to avoid live Dropbox/Windows I/O hangs")
    parser.add_argument("--workbook-timeout-seconds", type=float, default=3.0)
    parser.add_argument(
        "--require-all-yhome-rows",
        action="store_true",
        help="Report candidate properties missing from the non-authoritative Yhome work product as sheet-review issues",
    )
    parser.add_argument("--probe-workbook", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--probe-property-path", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--probe-year", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--probe-month", type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.probe_workbook:
        if not args.probe_year or not args.probe_month:
            raise SystemExit("--probe-year and --probe-month are required with --probe-workbook")
        print(json.dumps(probe_workbook_payload(args.probe_workbook, args.probe_year, args.probe_month)))
        return 0
    if args.probe_property_path:
        if not args.probe_year or not args.probe_month:
            raise SystemExit("--probe-year and --probe-month are required with --probe-property-path")
        print(json.dumps(probe_property_workbook_payload(args.probe_property_path, args.probe_year, args.probe_month)))
        return 0

    report = build_report(args)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.report} status={report['status']} issues={report['issue_count']}")
    return 1 if report["status"] == "review" else 0


if __name__ == "__main__":
    raise SystemExit(main())
