import argparse
import csv
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from datetime import datetime
from difflib import SequenceMatcher
from collections import defaultdict
from typing import Any, Dict, List, Tuple

try:
    from canonical_property_ledger import (
        canonical_output_ledger_path,
        equivalent_output_ledger_paths,
        ledger_property_identity,
    )
except ImportError:
    from scripts.canonical_property_ledger import (
        canonical_output_ledger_path,
        equivalent_output_ledger_paths,
        ledger_property_identity,
    )

try:
    from coownership_mortgage_policy import P_AND_I_DAO_PROPERTIES, is_p_and_i_dao_property
except ImportError:
    from scripts.coownership_mortgage_policy import P_AND_I_DAO_PROPERTIES, is_p_and_i_dao_property

try:
    from coownership_reserve_policy import manual_accrual_kind
except ImportError:
    from scripts.coownership_reserve_policy import manual_accrual_kind

SCRIPT_WORKSPACE_ROOT = Path(__file__).absolute().parents[1]
WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT") or SCRIPT_WORKSPACE_ROOT)
DEFAULT_REPORT_PATH = Path(
    os.environ.get("BASELANE_SPLIT_LEDGER_REPORT")
    or WORKSPACE_ROOT / "reports" / "split_ledger_public_financials_last.json"
)
DEFAULT_EXCLUSION_REPORT_PATH = WORKSPACE_ROOT / "reports" / "baselane_monthly_owner_review_gate.json"
ESCROW_NATIVE_SPLIT_SCHEDULE_MONTHS = 12


def default_amortization_terms_path() -> Path:
    explicit = os.environ.get("BASELANE_MORTGAGE_AMORTIZATION_TERMS")
    if explicit:
        return Path(explicit)
    inherited = WORKSPACE_ROOT / "config" / "coownership_mortgage_amortization_terms.json"
    canonical = SCRIPT_WORKSPACE_ROOT / "config" / "coownership_mortgage_amortization_terms.json"
    cwd_canonical = Path.cwd() / "config" / "coownership_mortgage_amortization_terms.json"
    home_canonical = Path.home() / ".openclaw" / "workspace" / "config" / "coownership_mortgage_amortization_terms.json"
    if inherited.is_file() or not canonical.is_file():
        for candidate in (cwd_canonical, home_canonical):
            if candidate.is_file():
                return candidate
        return inherited
    return canonical


DEFAULT_AMORTIZATION_TERMS_PATH = default_amortization_terms_path()
DEFAULT_MANUAL_SPLIT_EXCLUDES = ("3560 Saint Albans Rd", "1935 S Glen Rd")
DEFAULT_LISTING_UPDATE_POLICY_PATH = WORKSPACE_ROOT / "config" / "lofty_listing_update_policy.json"
EXCLUDED_CHECKLIST_STATUSES = {"skipped_closed", "excluded_external"}
CITADEL_TEXT_RE = re.compile(r"\b(CITADEL|ACRA|LOANSPHERE|LOANDEPOT|FREEDOM|NEWREZ|SHELLPOIN(?:T)?|MORTGAGE\s+SERV)\b", re.I)
CONFLICT_THRESHOLD = 0.005
RATE_ZERO_THRESHOLD = 1e-12
DAO_ECO_REVENUE_PREFIX = "ECO Systems LLC DAO Registration Fee Revenue | "
PM_ECO_REVENUE_PREFIX = "ECO Systems LLC PM Fee Revenue | "


def first_existing_dir(candidates, fallback):
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate
    return fallback


DROPBOX_ROOT = Path(os.environ["DROPBOX_ROOT"]) if os.environ.get("DROPBOX_ROOT") else first_existing_dir(
    [
        Path("/mnt/c/Users/digit/Dropbox"),
        Path("/data/Dropbox"),
        Path.home() / "Dropbox",
        Path("/home/digit/Dropbox"),
    ],
    Path("/mnt/c/Users/digit/Dropbox"),
)
LEDGER_DIR = Path(os.environ["BASELANE_LEDGER_DIR"]) if os.environ.get("BASELANE_LEDGER_DIR") else first_existing_dir(
    [
        DROPBOX_ROOT / "Projects/assetrail",
        DROPBOX_ROOT / "Projects/transaction_tracker",
    ],
    DROPBOX_ROOT / "Projects/assetrail",
)
SOURCE = str(LEDGER_DIR / "ECO Systems General Ledger.csv")
REAL_ESTATE_BASE = str(DROPBOX_ROOT / "Real Estate")
TODAY = datetime.now().strftime("%Y-%m-%d")

# Manual overrides for known mismatches between ledger names and folder names.
OVERRIDES = {
    "122 Florida Park Dr": "FL/122 Florida Park Dr, Palm Coast, FL 32137",
    "326 South Alcott Street": "CO/326-332 S Alcott St Public",
    "804 S Quitman St": "CO/804 S Quitman St, Denver, CO 80219",
    "724 3rd Ave": "NY/724 3rd Ave, Watervliet, NY 12189",
    "85-104 Alawa Pl": "HI/85-104 Alawa Pl Public",
    "9 Country Club Ln N": "NY/9 Country Club Lane N Public",
    "1039 Mount Vernon Rd": "OH/APG/1039 Mount Vernon Rd",
    "10917 Fidelity Ave": "OH/APG/10917 Fidelity Ave",
    "11400 Linnet Ave": "OH/APG/11400 Linnet Ave",
    "1258 Lily St": "OH/Ohio 3-Property Package",
    "1321 Allendale Ave": "OH/Ohio 3-Property Package",
    "1321 Allendale Ave.": "OH/Ohio 3-Property Package",
    "1321 Allendale Avenue": "OH/Ohio 3-Property Package",
    "1518 Dille Rd": "OH/Ohio 3-Property Package",
    "1518 Dille Rd.": "OH/Ohio 3-Property Package",
    "1518 Dille Road": "OH/Ohio 3-Property Package",
    "16713 Lotus Drive": "OH/APG/16713 Lotus Drive",
    "4318 Clybourne Ave": "OH/4318 Clybourne Ave, Cleveland, OH 44109",
    "4183 E 146 St": "OH/LFTY0148 4183 E 146th St. Cleveland, OH 44128",
}

ABBREV = {
    "street": "st",
    "st.": "st",
    "avenue": "ave",
    "ave.": "ave",
    "road": "rd",
    "rd.": "rd",
    "drive": "dr",
    "dr.": "dr",
    "boulevard": "blvd",
    "place": "pl",
    "lane": "ln",
    "court": "ct",
    "circle": "cir",
    "south": "s",
    "north": "n",
    "east": "e",
    "west": "w",
}


def normalize(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"\b(\d+)(st|nd|rd|th)\b", r"\1", s)
    s = re.sub(r"[,|()]+", " ", s)
    s = s.replace("-", " ")
    s = re.sub(r"\s+", " ", s)
    toks = []
    for t in s.split():
        toks.append(ABBREV.get(t, t))
    return " ".join(toks)


def split_exclusion_guards(report_path: Path | None = None) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    report_path = Path(report_path or DEFAULT_EXCLUSION_REPORT_PATH)
    guards: List[Dict[str, str]] = []
    for name in DEFAULT_MANUAL_SPLIT_EXCLUDES:
        guards.append(
            {
                "source": "manual_split_exclusion",
                "property_name": name,
                "normalized_property": normalize(name),
                "status": "manual_excluded",
                "reason": "manual do-not-update property split exclusion",
            }
        )

    listing_policy_count = 0
    listing_policy_operational_count = 0
    listing_policy_status = "missing"
    listing_policy_path = DEFAULT_LISTING_UPDATE_POLICY_PATH
    if listing_policy_path.is_file():
        try:
            policy = json.loads(listing_policy_path.read_text(encoding="utf-8"))
            listing_policy_status = "ok"
        except Exception as exc:
            policy = {}
            listing_policy_status = f"unreadable:{exc}"
        policy_sources = [
            ("sold_ignore_listing_updates", "skipped_sold", "sold/offboarded property split exclusion"),
            ("operational_ignore_listing_updates", "operational_excluded", "operational/non-property split exclusion"),
        ]
        for policy_key, status, default_reason in policy_sources:
            values = policy.get(policy_key) if isinstance(policy, dict) else None
            if isinstance(values, list):
                for value in values:
                    raw_value = value if isinstance(value, dict) else {}
                    if policy_key == "operational_ignore_listing_updates" and raw_value.get("split_exclude") is not True:
                        continue
                    full_address = str(raw_value.get("address") or raw_value.get("property_name") or value or "").strip()
                    property_name = full_address.split(",", 1)[0].strip()
                    if not property_name:
                        continue
                    guards.append(
                        {
                            "source": f"listing_update_policy:{policy_key}",
                            "property_name": property_name,
                            "full_address": full_address,
                            "normalized_property": normalize(property_name),
                            "status": status,
                            "reason": str(raw_value.get("reason") or default_reason),
                        }
                    )
                    if policy_key == "sold_ignore_listing_updates":
                        listing_policy_count += 1
                    elif policy_key == "operational_ignore_listing_updates":
                        listing_policy_operational_count += 1

    report_meta: Dict[str, Any] = {
        "status": "not_configured",
        "path": str(report_path),
        "loaded_count": 0,
        "manual_count": len(DEFAULT_MANUAL_SPLIT_EXCLUDES),
        "listing_policy_path": str(listing_policy_path),
        "listing_policy_status": listing_policy_status,
        "listing_policy_sold_count": listing_policy_count,
        "listing_policy_operational_count": listing_policy_operational_count,
    }
    if not report_path.is_file():
        report_meta["status"] = "missing"
        return guards, report_meta

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        report_meta.update({"status": "unreadable", "error": str(exc)})
        return guards, report_meta

    loaded_count = 0
    for record in report.get("property_checklist") or []:
        if not isinstance(record, dict):
            continue
        status = str(record.get("status") or "").strip()
        external_exclusion = record.get("external_exclusion") is True
        if status not in EXCLUDED_CHECKLIST_STATUSES and not external_exclusion:
            continue
        property_name = str(record.get("property_name") or "").strip()
        property_path = str(record.get("property_path") or "").strip()
        if not property_name and not property_path:
            continue
        guards.append(
            {
                "source": "monthly_owner_review_gate",
                "property_name": property_name or Path(property_path).name,
                "property_path": property_path,
                "normalized_property": normalize(property_name or Path(property_path).name),
                "normalized_path_name": normalize(Path(property_path).name) if property_path else "",
                "status": status or "external_exclusion",
                "reason": "monthly review gate excludes sold/delisted/closed/manual property from live updates",
            }
        )
        loaded_count += 1

    report_meta.update(
        {
            "status": "ok",
            "loaded_count": loaded_count,
            "source_status": report.get("status"),
            "source_generated_at": report.get("generated_at"),
            "source_property_excluded_total_count": report.get("property_excluded_total_count"),
            "source_property_external_excluded_count": report.get("property_external_excluded_count"),
            "source_property_skipped_count": report.get("property_skipped_count"),
        }
    )
    return guards, report_meta


def split_exclusion_match(prop: str, root_path: str, rel: str, guards: List[Dict[str, str]]) -> Dict[str, str] | None:
    candidates = [normalize(prop), normalize(Path(root_path).name), normalize(rel)]
    for guard in guards:
        keys = [
            str(guard.get("normalized_property") or "").strip(),
            str(guard.get("normalized_path_name") or "").strip(),
        ]
        for key in keys:
            if not key:
                continue
            for candidate in candidates:
                if not candidate:
                    continue
                if key == candidate or key in candidate or candidate in key:
                    return guard
    return None


def tokens(s: str):
    return set(normalize(s).split())


def first_number(s: str):
    m = re.search(r"\d+", s)
    return m.group(0) if m else None


def acquisition_purchase_agreement_matches(prop: str, real_estate_base: Path) -> List[str]:
    purchase_dir = real_estate_base / "IL" / "Albin" / "Purchase Agreements"
    if not purchase_dir.is_dir():
        return []
    prop_tokens = tokens(prop)
    if not prop_tokens:
        return []
    matches = []
    for path in sorted(purchase_dir.glob("**/*")):
        if not path.is_file():
            continue
        name_tokens = tokens(path.stem)
        if prop_tokens <= name_tokens:
            matches.append(str(path))
    return matches


def is_acquisition_down_payment(rows: List[Dict[str, str]]) -> bool:
    if not rows:
        return False
    has_acquisition_activity = False
    for row in rows:
        row_type = (row.get("Type") or "").strip()
        category = (row.get("Category") or "").strip()
        merchant_description = " ".join(
            str(row.get(key) or "") for key in ("Merchant", "Description", "Notes")
        )
        is_down_payment = row_type == "Property Transactions" and category == "Down Payments"
        is_registration_cost = (
            row_type == "Operating Expenses"
            and category == "Tax Licenses & Registrations"
            and re.search(r"\bWY SECRETARY OF STA\b", merchant_description, re.I)
        )
        if not (is_down_payment or is_registration_cost):
            return False
        has_acquisition_activity = True
    return has_acquisition_activity and amount_total(rows) < 0


def deferred_acquisition_record(prop: str, rows: List[Dict[str, str]], real_estate_base: Path) -> Dict[str, Any] | None:
    evidence = acquisition_purchase_agreement_matches(prop, real_estate_base)
    if not evidence or not is_acquisition_down_payment(rows):
        return None
    return {
        "property": prop,
        "row_count": len(rows),
        "amount_total": amount_total(rows),
        "classification": "deferred_acquisition_activity",
        "evidence_count": len(evidence),
        "evidence_paths_bounded": evidence[:3],
    }


STATUS_OK = "NO_REPLY"
STATUS_REVIEW = "SPLIT_LEDGER_PUBLIC_FINANCIALS_REVIEW"
CLASS_OK = "ok"
CLASS_REVIEW = "split-ledger-public-financials-review"


ADDRESS_ROOT_RE = re.compile(
    r"\b\d+\b.*\b(?:st|street|ave|avenue|rd|road|dr|drive|blvd|boulevard|pl|place|ln|lane|ct|court|cir|circle)\b",
    re.I,
)
MAX_NESTED_PROPERTY_ROOT_DEPTH = 4
STATE_DIR_RE = re.compile(r"^[A-Z]{2}$")
PROPERTY_ROOT_PRUNE_DIRS = {
    "Public",
    "Private",
    "Legal",
    "Tenant Ledgers",
    "Bank Statements",
    "Statements",
    "Photos",
    "Photos & Media",
}


def looks_like_nested_property_root(path: str, state_path: str) -> bool:
    rel_parts = Path(os.path.relpath(path, state_path)).parts
    if len(rel_parts) <= 1:
        return True
    if len(rel_parts) > MAX_NESTED_PROPERTY_ROOT_DEPTH:
        return False
    name = os.path.basename(path)
    if ADDRESS_ROOT_RE.search(name):
        return True
    if os.path.isdir(os.path.join(path, "Public")):
        return True
    if os.path.isdir(os.path.join(path, "07 - P&L & Owner Statements")):
        return True
    return False


def build_property_roots(base):
    roots = []
    seen = set()
    for state in sorted(os.listdir(base)):
        state_path = os.path.join(base, state)
        if not os.path.isdir(state_path):
            continue
        for current, dirnames, _filenames in os.walk(state_path):
            rel_parts = Path(os.path.relpath(current, state_path)).parts
            if rel_parts == (".",):
                continue
            if len(rel_parts) > MAX_NESTED_PROPERTY_ROOT_DEPTH:
                dirnames[:] = []
                continue
            dirnames[:] = [
                name
                for name in dirnames
                if not name.startswith(".")
                and name not in PROPERTY_ROOT_PRUNE_DIRS
                and not re.match(r"^\d{2}\s+-\s+", name)
            ]
            if len(rel_parts) > 1 and not STATE_DIR_RE.match(state):
                dirnames[:] = []
                continue
            looks_like_property = looks_like_nested_property_root(current, state_path)
            if not looks_like_property:
                continue
            rel = os.path.join(state, *rel_parts).replace(os.sep, "/")
            if rel in seen:
                continue
            seen.add(rel)
            name = os.path.basename(current)
            roots.append((rel, current, normalize(name), tokens(name)))
            if len(rel_parts) > 1:
                dirnames[:] = []
    return roots


def resolve_target_financials(root_path, create_dirs=True):
    pub_fin = os.path.join(root_path, "Public", "07 - P&L & Owner Statements")
    fin = os.path.join(root_path, "07 - P&L & Owner Statements")
    pub = os.path.join(root_path, "Public")

    # If folder name itself ends with 'Public', keep files in <root>/07 - P&L & Owner Statements.
    if os.path.basename(root_path).lower().endswith(" public"):
        if os.path.isdir(fin):
            return fin, False
        if create_dirs:
            os.makedirs(fin, exist_ok=True)
        return fin, True

    # For normal roots, prefer existing Public/07 - P&L & Owner Statements when present.
    if os.path.isdir(pub_fin):
        return pub_fin, False
    if os.path.isdir(fin):
        return fin, False
    if os.path.isdir(pub):
        if create_dirs:
            os.makedirs(pub_fin, exist_ok=True)
        return pub_fin, True

    if create_dirs:
        os.makedirs(fin, exist_ok=True)
    return fin, True


def choose_target_financials(root_path):
    target, _would_create = resolve_target_financials(root_path, create_dirs=True)
    return target


def best_match(prop, roots, real_estate_base=REAL_ESTATE_BASE):
    if prop in OVERRIDES:
        rel = OVERRIDES[prop]
        full = os.path.join(real_estate_base, rel)
        if os.path.isdir(full):
            return full, 1.0, f"override:{rel}"

    prop_norm = normalize(prop)
    prop_toks = tokens(prop)
    num = first_number(prop)

    candidates = roots
    if num:
        by_num = [r for r in roots if num in r[2].split()]
        if by_num:
            candidates = by_num

    best = (None, 0.0, "")
    for rel, full, name_norm, name_toks in candidates:
        inter = len(prop_toks & name_toks)
        union = max(1, len(prop_toks | name_toks))
        jacc = inter / union
        seq = SequenceMatcher(None, prop_norm, name_norm).ratio()
        score = 0.65 * jacc + 0.35 * seq
        if score > best[1]:
            best = (full, score, rel)

    return best


def consolidate_property_alias_groups(grouped, roots, real_estate_base):
    """Merge raw-ledger labels that map to one property street identity."""
    bundles: Dict[Tuple[str, str], List[Tuple[str, List[Dict[str, str]]]]] = {}
    for prop, rows in grouped.items():
        full, score, _rel = best_match(prop, roots, real_estate_base)
        root_key = full if full and score >= 0.38 else f"unresolved:{prop}"
        bundles.setdefault((root_key, ledger_property_identity(prop)), []).append((prop, rows))

    consolidated: Dict[str, List[Dict[str, str]]] = {}
    for records in bundles.values():
        representative = min((prop for prop, _rows in records), key=lambda value: (len(value), value.casefold()))
        consolidated[representative] = [row for _prop, rows in records for row in rows]
    return consolidated


def read_ledger_groups(source) -> Tuple[List[str], Dict[str, List[Dict[str, str]]], int, int, int]:
    grouped = defaultdict(list)
    total_rows = 0
    missing_property_rows = 0
    exact_duplicate_extra_row_count = 0
    seen_rows: set[Tuple[str, ...]] = set()
    with open(source, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        for row in reader:
            total_rows += 1
            if None in row:
                del row[None]
            row_key = tuple(str(row.get(field) or "") for field in fieldnames)
            if row_key in seen_rows:
                exact_duplicate_extra_row_count += 1
                continue
            seen_rows.add(row_key)
            prop = (row.get("Property") or "").strip()
            if not prop:
                missing_property_rows += 1
                continue
            grouped[prop].append(row)
    return fieldnames, grouped, total_rows, missing_property_rows, exact_duplicate_extra_row_count


def is_eco_company_dao_fee_revenue(row: Dict[str, str]) -> bool:
    """Identify ECO-owned intercompany revenue mapped to a DAO property."""
    kind = manual_accrual_kind(row)
    if kind not in {"dao_eco", "pm_eco"}:
        return False
    row_type = str(row.get("Type") or "").strip()
    category = str(row.get("Category") or "").strip()
    sub_category = str(row.get("Sub-category") or "").strip()
    generated_classification = row_type == "Revenue" and category == "Fees & Other Revenue"
    baselane_export_classification = (
        row_type == "Manual"
        and category == "Revenue"
        and sub_category == "Fees & Other Revenue"
    )
    expected_prefix = DAO_ECO_REVENUE_PREFIX if kind == "dao_eco" else PM_ECO_REVENUE_PREFIX
    return (
        safe_float(row.get("Amount")) > 0
        and (generated_classification or baselane_export_classification)
        and str(row.get("Merchant") or "").strip().startswith(expected_prefix)
        and str(row.get("Description") or "").strip().startswith(expected_prefix)
    )


def exclude_eco_company_revenue_from_dao_groups(
    grouped: Dict[str, List[Dict[str, str]]],
) -> Tuple[Dict[str, List[Dict[str, str]]], List[Dict[str, str]]]:
    """Keep ECO company revenue in the master ledger but out of DAO property ledgers."""
    filtered: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    excluded: List[Dict[str, str]] = []
    for prop, rows in grouped.items():
        for row in rows:
            if is_eco_company_dao_fee_revenue(row):
                excluded.append(row)
            else:
                filtered[prop].append(row)
    return filtered, excluded


def safe_float(value: object) -> float:
    try:
        return float(str(value or "0").replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return 0.0


def money_to_float(value: object) -> float:
    text = str(value or "").strip().replace("$", "").replace(",", "")
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    amount = float(text or "0")
    return -amount if negative else amount


def extract_statement_text(path: Path) -> str:
    if path.suffix.lower() != ".pdf":
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""
    try:
        completed = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            text=True,
            capture_output=True,
            timeout=20,
        )
    except Exception:
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout


def parse_citadel_statement_text(text: str) -> Dict[str, float] | None:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    money_re = re.compile(r"^\(?\$?-?\d[\d,]*\.\d{2}\)?$")
    money_any_re = re.compile(r"\(?\$-?\d[\d,]*\.\d{2}\)?")
    principal_balance = None
    paid_last_month = []

    for index, line in enumerate(lines):
        lowered = line.lower()
        if principal_balance is None and "outstanding principal balance" in lowered:
            inline_match = money_any_re.search(line)
            if inline_match:
                principal_balance = money_to_float(inline_match.group(0))
                continue
            for candidate in lines[index + 1 : index + 20]:
                if money_re.match(candidate):
                    principal_balance = money_to_float(candidate)
                    break
        if "paid last month" in lowered:
            labeled_values = {}
            for candidate in lines[index + 1 : index + 12]:
                candidate_lower = candidate.lower()
                values = money_any_re.findall(candidate)
                if values:
                    for label in ("principal", "interest", "escrow", "fees", "total"):
                        if candidate_lower.startswith(label):
                            labeled_values[label] = money_to_float(values[0])
                            break
                if money_re.match(candidate):
                    paid_last_month.append(money_to_float(candidate))
                if len(paid_last_month) >= 5:
                    break
            if len(labeled_values) >= 3:
                paid_last_month = [
                    labeled_values.get("principal", 0.0),
                    labeled_values.get("interest", 0.0),
                    labeled_values.get("escrow", 0.0),
                    labeled_values.get("fees", 0.0),
                    labeled_values.get("total", 0.0),
                ]
                break

    if principal_balance is None or len(paid_last_month) < 3:
        return None
    return {
        "principal_balance": round(principal_balance, 2),
        "paid_principal": round(paid_last_month[0], 2),
        "paid_interest": round(paid_last_month[1], 2),
        "paid_escrow": round(paid_last_month[2], 2),
        "paid_fees": round(paid_last_month[3], 2) if len(paid_last_month) > 3 else 0.0,
        "paid_total": round(paid_last_month[4], 2) if len(paid_last_month) > 4 else None,
    }


def parse_loandepot_statement_text(text: str) -> Dict[str, float] | None:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    money_any_re = re.compile(r"\(?\$-?\d[\d,]*\.\d{2}\)?")
    principal_balance = None
    explanation: dict[str, float] = {}
    past_payments: dict[str, float] = {}

    for index, line in enumerate(lines):
        lowered = line.lower()
        if principal_balance is None and "outstanding principal balance" in lowered:
            inline_match = money_any_re.search(line)
            if inline_match:
                principal_balance = money_to_float(inline_match.group(0))
        if lowered.startswith("principal") or lowered.startswith("interest") or lowered.startswith("escrow"):
            values = money_any_re.findall(line)
            if not values:
                continue
            if "paid year to date" in " ".join(lines[max(0, index - 4) : index]).lower():
                key = lowered.split()[0]
                past_payments[key] = money_to_float(values[0])
                continue
            key = "escrow" if lowered.startswith("escrow") else lowered.split()[0]
            explanation.setdefault(key, money_to_float(values[0]))
        if lowered.startswith("regular monthly payment") or lowered.startswith("current amount due"):
            values = money_any_re.findall(line)
            if values:
                explanation.setdefault("total", money_to_float(values[0]))

    principal = past_payments.get("principal", explanation.get("principal"))
    interest = past_payments.get("interest", explanation.get("interest"))
    escrow = past_payments.get("escrow", explanation.get("escrow"))
    total = explanation.get("total")
    if principal_balance is None or principal is None or interest is None or escrow is None:
        return None
    if total is None:
        total = round(float(principal) + float(interest) + float(escrow), 2)
    return {
        "principal_balance": round(principal_balance, 2),
        "paid_principal": round(principal, 2),
        "paid_interest": round(interest, 2),
        "paid_escrow": round(escrow, 2),
        "paid_fees": 0.0,
        "paid_total": round(total, 2),
    }


def parse_generic_mortgage_statement_text(text: str) -> Dict[str, Any] | None:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    money_any_re = re.compile(r"\(?\$-?\d[\d,]*\.\d{2}\)?")
    principal_balance = None
    interest_rate = None
    due_year = None
    due_month = None
    due_components: dict[str, float] = {}
    paid_components: dict[str, float] = {}
    in_past_payments = False

    for index, line in enumerate(lines):
        lowered = line.lower()
        if principal_balance is None and (
            "outstanding principal" in lowered
            or "outstanding balance" in lowered
        ):
            values = money_any_re.findall(line)
            if values:
                principal_balance = max(money_to_float(value) for value in values)
        if interest_rate is None and "interest rate" in lowered:
            rate_match = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
            if rate_match:
                interest_rate = float(rate_match.group(1))
        if due_year is None and ("payment due date" in lowered or "next due date" in lowered):
            date_match = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d{2}|\d{2})\b", line)
            if date_match:
                due_month = int(date_match.group(1))
                year_value = int(date_match.group(3))
                due_year = 2000 + year_value if year_value < 100 else year_value
        if "past payments breakdown" in lowered:
            in_past_payments = True
            continue
        if in_past_payments and lowered.startswith(("transaction activity", "important messages", "amount due")):
            in_past_payments = False

        values = money_any_re.findall(line)
        if not values:
            continue
        key = None
        if lowered.startswith("principal"):
            key = "principal"
        elif "principal" in lowered and not ("balance" in lowered and len(values) == 1):
            key = "principal"
        elif lowered.startswith("interest"):
            key = "interest"
        elif "interest" in lowered and "rate" not in lowered and not ("balance" in lowered and len(values) == 1):
            key = "interest"
        elif lowered.startswith("escrow"):
            key = "escrow"
        elif "escrow" in lowered and "balance" not in lowered:
            key = "escrow"
        elif lowered.startswith("fees") or lowered.startswith("total fees"):
            key = "fees"
        elif lowered.startswith("total"):
            key = "total"
        elif "regular monthly payment" in lowered:
            key = "total"
        if key is None:
            continue
        if key == "interest" and lowered.startswith("interest rate"):
            continue
        if in_past_payments:
            paid_components.setdefault(key, money_to_float(values[0]))
        else:
            due_components.setdefault(key, money_to_float(values[-1]))

    principal = paid_components.get("principal", due_components.get("principal"))
    interest = paid_components.get("interest", due_components.get("interest"))
    escrow = paid_components.get("escrow", due_components.get("escrow"))
    if principal_balance is None or principal is None or interest is None or escrow is None:
        return None
    paid_total = paid_components.get("total")
    if paid_total is None:
        paid_total = round(float(principal) + float(interest) + float(escrow) + float(paid_components.get("fees") or 0.0), 2)
    result: Dict[str, Any] = {
        "principal_balance": round(principal_balance, 2),
        "paid_principal": round(principal, 2),
        "paid_interest": round(interest, 2),
        "paid_escrow": round(escrow, 2),
        "paid_fees": round(float(paid_components.get("fees") or 0.0), 2),
        "paid_total": round(float(paid_total), 2),
    }
    if interest_rate is not None:
        result["interest_rate"] = round(interest_rate, 6)
    if due_year and due_month:
        result["due_year"] = due_year
        result["due_month"] = due_month
    if {"principal", "interest", "escrow"} <= due_components.keys():
        due_total = due_components.get("total")
        if due_total is None:
            due_total = round(
                float(due_components["principal"])
                + float(due_components["interest"])
                + float(due_components["escrow"])
                + float(due_components.get("fees") or 0.0),
                2,
            )
        result.update(
            {
                "due_principal": round(float(due_components["principal"]), 2),
                "due_interest": round(float(due_components["interest"]), 2),
                "due_escrow": round(float(due_components["escrow"]), 2),
                "due_fees": round(float(due_components.get("fees") or 0.0), 2),
                "due_total": round(float(due_total), 2),
            }
        )
    return result


def parse_mortgage_statement_text(text: str) -> Dict[str, Any] | None:
    generic = parse_generic_mortgage_statement_text(text)
    parsed = parse_citadel_statement_text(text) or parse_loandepot_statement_text(text) or generic
    if not parsed:
        return None
    if generic:
        merged = dict(generic)
        merged.update({key: value for key, value in parsed.items() if value is not None})
        return merged
    return parsed


def row_year_month(row: Dict[str, str]) -> Tuple[int, int] | None:
    for field in ("ISODate", "Date"):
        value = str(row.get(field) or "").strip()
        if not value:
            continue
        for fmt in ("%Y-%m-%d", "%B %d, %Y", "%B %e, %Y"):
            try:
                parsed = datetime.strptime(value, fmt)
                return parsed.year, parsed.month
            except ValueError:
                continue
    return None


def mortgage_statement_dirs_for_root(root_path: str, year: int) -> List[Path]:
    public_dir = Path(root_path) / "Public"
    if Path(root_path).name.lower().endswith(" public"):
        public_dir = Path(root_path)
    loan_dir = public_dir / "04 - Loan Documents"
    candidates = [loan_dir / str(year), loan_dir / "Mortgage Statements" / str(year), loan_dir / "Mortgage Statements", loan_dir]
    return [candidate for candidate in candidates if candidate.is_dir()]


def statement_file_year_month(path: Path) -> Tuple[int, int, int] | None:
    match = re.search(r"(20\d{2})[-_]?(\d{2})(?:[-_]?(\d{2}))?", path.name, flags=re.I)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or "31")


def iter_mortgage_statement_candidates(root_path: str, year: int):
    seen: set[Path] = set()
    for loan_dir in mortgage_statement_dirs_for_root(root_path, year):
        for path in loan_dir.rglob("*"):
            if not path.is_file() or path in seen:
                continue
            seen.add(path)
            if "statement" not in path.name.lower():
                continue
            file_ymd = statement_file_year_month(path)
            if not file_ymd:
                continue
            yield file_ymd, path


def select_statement_amount_variant(statement: Dict[str, Any], row_amount: float | None, prefer_due: bool = False) -> Dict[str, Any]:
    if row_amount is None:
        return statement
    target = abs(float(row_amount))
    variants = [("due", statement.get("due_total")), ("paid", statement.get("paid_total"))] if prefer_due else [
        ("paid", statement.get("paid_total")),
        ("due", statement.get("due_total")),
    ]
    for prefix, total in variants:
        if total is None or abs(target - float(total)) > 1.0:
            continue
        if prefix == "paid":
            selected = dict(statement)
        else:
            selected = {
                **statement,
                "paid_principal": statement.get("due_principal"),
                "paid_interest": statement.get("due_interest"),
                "paid_escrow": statement.get("due_escrow"),
                "paid_fees": statement.get("due_fees", 0.0),
                "paid_total": statement.get("due_total"),
            }
        selected["statement_component_source"] = prefix
        return selected
    return statement


def month_ordinal(year: int, month: int) -> int:
    return year * 12 + month


def parse_year_month(value: object) -> Tuple[int, int] | None:
    match = re.match(r"^\s*(20\d{2})-(\d{1,2})\s*$", str(value or ""))
    if not match:
        return None
    month = int(match.group(2))
    if month < 1 or month > 12:
        return None
    return int(match.group(1)), month


def parse_positive_int(value: object) -> int | None:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if parsed < 1:
        return None
    return parsed


def pmt(monthly_rate: float, term_months: int, principal: float) -> float:
    if term_months <= 0:
        return 0.0
    if abs(monthly_rate) <= RATE_ZERO_THRESHOLD:
        return principal / term_months
    return principal * monthly_rate / (1.0 - (1.0 + monthly_rate) ** (-term_months))


def principal_from_pmt(monthly_rate: float, term_months: int, monthly_payment: float) -> float:
    if term_months <= 0:
        return 0.0
    if abs(monthly_rate) <= RATE_ZERO_THRESHOLD:
        return monthly_payment * term_months
    return monthly_payment * (1.0 - (1.0 + monthly_rate) ** (-term_months)) / monthly_rate


def original_principal_from_balance_before_payment(
    *,
    monthly_rate: float,
    monthly_pi_payment: float,
    payment_index: int,
    balance_before_payment: float,
) -> float | None:
    if payment_index < 1:
        return None
    elapsed = payment_index - 1
    if abs(monthly_rate) <= RATE_ZERO_THRESHOLD:
        return float(balance_before_payment) + float(monthly_pi_payment) * elapsed
    growth = (1.0 + monthly_rate) ** elapsed
    paid_forward = float(monthly_pi_payment) * ((growth - 1.0) / monthly_rate)
    return (float(balance_before_payment) + paid_forward) / growth


def amortization_by_month_index(
    *,
    original_principal: float,
    annual_interest_rate: float,
    term_months: int,
    payment_index: int,
    monthly_escrow: float,
    monthly_pi_payment: float | None = None,
) -> Dict[str, Any] | None:
    if payment_index < 1 or payment_index > term_months:
        return None
    monthly_rate = float(annual_interest_rate) / 1200.0
    monthly_pi = float(monthly_pi_payment) if monthly_pi_payment is not None else pmt(monthly_rate, int(term_months), float(original_principal))
    elapsed = payment_index - 1
    if abs(monthly_rate) <= RATE_ZERO_THRESHOLD:
        balance_before = float(original_principal) - (monthly_pi * elapsed)
    else:
        balance_before = (
            float(original_principal) * ((1.0 + monthly_rate) ** elapsed)
            - monthly_pi * (((1.0 + monthly_rate) ** elapsed - 1.0) / monthly_rate)
        )
    interest = round(balance_before * monthly_rate, 2)
    principal = round(monthly_pi - interest, 2)
    return {
        "principal_balance": round(balance_before, 2),
        "paid_principal": principal,
        "paid_interest": interest,
        "paid_escrow": round(float(monthly_escrow), 2),
        "paid_fees": 0.0,
        "paid_total": round(principal + interest + float(monthly_escrow), 2),
        "monthly_pi_payment": round(monthly_pi, 2),
        "payment_index": payment_index,
        "term_months": int(term_months),
    }


def payment_index_from_terms(terms: Dict[str, Any], year: int, month: int) -> Tuple[int, str] | None:
    target_key = f"{year:04d}-{month:02d}"
    for mapping_key in (
        "payment_number_by_month",
        "payment_index_by_month",
        "payment_month_number_by_month",
        "month_number_by_month",
    ):
        payment_numbers = terms.get(mapping_key)
        if isinstance(payment_numbers, dict) and payment_numbers.get(target_key) is not None:
            payment_index = parse_positive_int(payment_numbers[target_key])
            if payment_index:
                return payment_index, mapping_key
    for month_key, number_key in (
        ("anchor_payment_month", "anchor_payment_number"),
        ("anchor_payment_month", "anchor_payment_index"),
        ("anchor_payment_month", "payment_month_number"),
        ("current_payment_month", "current_payment_number"),
        ("current_payment_month", "current_payment_index"),
        ("payment_month", "payment_number"),
        ("payment_month", "payment_index"),
        ("payment_month", "payment_month_number"),
    ):
        anchor_month = parse_year_month(terms.get(month_key))
        anchor_payment_number = parse_positive_int(terms.get(number_key))
        if anchor_month and anchor_payment_number:
            return (
                anchor_payment_number + month_ordinal(year, month) - month_ordinal(*anchor_month),
                number_key,
            )
    first_payment_month = parse_year_month(terms.get("first_payment_month"))
    if first_payment_month:
        return month_ordinal(year, month) - month_ordinal(*first_payment_month) + 1, "first_payment_month"
    return None


def load_amortization_terms(path: Path | None = None) -> Dict[str, Any]:
    path = Path(path or DEFAULT_AMORTIZATION_TERMS_PATH)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    terms = {}
    for record in data.get("loans") or []:
        if not isinstance(record, dict):
            continue
        property_name = str(record.get("property") or "").strip()
        if not property_name:
            continue
        terms[normalize(property_name)] = record
    return terms


def amortized_statement_from_terms(property_name: str, year: int, month: int) -> Dict[str, Any] | None:
    terms = load_amortization_terms().get(normalize(property_name))
    if not terms:
        return None
    annual_interest_rate = terms.get("annual_interest_rate")
    term_months = int(terms.get("term_months") or terms.get("loan_term_months") or 360)
    monthly_escrow = float(terms.get("monthly_escrow") or 0.0)
    if annual_interest_rate is None:
        return None
    payment_index = payment_index_from_terms(terms, year, month)
    monthly_rate = float(annual_interest_rate) / 1200.0
    original_principal = terms.get("original_principal")
    original_principal_source = "original_principal"
    anchor_month = parse_year_month(terms.get("anchor_payment_month"))
    anchor_payment_index = payment_index_from_terms(terms, *anchor_month) if anchor_month else payment_index
    if (
        original_principal is None
        and anchor_payment_index
        and terms.get("principal_balance_before_payment") is not None
        and terms.get("monthly_pi_payment") is not None
    ):
        original_principal = original_principal_from_balance_before_payment(
            monthly_rate=monthly_rate,
            monthly_pi_payment=float(terms["monthly_pi_payment"]),
            payment_index=anchor_payment_index[0],
            balance_before_payment=float(terms["principal_balance_before_payment"]),
        )
        original_principal_source = "principal_balance_before_payment"
    if original_principal is None and terms.get("monthly_pi_payment") is not None:
        original_principal = principal_from_pmt(monthly_rate, term_months, float(terms["monthly_pi_payment"]))
        original_principal_source = "monthly_pi_payment"
    if payment_index and original_principal is not None:
        statement = amortization_by_month_index(
            original_principal=float(original_principal),
            annual_interest_rate=float(annual_interest_rate),
            term_months=term_months,
            payment_index=payment_index[0],
            monthly_escrow=monthly_escrow,
            monthly_pi_payment=float(terms["monthly_pi_payment"]) if terms.get("monthly_pi_payment") is not None else None,
        )
        if not statement:
            return None
        return {
            **statement,
            "interest_rate": round(float(annual_interest_rate), 6),
            "statement_component_source": "amortization_terms_config_pmt_month_number",
            "amortization_terms_source": str(DEFAULT_AMORTIZATION_TERMS_PATH),
            "payment_index_source": payment_index[1],
            "original_principal_source": original_principal_source,
        }
    if not anchor_month:
        return None
    anchor = {
        "principal_balance": float(terms.get("principal_balance_before_payment") or 0.0),
        "paid_principal": float(terms.get("anchor_principal") or 0.0),
        "paid_interest": float(terms.get("anchor_interest") or 0.0),
        "paid_escrow": monthly_escrow,
        "paid_total": round(float(terms.get("monthly_pi_payment") or 0.0) + monthly_escrow, 2),
        "interest_rate": float(annual_interest_rate),
    }
    statement = amortized_statement_from_anchor(anchor, anchor_month[0], anchor_month[1], year, month)
    if not statement:
        return None
    return {
        **statement,
        "statement_component_source": "amortization_terms_config_anchor",
        "amortization_terms_source": str(DEFAULT_AMORTIZATION_TERMS_PATH),
    }


def amortized_statement_from_anchor(statement: Dict[str, Any], anchor_year: int, anchor_month: int, year: int, month: int) -> Dict[str, Any] | None:
    rate = statement.get("interest_rate")
    principal_balance = statement.get("principal_balance")
    principal = statement.get("paid_principal")
    interest = statement.get("paid_interest")
    escrow = statement.get("paid_escrow")
    if rate is None or principal_balance is None or principal is None or interest is None or escrow is None:
        return None
    monthly_rate = float(rate) / 1200.0
    payment_pi = round(float(principal) + float(interest), 2)
    balance = float(principal_balance)
    delta = month_ordinal(year, month) - month_ordinal(anchor_year, anchor_month)
    projected_principal = float(principal)
    projected_interest = float(interest)
    if delta > 0:
        for _ in range(delta):
            projected_interest = round(balance * monthly_rate, 2)
            projected_principal = round(payment_pi - projected_interest, 2)
            balance = round(balance - projected_principal, 2)
    elif delta < 0:
        if abs(monthly_rate) <= RATE_ZERO_THRESHOLD:
            previous_balance = balance + payment_pi
            projected_principal = payment_pi
            projected_interest = 0.0
            balance = previous_balance
            for _ in range(abs(delta) - 1):
                previous_balance = balance + payment_pi
                projected_principal = payment_pi
                projected_interest = 0.0
                balance = previous_balance
        else:
            for _ in range(abs(delta)):
                previous_balance = round((balance + payment_pi) / (1.0 + monthly_rate), 2)
                projected_principal = round(previous_balance - balance, 2)
                projected_interest = round(payment_pi - projected_principal, 2)
                balance = previous_balance
    return {
        **statement,
        "principal_balance": round(balance, 2),
        "paid_principal": round(projected_principal, 2),
        "paid_interest": round(projected_interest, 2),
        "paid_escrow": round(float(escrow), 2),
        "paid_fees": 0.0,
        "paid_total": round(projected_principal + projected_interest + float(escrow), 2),
        "statement_component_source": "amortization_schedule_fallback",
        "statement_fallback_anchor_year": anchor_year,
        "statement_fallback_anchor_month": anchor_month,
    }


def mortgage_statement_for_root(root_path: str, year: int, month: int, row_amount: float | None = None, property_name: str | None = None) -> Dict[str, Any] | None:
    terms_projected = None
    if property_name:
        terms_projected = amortized_statement_from_terms(property_name, year, month)
    candidates = []
    parsed_statements = []
    for (file_year, file_month, file_day), path in iter_mortgage_statement_candidates(root_path, year):
        if abs(month_ordinal(year, month) - month_ordinal(file_year, file_month)) > 12:
            continue
        parsed = parse_mortgage_statement_text(extract_statement_text(path))
        if parsed:
            parsed = {**parsed, "statement_path": str(path)}
            parsed_statements.append((file_year, file_month, file_day, path, parsed))
            if (file_year, file_month) == (year, month) or (
                parsed.get("due_year") == year and parsed.get("due_month") == month
            ):
                candidates.append((file_year, file_month, file_day, path, parsed))
    if terms_projected:
        paid_total = terms_projected.get("paid_total")
        if row_amount is None or paid_total is None or abs(abs(float(row_amount)) - float(paid_total)) <= 1.0:
            projected = dict(terms_projected)
            for _file_year, _file_month, file_day, path, parsed in sorted(candidates, key=lambda item: item[2], reverse=True):
                prefer_due = (
                    parsed.get("due_year") == year
                    and parsed.get("due_month") == month
                    and (_file_year, _file_month) != (year, month)
                )
                selected = select_statement_amount_variant(parsed, row_amount, prefer_due=prefer_due)
                selected_total = selected.get("paid_total")
                if row_amount is not None and selected_total is not None and abs(abs(float(row_amount)) - float(selected_total)) <= 1.0:
                    projected["statement_path"] = str(path)
                    projected["statement_evidence_component_source"] = selected.get("statement_component_source")
                    break
            return projected
    for _file_year, _file_month, file_day, path, parsed in sorted(candidates, key=lambda item: item[2], reverse=True):
        prefer_due = (
            parsed.get("due_year") == year
            and parsed.get("due_month") == month
            and (_file_year, _file_month) != (year, month)
        )
        selected = select_statement_amount_variant(parsed, row_amount, prefer_due=prefer_due)
        paid_total = selected.get("paid_total")
        if row_amount is not None and paid_total is not None and abs(abs(float(row_amount)) - float(paid_total)) > 1.0:
            continue
        return {**selected, "statement_path": str(path)}
    fallback_candidates = sorted(
        parsed_statements,
        key=lambda item: abs(month_ordinal(year, month) - month_ordinal(item[0], item[1])),
    )
    for file_year, file_month, _file_day, path, parsed in fallback_candidates:
        projected = amortized_statement_from_anchor(parsed, file_year, file_month, year, month)
        if not projected:
            continue
        paid_total = projected.get("paid_total")
        if row_amount is not None and paid_total is not None and abs(abs(float(row_amount)) - float(paid_total)) > 1.0:
            continue
        return {**projected, "statement_path": str(path)}
    return None


def row_contains_citadel(row: Dict[str, str]) -> bool:
    haystack = " ".join(str(row.get(field) or "") for field in ("Merchant", "Description", "Category", "Sub-category", "Notes"))
    return bool(CITADEL_TEXT_RE.search(haystack))


def is_unsplit_citadel_mortgage_parent(row: Dict[str, str]) -> bool:
    category = str(row.get("Category") or "").strip()
    return row_contains_citadel(row) and category == "Mortgage Payments" and safe_float(row.get("Amount")) < 0


def format_amount(value: float) -> str:
    return f"{round(value, 2):.2f}".rstrip("0").rstrip(".")


def add_months(year: int, month: int, offset: int) -> Tuple[int, int]:
    index = (year * 12) + (month - 1) + offset
    return index // 12, (index % 12) + 1


def month_label(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def escrow_native_split_schedule(
    *,
    year: int,
    month: int,
    insurance: float,
    taxes: float,
    statement_path: object | None,
) -> Dict[str, Any]:
    start_year, start_month = add_months(year, month, 1)
    end_year, end_month = add_months(start_year, start_month, ESCROW_NATIVE_SPLIT_SCHEDULE_MONTHS - 1)
    evidence_present = bool(statement_path)
    escrow_disbursement_amount = round(float(insurance) + float(taxes), 2)
    escrow_disbursement_detected = escrow_disbursement_amount > CONFLICT_THRESHOLD
    status = "statement_verified" if evidence_present and escrow_disbursement_detected else "blocked_missing_statement_evidence"
    if evidence_present and not escrow_disbursement_detected:
        status = "no_escrow_disbursement_detected"
    insurance_native_split_amount = round(float(insurance), 2)
    taxes_native_split_amount = round(float(taxes), 2)
    monthly_splits = []
    for offset in range(ESCROW_NATIVE_SPLIT_SCHEDULE_MONTHS):
        split_year, split_month = add_months(start_year, start_month, offset)
        monthly_splits.append(
            {
                "month": month_label(split_year, split_month),
                "insurance": insurance_native_split_amount,
                "taxes": taxes_native_split_amount,
                "insurance_native_split_amount": insurance_native_split_amount,
                "taxes_native_split_amount": taxes_native_split_amount,
                "source": "statement_paid_escrow_disbursement",
            }
        )
    return {
        "status": status,
        "source": "mortgage_statement_escrow_disbursement",
        "statement_evidence_required": True,
        "statement_evidence_present": evidence_present,
        "source_statement_path": str(statement_path) if statement_path else None,
        "escrow_disbursement_detected": escrow_disbursement_detected,
        "escrow_disbursement_amount": escrow_disbursement_amount,
        "schedule_months": ESCROW_NATIVE_SPLIT_SCHEDULE_MONTHS,
        "effective_start_month": month_label(start_year, start_month),
        "effective_end_month": month_label(end_year, end_month),
        "insurance_monthly_amount": insurance_native_split_amount,
        "taxes_monthly_amount": taxes_native_split_amount,
        "insurance_native_split_amount": insurance_native_split_amount,
        "taxes_native_split_amount": taxes_native_split_amount,
        "native_split_amount_source": "statement_paid_escrow_disbursement",
        "annual_escrow_reset": True,
        "annual_escrow_reset_reason": "taxes_and_insurance_change_every_year",
        "native_split_schedule_semantics": (
            "Use the statement-paid tax and insurance escrow disbursement amounts as the native split "
            "amounts for each month in the next 12-month live Baselane schedule."
        ),
        "monthly_native_splits": monthly_splits,
        "upstream": "live_baselane_native_splits",
        "native_split_update_required": escrow_disbursement_detected,
        "native_split_update_ready": status == "statement_verified",
        "native_split_update_mode": "refresh_next_12_months_from_statement_paid_escrow",
        "applies_to_categories": ["Insurance", "Taxes"],
        "policy": (
            "When an escrow tax or insurance disbursement appears on a mortgage statement, "
            "use the statement-paid amount to refresh the monthly native split amount for the next 12 months."
        ),
    }


def split_child_row(parent: Dict[str, str], amount: float, category: str, merchant: str, notes: str = "") -> Dict[str, str]:
    row = dict(parent)
    row["Amount"] = format_amount(amount)
    row["Category"] = category
    row["Merchant"] = merchant
    if "Notes" in row:
        row["Notes"] = notes
    if category in {"Insurance", "Taxes"}:
        row["Type"] = "Operating Expenses"
    elif category in {"Mortgage Principal Payments", "Mortgage Interest Payments"}:
        row["Type"] = "Loan Payments & Capex"
    return row


DAO_P_AND_I_MORTGAGE_PROPERTIES = set(P_AND_I_DAO_PROPERTIES)


def dao_pays_mortgage_principal_interest(prop: str) -> bool:
    return is_p_and_i_dao_property(prop)


def citadel_escrow_components(rows: List[Dict[str, str]], year: int, month: int, paid_escrow: float) -> Tuple[float, float]:
    prior_insurance = None
    prior_taxes = None
    for row in sorted(rows, key=lambda item: row_year_month(item) or (0, 0), reverse=True):
        ym = row_year_month(row)
        if not ym or ym >= (year, month):
            continue
        category = str(row.get("Category") or "").strip()
        amount = abs(safe_float(row.get("Amount")))
        if amount <= CONFLICT_THRESHOLD:
            continue
        if prior_insurance is None and category in {"Insurance", "Rental Dwelling"}:
            prior_insurance = round(amount, 2)
        if prior_taxes is None and category in {"Taxes", "City, State, & Local Taxes"}:
            prior_taxes = round(amount, 2)
        if prior_insurance is not None and prior_taxes is not None:
            break
    insurance = prior_insurance if prior_insurance is not None else 0.0
    taxes = round(float(paid_escrow) - insurance, 2)
    if taxes < 0:
        insurance = 0.0
        taxes = round(float(paid_escrow), 2)
    return insurance, taxes


def apply_mortgage_statement_splits(prop: str, rows: List[Dict[str, str]], root_path: str | None = None) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    if not root_path:
        return rows, {"applied_count": 0, "status": "not_checked"}
    transformed: List[Dict[str, str]] = []
    applied = []
    statements: Dict[Tuple[int, int], Dict[str, Any] | None] = {}
    existing_split_months = {
        ym
        for row in rows
        for ym in [row_year_month(row)]
        if ym and str(row.get("Category") or "").strip() in {"Mortgage Principal Payments", "Mortgage Interest Payments"}
    }
    for row in rows:
        ym = row_year_month(row)
        if not ym or not is_unsplit_citadel_mortgage_parent(row) or ym in existing_split_months:
            transformed.append(row)
            continue
        statement_key = (ym[0], ym[1], round(abs(safe_float(row.get("Amount"))), 2))
        statement = statements.setdefault(statement_key, mortgage_statement_for_root(root_path, ym[0], ym[1], safe_float(row.get("Amount")), prop))
        if not statement:
            transformed.append(row)
            continue
        paid_total = statement.get("paid_total")
        if paid_total is not None and abs(abs(safe_float(row.get("Amount"))) - float(paid_total)) > 1.0:
            transformed.append(row)
            continue
        if not dao_pays_mortgage_principal_interest(prop):
            transfer = dict(row)
            transfer["Category"] = "Transfers Between Accounts"
            if "Sub-category" in transfer:
                transfer["Sub-category"] = "Transfers Between Accounts"
            transfer["Type"] = "Transfers & Other"
            transformed.append(transfer)
            applied.append(
                {
                    "property": prop,
                    "year": ym[0],
                    "month": ym[1],
                    "parent_amount": round(safe_float(row.get("Amount")), 2),
                    "child_count": 0,
                    "statement_path": statement.get("statement_path"),
                    "statement_component_source": statement.get("statement_component_source"),
                    "cash_basis_transfer": True,
                }
            )
            continue
        insurance, taxes = citadel_escrow_components(rows, ym[0], ym[1], float(statement["paid_escrow"]))
        children = []
        children.extend(
            [
                split_child_row(row, -float(statement["paid_principal"]), "Mortgage Principal Payments", f"{prop} Mortgage Principal"),
                split_child_row(row, -float(statement["paid_interest"]), "Mortgage Interest Payments", f"{prop} Mortgage Interest"),
            ]
        )
        paid_fees = float(statement.get("paid_fees") or 0.0)
        if paid_fees > CONFLICT_THRESHOLD:
            children.append(split_child_row(row, -paid_fees, "Mortgage Payments", f"{prop} Mortgage Fees"))
        if insurance > CONFLICT_THRESHOLD:
            children.append(split_child_row(row, -insurance, "Insurance", f"{prop} Mortgage Escrow - Insurance"))
        if taxes > CONFLICT_THRESHOLD:
            children.append(split_child_row(row, -taxes, "Taxes", f"{prop} Mortgage Escrow - Property Taxes"))
        schedule = escrow_native_split_schedule(
            year=ym[0],
            month=ym[1],
            insurance=insurance,
            taxes=taxes,
            statement_path=statement.get("statement_path"),
        )
        transformed.extend(children)
        applied.append(
            {
                "property": prop,
                "year": ym[0],
                "month": ym[1],
                "parent_amount": round(safe_float(row.get("Amount")), 2),
                "child_count": len(children),
                "statement_path": statement.get("statement_path"),
                "statement_component_source": statement.get("statement_component_source"),
                "escrow_native_split_schedule": schedule,
                "escrow_native_split_schedule_status": schedule["status"],
                "escrow_native_split_schedule_months": schedule["schedule_months"],
                "escrow_statement_evidence_required": schedule["statement_evidence_required"],
                "escrow_statement_evidence_present": schedule["statement_evidence_present"],
                "escrow_insurance_monthly_amount": schedule["insurance_monthly_amount"],
                "escrow_taxes_monthly_amount": schedule["taxes_monthly_amount"],
                "escrow_schedule_effective_start_month": schedule["effective_start_month"],
                "escrow_schedule_effective_end_month": schedule["effective_end_month"],
            }
        )
    return transformed, {"applied_count": len(applied), "applied_bounded": applied[:25], "status": "applied" if applied else "not_applicable"}


def apply_citadel_statement_splits(prop: str, rows: List[Dict[str, str]], root_path: str | None = None) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    return apply_mortgage_statement_splits(prop, rows, root_path)


def canonical_csv_text(fieldnames: List[str], rows: List[Dict[str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in fieldnames})
    return buffer.getvalue()


def rows_digest(fieldnames: List[str], rows: List[Dict[str, str]]) -> str:
    return hashlib.sha256(canonical_csv_text(fieldnames, rows).encode("utf-8")).hexdigest()


def output_plan_digest(records: List[Dict[str, Any]]) -> str:
    canonical = json.dumps(
        [
            {
                "property": record.get("property"),
                "target": record.get("target"),
                "row_count": record.get("row_count"),
                "amount_total": record.get("amount_total"),
                "source_digest": record.get("source_digest"),
            }
            for record in records
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def public_ledger_rows(fieldnames: List[str], rows: List[Dict[str, str]]) -> tuple[List[str], List[Dict[str, str]]]:
    filtered_fieldnames = [fieldname for fieldname in fieldnames if fieldname != "Account"]
    filtered_rows = [{key: value for key, value in row.items() if key != "Account"} for row in rows]
    return filtered_fieldnames, filtered_rows


def write_property_csv(out_path: str, fieldnames: List[str], rows: List[Dict[str, str]]) -> None:
    target = Path(out_path)
    filtered_fieldnames, filtered_rows = public_ledger_rows(fieldnames, rows)
    # Dropbox can lock a newly created temp file before the copy/unlink cycle
    # completes. Stage on ext4 and copy only the completed artifact upstream.
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        prefix=f".{target.name}.tmp.",
        suffix=".csv",
        delete=False,
    ) as wf:
        tmp_path = Path(wf.name)
        writer = csv.DictWriter(wf, fieldnames=filtered_fieldnames)
        writer.writeheader()
        writer.writerows(filtered_rows)
    try:
        copied = subprocess.run(["cp", "-f", str(tmp_path), str(target)], text=True, capture_output=True)
        if copied.returncode != 0:
            raise OSError(f"cp failed for {target}: {copied.stderr.strip()}")
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def amount_total(rows: List[Dict[str, str]]) -> float:
    return round(sum(safe_float(row.get("Amount")) for row in rows), 2)


def existing_output_state(path: Path, fieldnames: List[str], source_rows: List[Dict[str, str]]) -> Dict[str, Any]:
    public_fieldnames, public_rows = public_ledger_rows(fieldnames, source_rows)
    source_digest = rows_digest(public_fieldnames, public_rows)
    source_row_count = len(public_rows)
    source_amount_total = amount_total(public_rows)
    base = {
        "path": str(path),
        "source_row_count": source_row_count,
        "source_amount_total": source_amount_total,
        "source_digest": source_digest,
        "existing": path.is_file(),
        "existing_row_count": None,
        "existing_amount_total": None,
        "existing_digest": None,
        "current": False,
        "status": "missing",
    }
    if not path.is_file():
        return base
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            existing_rows = []
            for row in reader:
                if None in row:
                    del row[None]
                existing_rows.append(row)
    except Exception as exc:  # noqa: BLE001
        base["status"] = "unreadable"
        base["error"] = str(exc)
        return base
    existing_fieldnames = reader.fieldnames or []
    digest_fieldnames = public_fieldnames if existing_fieldnames == public_fieldnames else existing_fieldnames
    existing_digest = rows_digest(digest_fieldnames, existing_rows)
    current = existing_fieldnames == public_fieldnames and existing_digest == source_digest
    base.update(
        {
            "existing_row_count": len(existing_rows),
            "existing_amount_total": amount_total(existing_rows),
            "existing_digest": existing_digest,
            "current": current,
            "status": "current" if current else "stale",
        }
    )
    return base


def planned_output_record(
    prop: str,
    rows: List[Dict[str, str]],
    fieldnames: List[str],
    target_fin: str,
    score: float,
    rel: str,
) -> Dict[str, Any]:
    target_dir = Path(target_fin)
    out_path = canonical_output_ledger_path(target_dir, prop, allow_divergent_replacement=True)
    aliases = equivalent_output_ledger_paths(target_dir, prop)
    output_state = existing_output_state(out_path, fieldnames, rows)
    return {
        "property": prop,
        "target": str(out_path),
        "match_score": round(score, 3),
        "matched_root": rel,
        "row_count": len(rows),
        "amount_total": amount_total(rows),
        "source_digest": output_state["source_digest"],
        "output_status": output_state["status"],
        "output_current": output_state["current"],
        "existing_row_count": output_state["existing_row_count"],
        "existing_amount_total": output_state["existing_amount_total"],
        "existing_digest": output_state["existing_digest"],
        "obsolete_aliases": [str(path) for path in aliases if path != out_path],
    }


def review_command(script_path: Path, source: Path, real_estate_base: Path) -> str:
    return " ".join(
        [
            "python3",
            shlex.quote(str(script_path)),
            "--source",
            shlex.quote(str(source)),
            "--real-estate-base",
            shlex.quote(str(real_estate_base)),
            "--json",
        ]
    )


def validate_review_command(command: str, script_path: Path) -> Dict[str, Any]:
    issues = []
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return {"valid": False, "issues": [f"shell parse failed: {exc}"], "parts": []}

    script = str(script_path)
    if len(parts) < 6:
        issues.append("review command is too short")
    if not parts or parts[0] != "python3":
        issues.append("review command must start with python3")
    if len(parts) < 2 or parts[1] != script:
        issues.append("review command must use the current helper path")
    if "--json" not in parts[2:]:
        issues.append("review command must include --json")
    if "--source" not in parts[2:]:
        issues.append("review command must include --source")
    if "--real-estate-base" not in parts[2:]:
        issues.append("review command must include --real-estate-base")
    for flag in ("--apply", "--write", "--delete", "--sync", "--restart"):
        if flag in parts:
            issues.append(f"review command must not include {flag}")
    if not script_path.exists():
        issues.append("review helper path is missing")
    elif not script_path.is_file():
        issues.append("review helper path is not a file")

    return {"valid": not issues, "issues": issues, "parts": parts}


def issue_record(message: str, source: Path, real_estate_base: Path, script_path: Path) -> Dict[str, Any]:
    command = review_command(script_path, source, real_estate_base)
    validation = validate_review_command(command, script_path)
    return {
        "title": "Split ledger public financials review",
        "issue": message,
        "issue_class": CLASS_REVIEW,
        "classification": CLASS_REVIEW,
        "requires_operator_approval": True,
        "requires_interactive_sudo": False,
        "requires_interactive_oauth": False,
        "safe_to_run_automatically": False,
        "review_command": command,
        "review_command_safe_to_run_automatically": True,
        "review_command_valid": validation["valid"],
        "review_command_validation": validation,
        "command": None,
        "cleanup_command_after_review": None,
        "restart_command_after_review": None,
        "oauth_command_after_review": None,
    }


def summarize_issues(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    valid = sum(1 for record in records if record.get("review_command_valid"))
    invalid = sum(1 for record in records if record.get("review_command") and not record.get("review_command_valid"))
    validation_issues = []
    for record in records:
        validation = record.get("review_command_validation") or {}
        validation_issues.extend(validation.get("issues") or [])
    classes = sorted({record.get("issue_class") for record in records if record.get("issue_class")})
    return {
        "total": len(records),
        "issue_count": len(records),
        "covered_count": len(records),
        "uncovered_count": 0,
        "approval_required_count": sum(1 for record in records if record.get("requires_operator_approval")),
        "requires_operator_approval_count": sum(1 for record in records if record.get("requires_operator_approval")),
        "requires_interactive_sudo_count": sum(1 for record in records if record.get("requires_interactive_sudo")),
        "requires_interactive_oauth_count": sum(1 for record in records if record.get("requires_interactive_oauth")),
        "safe_review_command_count": sum(1 for record in records if record.get("review_command_safe_to_run_automatically")),
        "valid_review_command_count": valid,
        "invalid_review_command_count": invalid,
        "review_command_validation_issues": validation_issues,
        "classes": classes,
        "class_counts": {CLASS_REVIEW: len(records)} if records else {},
        "route_classification_counts": {CLASS_REVIEW: len(records)} if records else {},
    }


def build_report(
    source=SOURCE,
    real_estate_base=REAL_ESTATE_BASE,
    script_path=None,
    *,
    require_current_outputs: bool = False,
    write_attempted: bool = False,
    delete_attempted: bool = False,
    deleted_obsolete_alias_count: int = 0,
    skip_excluded_properties: bool = True,
) -> Dict[str, Any]:
    source_path = Path(source)
    real_estate_path = Path(real_estate_base)
    helper_path = Path(script_path) if script_path else Path(__file__).resolve()
    records: List[Dict[str, Any]] = []
    fieldnames: List[str] = []
    grouped: Dict[str, List[Dict[str, str]]] = {}
    total_rows = 0
    missing_property_rows = 0
    exact_duplicate_extra_row_count = 0
    roots = []
    planned_write_count = 0
    planned_row_count = 0
    unresolved_count = 0
    would_create_target_dir_count = 0
    existing_target_dir_count = 0
    planned_outputs: List[Dict[str, Any]] = []
    unresolved_outputs: List[Dict[str, Any]] = []
    deferred_acquisition_outputs: List[Dict[str, Any]] = []
    excluded_write_skipped_outputs: List[Dict[str, Any]] = []
    eco_company_revenue_excluded_rows: List[Dict[str, str]] = []
    citadel_statement_split_applied_count = 0
    citadel_statement_split_records: List[Dict[str, Any]] = []
    exclusion_guards, exclusion_report = split_exclusion_guards()

    if not source_path.is_file():
        records.append(issue_record(f"Source ledger missing: {source_path}", source_path, real_estate_path, helper_path))
    else:
        try:
            (
                fieldnames,
                grouped,
                total_rows,
                missing_property_rows,
                exact_duplicate_extra_row_count,
            ) = read_ledger_groups(source_path)
            grouped, eco_company_revenue_excluded_rows = exclude_eco_company_revenue_from_dao_groups(grouped)
        except Exception as exc:
            records.append(issue_record(f"Source ledger unreadable: {exc}", source_path, real_estate_path, helper_path))

    if not real_estate_path.is_dir():
        records.append(issue_record(f"Real estate base missing: {real_estate_path}", source_path, real_estate_path, helper_path))
    else:
        try:
            roots = build_property_roots(str(real_estate_path))
        except Exception as exc:
            records.append(issue_record(f"Real estate base unreadable: {exc}", source_path, real_estate_path, helper_path))

    if grouped and roots:
        grouped = consolidate_property_alias_groups(grouped, roots, str(real_estate_path))
        for prop, rows in sorted(grouped.items(), key=lambda kv: kv[0].lower()):
            full, score, rel = best_match(prop, roots, str(real_estate_path))
            exclusion = split_exclusion_match(prop, full, rel, exclusion_guards) if skip_excluded_properties else None
            if exclusion:
                excluded_write_skipped_outputs.append(
                    {
                        "property": prop,
                        "row_count": len(rows),
                        "amount_total": amount_total(rows),
                        "best_match_score": round(score, 3),
                        "best_match_root": rel,
                        "target_financials": None,
                        "exclude_source": exclusion.get("source"),
                        "exclude_status": exclusion.get("status"),
                        "exclude_reason": exclusion.get("reason"),
                        "exclude_property_name": exclusion.get("property_name"),
                    }
                )
                continue
            if not full or score < 0.38:
                acquisition_record = deferred_acquisition_record(prop, rows, real_estate_path)
                if acquisition_record:
                    acquisition_record.update(
                        {
                            "best_match_score": round(score, 3),
                            "best_match_root": rel,
                        }
                    )
                    deferred_acquisition_outputs.append(acquisition_record)
                    continue
                unresolved_count += 1
                unresolved_outputs.append(
                    {
                        "property": prop,
                        "row_count": len(rows),
                        "amount_total": amount_total(rows),
                        "best_match_score": round(score, 3),
                        "best_match_root": rel,
                    }
                )
                continue
            target_fin, would_create = resolve_target_financials(full, create_dirs=False)
            rows, citadel_split_report = apply_citadel_statement_splits(prop, rows, full)
            citadel_statement_split_applied_count += int(citadel_split_report.get("applied_count") or 0)
            citadel_statement_split_records.extend(citadel_split_report.get("applied_bounded") or [])
            output_record = planned_output_record(prop, rows, fieldnames, target_fin, score, rel)
            planned_write_count += 1
            planned_row_count += len(rows)
            planned_outputs.append(output_record)
            if would_create:
                would_create_target_dir_count += 1
            elif os.path.isdir(target_fin):
                existing_target_dir_count += 1

    if unresolved_count:
        records.append(
            issue_record(
                f"Unresolved ledger properties: {unresolved_count}",
                source_path,
                real_estate_path,
                helper_path,
            )
        )

    output_missing_count = sum(1 for record in planned_outputs if record["output_status"] == "missing")
    output_stale_count = sum(1 for record in planned_outputs if record["output_status"] == "stale")
    output_unreadable_count = sum(1 for record in planned_outputs if record["output_status"] == "unreadable")
    output_current_count = sum(1 for record in planned_outputs if record["output_status"] == "current")
    planned_obsolete_alias_delete_count = sum(
        len(record.get("obsolete_aliases") or []) for record in planned_outputs
    )
    output_current_properties = sorted(
        record["property"] for record in planned_outputs if record["output_status"] == "current"
    )
    output_mismatch_count = output_missing_count + output_stale_count + output_unreadable_count
    unresolved_row_count = sum(record["row_count"] for record in unresolved_outputs)
    unresolved_amount_total = round(sum(record["amount_total"] for record in unresolved_outputs), 2)
    deferred_acquisition_row_count = sum(record["row_count"] for record in deferred_acquisition_outputs)
    deferred_acquisition_amount_total = round(
        sum(record["amount_total"] for record in deferred_acquisition_outputs),
        2,
    )
    excluded_write_skipped_row_count = sum(record["row_count"] for record in excluded_write_skipped_outputs)
    excluded_write_skipped_amount_total = round(
        sum(record["amount_total"] for record in excluded_write_skipped_outputs),
        2,
    )

    if require_current_outputs and output_mismatch_count:
        records.append(
            issue_record(
                (
                    "Property ledger exports not current: "
                    f"missing={output_missing_count} stale={output_stale_count} unreadable={output_unreadable_count}"
                ),
                source_path,
                real_estate_path,
                helper_path,
            )
        )

    status = STATUS_REVIEW if records else STATUS_OK
    classification = CLASS_REVIEW if records else CLASS_OK
    summary = summarize_issues(records)
    issues = [record["issue"] for record in records]

    return {
        "status": status,
        "classification": classification,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "ok_state": not records,
        "ok": not records,
        "visible_ok": not records,
        "issues": issues,
        "issue_count": len(records),
        "issue_classes": sorted({record["issue_class"] for record in records}),
        "classified_issues": records,
        "issue_records": records,
        "structured_issues": records,
        "classified_issue_summary": summary,
        "remediation": {
            "command": None,
            "cleanup_command_after_review": None,
            "restart_command_after_review": None,
            "oauth_command_after_review": None,
        },
        "approval_required_count": summary["approval_required_count"],
        "requires_operator_approval_count": summary["requires_operator_approval_count"],
        "requires_interactive_sudo_count": summary["requires_interactive_sudo_count"],
        "requires_interactive_oauth_count": summary["requires_interactive_oauth_count"],
        "safe_review_command_count": summary["safe_review_command_count"],
        "valid_review_command_count": summary["valid_review_command_count"],
        "invalid_review_command_count": summary["invalid_review_command_count"],
        "review_command_validation_issues": summary["review_command_validation_issues"],
        "source": str(source_path),
        "source_sha256": file_digest(source_path),
        "real_estate_base": str(real_estate_path),
        "source_exists": source_path.is_file(),
        "real_estate_base_exists": real_estate_path.is_dir(),
        "field_count": len(fieldnames),
        "total_row_count": total_rows,
        "deduped_row_count": total_rows - exact_duplicate_extra_row_count,
        "exact_duplicate_extra_row_count": exact_duplicate_extra_row_count,
        "exact_duplicate_policy": (
            "Exact duplicate source rows are deterministically collapsed before property grouping; "
            "the first occurrence is retained and the source ledger is not mutated."
        ),
        "missing_property_row_count": missing_property_rows,
        "eco_company_revenue_exclusion_policy": (
            "ECO-owned dao_eco registration-fee and pm_eco management-fee revenue remains in the "
            "consolidated master ledger but is excluded from DAO property ledgers so it cannot "
            "cancel the DAO-side liability."
        ),
        "eco_company_revenue_excluded_row_count": len(eco_company_revenue_excluded_rows),
        "eco_company_revenue_excluded_amount_total": amount_total(eco_company_revenue_excluded_rows),
        "eco_company_revenue_excluded_property_count": len(
            {
                str(row.get("Property") or "").strip()
                for row in eco_company_revenue_excluded_rows
                if str(row.get("Property") or "").strip()
            }
        ),
        "grouped_property_count": len(grouped),
        "property_root_count": len(roots),
        "planned_write_count": planned_write_count,
        "planned_row_count": planned_row_count,
        "output_plan_digest": output_plan_digest(planned_outputs),
        "planned_outputs_bounded": planned_outputs[:25],
        "output_mismatches_bounded": [
            record for record in planned_outputs if record["output_status"] != "current"
        ][:25],
        "output_current_properties": output_current_properties,
        "planned_obsolete_alias_delete_count": planned_obsolete_alias_delete_count,
        "planned_obsolete_aliases_bounded": [
            alias
            for record in planned_outputs
            for alias in (record.get("obsolete_aliases") or [])
        ][:50],
        "skip_excluded_properties": skip_excluded_properties,
        "exclusion_report": exclusion_report,
        "exclusion_guard_count": len(exclusion_guards),
        "excluded_write_skipped_count": len(excluded_write_skipped_outputs),
        "excluded_write_skipped_row_count": excluded_write_skipped_row_count,
        "excluded_write_skipped_amount_total": excluded_write_skipped_amount_total,
        "excluded_write_skipped_properties_bounded": excluded_write_skipped_outputs[:25],
        "unresolved_property_count": unresolved_count,
        "unresolved_row_count": unresolved_row_count,
        "unresolved_amount_total": unresolved_amount_total,
        "unresolved_properties_bounded": unresolved_outputs[:25],
        "deferred_acquisition_property_count": len(deferred_acquisition_outputs),
        "deferred_acquisition_row_count": deferred_acquisition_row_count,
        "deferred_acquisition_amount_total": deferred_acquisition_amount_total,
        "deferred_acquisition_properties_bounded": deferred_acquisition_outputs[:25],
        "citadel_statement_split_applied_count": citadel_statement_split_applied_count,
        "citadel_statement_split_records_bounded": citadel_statement_split_records[:25],
        "would_create_target_dir_count": would_create_target_dir_count,
        "existing_target_dir_count": existing_target_dir_count,
        "output_current_count": output_current_count,
        "output_missing_count": output_missing_count,
        "output_stale_count": output_stale_count,
        "output_unreadable_count": output_unreadable_count,
        "output_mismatch_count": output_mismatch_count,
        "require_current_outputs": require_current_outputs,
        "would_write_property_csvs": planned_write_count > 0,
        "would_create_target_dirs": would_create_target_dir_count > 0,
        "source_read_attempted": source_path.is_file(),
        "write_attempted": write_attempted,
        "property_csv_write_attempted": write_attempted,
        "directory_create_attempted": False,
        "delete_attempted": delete_attempted,
        "deleted_obsolete_alias_count": deleted_obsolete_alias_count,
        "sync_attempted": False,
        "restart_attempted": False,
        "network_attempted": False,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Split a Baselane ledger into per-property public financial CSVs")
    parser.add_argument("--source", default=SOURCE, help="Source Baselane/ECO ledger CSV")
    parser.add_argument("--real-estate-base", default=REAL_ESTATE_BASE, help="Real Estate folder root")
    parser.add_argument("--json", action="store_true", help="Emit a no-action diagnostic report")
    parser.add_argument("--verify-existing", action="store_true", help="Require existing per-property CSVs to match the source ledger")
    parser.add_argument(
        "--include-excluded",
        action="store_true",
        help="Also write sold/delisted/closed/manual-excluded property CSVs",
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH, help="Write freshness report JSON")
    return parser.parse_args(argv)


def write_json(path: Path, report: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def main(argv=None):
    args = parse_args(argv)
    source = args.source
    real_estate_base = args.real_estate_base
    skip_excluded_properties = not args.include_excluded

    if args.json:
        report = build_report(
            source,
            real_estate_base,
            require_current_outputs=args.verify_existing,
            skip_excluded_properties=skip_excluded_properties,
        )
        write_json(args.report, report)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    if not os.path.isfile(source):
        raise FileNotFoundError(source)

    roots = build_property_roots(real_estate_base)

    fieldnames, grouped, _total_rows, _missing_property_rows, _exact_duplicate_extra_row_count = read_ledger_groups(source)
    grouped, eco_company_revenue_excluded_rows = exclude_eco_company_revenue_from_dao_groups(grouped)
    grouped = consolidate_property_alias_groups(grouped, roots, real_estate_base)

    written = []
    deleted_aliases = []
    unresolved = []
    deferred = []
    skipped_excluded = []
    citadel_statement_split_applied_count = 0
    exclusion_guards, _exclusion_report = split_exclusion_guards()

    for prop, rows in sorted(grouped.items(), key=lambda kv: kv[0].lower()):
        full, score, rel = best_match(prop, roots, real_estate_base)
        exclusion = split_exclusion_match(prop, full, rel, exclusion_guards) if skip_excluded_properties else None
        if exclusion:
            skipped_excluded.append((prop, score, rel, len(rows), exclusion.get("status"), exclusion.get("source")))
            continue
        if not full or score < 0.38:
            acquisition_record = deferred_acquisition_record(prop, rows, Path(real_estate_base))
            if acquisition_record:
                deferred.append((prop, score, rel, len(rows), acquisition_record["amount_total"]))
                continue
            unresolved.append((prop, score, rel))
            continue

        target_fin = choose_target_financials(full)
        rows, citadel_split_report = apply_citadel_statement_splits(prop, rows, full)
        citadel_statement_split_applied_count += int(citadel_split_report.get("applied_count") or 0)
        aliases = equivalent_output_ledger_paths(Path(target_fin), prop)
        out_path = canonical_output_ledger_path(
            Path(target_fin),
            prop,
            allow_divergent_replacement=True,
        )

        write_property_csv(str(out_path), fieldnames, rows)
        for alias in aliases:
            if alias == out_path:
                continue
            alias.unlink()
            deleted_aliases.append(str(alias))

        written.append((prop, str(out_path), score, rel, len(rows)))

    print(f"WROTE {len(written)} property CSVs")
    print(
        "EXCLUDED_ECO_COMPANY_REVENUE "
        f"{len(eco_company_revenue_excluded_rows)} "
        f"{amount_total(eco_company_revenue_excluded_rows):.2f}"
    )
    print(f"CITADEL_STATEMENT_SPLITS {citadel_statement_split_applied_count}")
    for prop, out_path, score, rel, count in written:
        print(f"OK\t{count}\t{score:.3f}\t{prop}\t->\t{out_path}")
    print(f"DELETED_OBSOLETE_ALIASES {len(deleted_aliases)}")
    for path in deleted_aliases:
        print(f"DELETE\t{path}")
    print(f"SKIPPED_EXCLUDED {len(skipped_excluded)}")
    for prop, score, rel, count, status, source_name in skipped_excluded:
        print(f"SKIP\t{count}\t{score:.3f}\t{prop}\t({status}/{source_name})\t(best={rel})")

    print(f"UNRESOLVED {len(unresolved)}")
    for prop, score, rel in unresolved:
        print(f"MISS\t{score:.3f}\t{prop}\t(best={rel})")
    print(f"DEFERRED_ACQUISITION {len(deferred)}")
    for prop, score, rel, count, total in deferred:
        print(f"ACQ\t{count}\t{total:.2f}\t{score:.3f}\t{prop}\t(best={rel})")

    report = build_report(
        source,
        real_estate_base,
        require_current_outputs=True,
        write_attempted=True,
        delete_attempted=bool(deleted_aliases),
        deleted_obsolete_alias_count=len(deleted_aliases),
        skip_excluded_properties=skip_excluded_properties,
    )
    write_json(args.report, report)
    print(f"REPORT\t{args.report}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
