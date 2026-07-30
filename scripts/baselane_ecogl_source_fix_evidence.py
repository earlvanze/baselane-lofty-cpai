#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def count(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "path": str(path), "error": str(exc)}
    return data if isinstance(data, dict) else {"status": "unreadable", "path": str(path), "error": "not a JSON object"}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def normalize(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).upper()


def amount_number(value: object) -> float | None:
    text = str(value or "").replace(",", "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def parsed_date(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw[:10] if fmt == "%Y-%m-%d" else raw, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def latest_export_csv(root: Path) -> Path | None:
    reports = root / "reports"
    candidates = sorted(reports.glob("baselane_export_filtered_preview.*.csv"))
    if not candidates:
        candidates = sorted(reports.glob("baselane_export_all_transactions.*.csv"))
    return candidates[-1] if candidates else None


def export_csvs(root: Path) -> list[Path]:
    reports = root / "reports"
    candidates = sorted(reports.glob("baselane_export_filtered_preview.*.csv"))
    if not candidates:
        candidates = sorted(reports.glob("baselane_export_all_transactions.*.csv"))
    for external_root in [
        Path("/home/digit/Dropbox/Real Estate/Lofty PM/reports"),
        Path("/mnt/c/Users/digit/Dropbox/Real Estate/Lofty PM/reports"),
    ]:
        if external_root.exists():
            candidates.extend(sorted(external_root.glob("baselane_export_fresh_*.csv")))
    deduped = []
    seen = set()
    for candidate in candidates:
        try:
            key = candidate.resolve(strict=False)
        except OSError:
            key = candidate
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def row_value(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = str(row.get(name) or "").strip()
        if value:
            return value
    return ""


def transaction_identity(row: dict[str, str]) -> tuple[str, str, str, str, str]:
    return (
        normalize(row_value(row, "Property", "property")),
        normalize(row_value(row, "Date", "date")),
        str(amount_number(row_value(row, "Amount", "amount"))),
        normalize(row_value(row, "Merchant", "merchant")),
        normalize(row_value(row, "Description", "description")),
    )


def dedupe_history_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    passthrough: list[dict[str, str]] = []
    for row in rows:
        identity = transaction_identity(row)
        if not any(identity):
            passthrough.append(row)
            continue
        category = category_value(row)
        note_text = row_value(row, "Notes", "notes")
        grouped.setdefault(identity, {"row": row, "categories": {}, "notes": {}})
        if category:
            grouped[identity]["categories"][category] = row
        if note_text:
            grouped[identity]["notes"][note_text] = row
    deduped: list[dict[str, str]] = []
    for group in grouped.values():
        categories = group["categories"]
        notes = group["notes"]
        if categories:
            for category, source_row in categories.items():
                row_copy = dict(source_row)
                row_copy["Category"] = category
                deduped.append(row_copy)
        elif notes:
            for note_text, source_row in notes.items():
                row_copy = dict(source_row)
                row_copy["Notes"] = note_text
                deduped.append(row_copy)
        else:
            deduped.append(group["row"])
    return passthrough + deduped


def read_history_rows(paths: list[Path]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in paths:
        rows.extend(read_csv(path))
    return dedupe_history_rows(rows)


def category_value(row: dict[str, str]) -> str:
    return canonical_category(
        str(
        row.get("Category")
        or row.get("Baselane Category")
        or row.get("baselane_category")
        or ""
        ).strip()
    )


BASELANE_CATEGORY_ALIASES = {
    "Cleaning & Janitorial": "Cleaning & Maintenance",
    "Gardening & Landscaping": "Cleaning & Maintenance",
    "Landscaping": "Cleaning & Maintenance",
    "Remodeling": "Repairs",
    "Water & Sewer": "Utilities",
}


def canonical_category(category: object) -> str:
    category_text = str(category or "").strip()
    return BASELANE_CATEGORY_ALIASES.get(category_text, category_text)


def category_is_usable(value: str) -> bool:
    normalized = normalize(value)
    return bool(value.strip()) and normalized not in {"UNCATEGORIZED", "UNCATEGORIZED EXPENSE"}


NOTE_CATEGORY_RULES = [
    ("Cleaning & Maintenance", ("cleaning", "cleanings", "cleanup", "clean up", "snow removal")),
    ("Repairs", ("repair", "repairs", "handyman", "materials and labor", "tree removal")),
    ("Insurance", ("insurance premium", "insurance")),
    ("Taxes", ("tax payment", "taxes")),
    ("Utilities", ("verizon", "fios", "utility", "utilities")),
    ("Mortgage Interest Payments", ("mortgage interest",)),
    ("Other Loan Payments", ("solar loan", "loan payment")),
]


def category_from_notes(notes: object) -> str:
    normalized_notes = str(notes or "").strip().lower()
    if not normalized_notes:
        return ""
    for category, patterns in NOTE_CATEGORY_RULES:
        if any(pattern in normalized_notes for pattern in patterns):
            return category
    return ""


EMAIL_RECEIPT_CATEGORY_RULES = [
    ("Landscaping", ("lawn", "landscape", "landscaping", "mow", "mowing", "yard")),
    ("Cleaning & Maintenance", ("cleaning", "clean", "cleanup", "janitorial")),
    ("Repairs", ("repair", "handyman", "fix", "plumbing", "electrical", "hvac")),
    ("Snow Removal", ("snow", "shoveling", "plowing")),
    ("Utilities", ("utility", "utilities", "electric", "water", "gas", "trash")),
    ("Pest", ("pest", "exterminator", "bug")),
    ("Pool & Spa", ("pool", "spa")),
]


def email_receipt_category_evidence(row: dict[str, Any]) -> dict[str, Any]:
    """Derive a deterministic category suggestion from matched email receipt snippets.

    Only triggers for person-payment rows where Gmail/local mail returned receipt
    snippets.  A match requires:
      - subject/snippet contains a payment-confirmation marker ("payment sent",
        "paid", "complete", "completed")
      - snippet mentions the property address number
      - snippet mentions the transaction amount
      - a service keyword from EMAIL_RECEIPT_CATEGORY_RULES is present

    A request-only email (e.g. "Request received") does NOT qualify because it
    does not prove the expense was actually paid.  This function never mutates
    Baselane; it only returns a category suggestion that downstream approval
    can use as deterministic evidence.
    """
    gws = row.get("gws_mail_invoice_evidence") if isinstance(row.get("gws_mail_invoice_evidence"), dict) else {}
    local_mail = row.get("local_mail_invoice_evidence") if isinstance(row.get("local_mail_invoice_evidence"), dict) else {}
    matches = []
    if isinstance(gws.get("matches"), list):
        matches.extend(gws.get("matches") or [])
    if isinstance(local_mail.get("matches"), list):
        matches.extend(local_mail.get("matches") or [])
    if not matches:
        return {"status": "no_match", "category": "", "reason": "", "match_count": 0}

    property_text = normalized_path_text(row.get("property") or "")
    property_number = ""
    for token in property_text.split():
        if token.isdigit():
            property_number = token
            break
    amount_tokens = [str(t).lower().replace("$", "").replace(",", "") for t in amount_invoice_terms(row.get("amount"))]
    amount_tokens = [t for t in amount_tokens if t]

    paid_markers = ("payment sent", "paid", "complete", "completed", "successfully")
    request_markers = ("request received", "request to pay", "pay or decline")

    candidate_category = ""
    candidate_reason = ""
    paid_confirmed = False
    for match in matches:
        subject = str(match.get("subject") or "").lower()
        snippet = str(match.get("snippet") or "").lower()
        haystack = f"{subject} {snippet}"
        is_request = any(marker in haystack for marker in request_markers)
        is_paid = any(marker in haystack for marker in paid_markers)
        if is_request and not is_paid:
            continue
        if not is_paid:
            continue
        paid_confirmed = True
        if property_number and property_number not in haystack:
            continue
        amount_found = False
        for token in amount_tokens:
            if token in haystack:
                amount_found = True
                break
        if not amount_found:
            continue
        for category, keywords in EMAIL_RECEIPT_CATEGORY_RULES:
            matched_keyword = next((kw for kw in keywords if kw in haystack), None)
            if matched_keyword:
                candidate_category = category
                candidate_reason = (
                    f"Email receipt '{match.get('subject') or 'snippet'}' confirms payment "
                    f"of {row.get('amount')} for {row.get('property')} with service keyword "
                    f"'{matched_keyword}' matching {category}."
                )
                break
        if candidate_category:
            break

    if not candidate_category:
        if paid_confirmed:
            return {
                "status": "paid_no_category_keyword",
                "category": "",
                "reason": "Email confirms payment but no service keyword maps to a Baselane category; manual review required.",
                "match_count": len(matches),
            }
        return {
            "status": "request_only_or_no_paid_marker",
            "category": "",
            "reason": "Email matches are request-only or lack a payment-confirmation marker; do not infer category.",
            "match_count": len(matches),
        }
    return {
        "status": "automation_safe_email_receipt",
        "category": candidate_category,
        "reason": candidate_reason,
        "match_count": len(matches),
    }


def normalized_path_text(value: object) -> str:
    text = str(value or "").lower()
    replacements = {
        "circle": "cir",
        "street": "st",
        "avenue": "ave",
        "lane": "ln",
        "north": "n",
    }
    for source, target in replacements.items():
        text = re.sub(rf"\b{source}\b", target, text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def property_match_tokens(property_name: object) -> list[str]:
    normalized = normalized_path_text(property_name)
    tokens = [token for token in normalized.split() if token not in {"s", "n", "e", "w", "public"}]
    if not tokens:
        return []
    numeric = [token for token in tokens if token.isdigit()]
    words = [token for token in tokens if not token.isdigit() and len(token) > 2]
    selected = []
    if numeric:
        selected.append(numeric[0])
    selected.extend(words[:2])
    return selected


def merchant_identifier_tokens(merchant: object) -> list[str]:
    return sorted(set(re.findall(r"\b\d{3,8}\b", str(merchant or ""))))


def merchant_document_tokens(merchant: object) -> list[str]:
    stopwords = {
        "app",
        "cash",
        "cashapp",
        "venmo",
        "transfer",
        "transfers",
        "debit",
        "credit",
        "llc",
        "inc",
        "co",
        "company",
        "the",
        "and",
        "new",
        "york",
        "us",
        "usa",
    }
    tokens = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", str(merchant or "")):
        normalized = token.lower()
        if normalized in stopwords:
            continue
        tokens.append(normalized)
    return sorted(set(tokens))


PERSON_PAYMENT_RAIL_PATTERNS = [
    ("Venmo", re.compile(r"\bVENMO\b", re.IGNORECASE)),
    ("Cash App", re.compile(r"\bCASH\s*APP\b|\bCASHAPP\b", re.IGNORECASE)),
    ("Zelle", re.compile(r"\bZELLE\b", re.IGNORECASE)),
    ("PayPal", re.compile(r"\bPAYPAL\b|\bPAY\s*PAL\b", re.IGNORECASE)),
]


PAYEE_STOPWORDS = {
    "app",
    "cash",
    "cashapp",
    "venmo",
    "zelle",
    "paypal",
    "pay",
    "pal",
    "new",
    "york",
    "ny",
    "us",
    "usa",
    "payment",
    "transfer",
    "ave",
    "avenue",
    "st",
    "street",
    "ln",
    "lane",
    "dr",
    "drive",
    "cir",
    "circle",
    "pl",
    "place",
}


def payment_rail(text: object) -> str:
    haystack = str(text or "")
    for rail, pattern in PERSON_PAYMENT_RAIL_PATTERNS:
        if pattern.search(haystack):
            return rail
    return ""


def is_person_payment_row(row: dict[str, str]) -> bool:
    return bool(payment_rail(f"{row.get('merchant') or ''} {row.get('description') or ''}"))


def payee_tokens(merchant: object) -> list[str]:
    text = str(merchant or "")
    text = re.split(r"\s+\|\s+|#", text, maxsplit=1)[0]
    text = re.sub(r"\bCASH\s*APP\b|\bCASHAPP\b|\bVENMO\b|\bZELLE\b|\bPAYPAL\b|\bPAY\s*PAL\b", " ", text, flags=re.IGNORECASE)
    tokens = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9]{1,}", text):
        normalized = token.lower()
        if normalized in PAYEE_STOPWORDS:
            continue
        tokens.append(token.upper() if len(token) <= 3 else token.title())
    return sorted(set(tokens), key=lambda value: text.upper().find(value.upper()) if value.upper() in text.upper() else 999)


def quoted_or_terms(terms: list[str]) -> str:
    cleaned = []
    seen = set()
    for term in terms:
        normalized = re.sub(r"\s+", " ", str(term or "").strip())
        if not normalized:
            continue
        key = normalized.upper()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(f'"{normalized}"')
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    return "(" + " OR ".join(cleaned) + ")"


def parse_row_date(value: object) -> datetime | None:
    text = str(value or "").strip()
    for pattern in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def next_month_start(dt: datetime) -> datetime:
    if dt.month == 12:
        return datetime(dt.year + 1, 1, 1)
    return datetime(dt.year, dt.month + 1, 1)


def email_invoice_window(date_text: object) -> dict[str, str]:
    dt = parse_row_date(date_text)
    if not dt:
        return {"after": "", "before": "", "basis": "unknown_transaction_date"}
    after = datetime(dt.year, dt.month, 1)
    next_month = next_month_start(dt)
    before_day = min(16, 28)
    before = datetime(next_month.year, next_month.month, before_day)
    return {
        "after": after.strftime("%Y/%m/%d"),
        "before": before.strftime("%Y/%m/%d"),
        "basis": "transaction_month_through_mid_next_month",
    }


def property_invoice_terms(property_name: object) -> list[str]:
    tokens = property_match_tokens(property_name)
    if not tokens:
        return []
    terms: list[str] = []
    numeric = [token for token in tokens if token.isdigit()]
    words = [token for token in tokens if not token.isdigit()]
    if numeric and words:
        terms.append(" ".join([numeric[0], *words[:2]]))
    terms.extend(words[:2])
    if numeric:
        terms.append(numeric[0])
    return terms


def amount_invoice_terms(amount: object) -> list[str]:
    number = amount_number(amount)
    if number is None:
        return []
    absolute = abs(number)
    terms = [f"{absolute:.2f}", f"{absolute:g}"]
    if absolute >= 1000:
        terms.extend([f"{absolute:,.2f}", f"{absolute:,.0f}"])
    return [f"${term}" for term in terms] + terms


def invoice_search_terms(row: dict[str, str]) -> dict[str, list[str]]:
    payee = payee_tokens(row.get("merchant"))
    payee_phrase = " ".join(payee[:3])
    payee_terms = ([payee_phrase] if len(payee) > 1 else []) + payee
    return {
        "payee": payee_terms,
        "property": property_invoice_terms(row.get("property")),
        "amount": amount_invoice_terms(row.get("amount")),
    }


def email_invoice_evidence(row: dict[str, str]) -> dict[str, Any]:
    rail = payment_rail(f"{row.get('merchant') or ''} {row.get('description') or ''}")
    if not rail:
        return {
            "required": False,
            "status": "not_person_payment",
            "payment_rail": "",
            "payee_tokens": [],
            "search_query": "",
            "search_terms": {},
            "expected_window": {},
            "reason": "",
        }
    terms = invoice_search_terms(row)
    window = email_invoice_window(row.get("date"))
    query_parts = [
        quoted_or_terms(terms["payee"]),
        quoted_or_terms(terms["property"]),
        quoted_or_terms(terms["amount"][:4]),
    ]
    if window.get("after"):
        query_parts.append(f"after:{window['after']}")
    if window.get("before"):
        query_parts.append(f"before:{window['before']}")
    query = " ".join(part for part in query_parts if part)
    return {
        "required": True,
        "status": "query_ready" if query else "needs_manual_email_search",
        "payment_rail": rail,
        "payee_tokens": payee_tokens(row.get("merchant")),
        "search_query": query,
        "search_terms": terms,
        "expected_window": window,
        "reason": "Person-payment rails usually map to invoice/receipt evidence in email; do not infer category from payment merchant alone.",
    }


def local_mail_roots(root: Path) -> list[Path]:
    candidates = [
        root / "pdf-extracts",
        root / "mailbox",
    ]
    workspace_root = Path(__file__).absolute().parents[1]
    if os.path.abspath(root) == os.path.abspath(workspace_root):
        candidates.extend(
            [
                Path("/home/digit/.openclaw/workspace/pdf-extracts"),
                Path("/home/digit/.openclaw/workspace/mailbox"),
            ]
        )
    result = []
    seen = set()
    for candidate in candidates:
        if not candidate.exists():
            continue
        key = os.path.abspath(candidate)
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def normalize_search_text(value: object) -> str:
    return normalized_path_text(value)


def term_matches(text: str, terms: list[str]) -> list[str]:
    matches = []
    padded = f" {text} "
    for term in terms:
        normalized = normalize_search_text(term)
        if not normalized:
            continue
        if f" {normalized} " in padded or normalized in text:
            matches.append(str(term))
    return matches


def read_text_sample(path: Path, max_bytes: int = 200_000) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt", ".eml", ".html", ".htm"}:
        try:
            return path.read_text(encoding="utf-8", errors="ignore")[:max_bytes]
        except OSError:
            return ""
    if suffix == ".pdf" and os.environ.get("BASELANE_SOURCE_FIX_ENABLE_MAIL_PDF_TEXT_SCAN") == "1":
        try:
            result = subprocess.run(
                ["pdftotext", "-layout", "-nopgbrk", str(path), "-"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:  # noqa: BLE001
            return ""
        if result.returncode == 0:
            return result.stdout[:max_bytes]
    return ""


def iter_mail_candidate_files(root: Path, max_files: int):
    checked = 0
    allowed_suffixes = {".md", ".txt", ".eml", ".html", ".htm", ".pdf"}
    for mail_root in local_mail_roots(root):
        for path, checked_in_root in iter_files_limited(mail_root, max_files=max_files, max_depth=8):
            if max_files and checked >= max_files:
                return
            if path.suffix.lower() not in allowed_suffixes:
                continue
            checked += 1
            yield path, checked


def local_mail_invoice_evidence(row: dict[str, str], root: Path) -> dict[str, Any]:
    email_context = email_invoice_evidence(row)
    if email_context.get("required") is not True:
        return {
            "status": "not_required",
            "checked_file_count": 0,
            "match_count": 0,
            "matches": [],
            "limit_reached": False,
        }
    if os.environ.get("BASELANE_SOURCE_FIX_ENABLE_LOCAL_MAIL_SEARCH", "1") != "1":
        return {
            "status": "disabled",
            "checked_file_count": 0,
            "match_count": 0,
            "matches": [],
            "limit_reached": False,
        }
    terms = email_context.get("search_terms") if isinstance(email_context.get("search_terms"), dict) else {}
    payee_terms = [str(term) for term in terms.get("payee") or []]
    property_terms = [str(term) for term in terms.get("property") or []]
    amount_terms = [str(term) for term in terms.get("amount") or []]
    max_files = max(0, count(os.environ.get("BASELANE_SOURCE_FIX_MAIL_MAX_FILES") or 1200))
    checked = 0
    matches: list[dict[str, Any]] = []
    for path, checked in iter_mail_candidate_files(root, max_files=max_files):
        path_text = normalize_search_text(path)
        body_text = normalize_search_text(read_text_sample(path))
        haystack = f"{path_text} {body_text}".strip()
        if not haystack:
            continue
        payee_matches = term_matches(haystack, payee_terms)
        property_matches = term_matches(haystack, property_terms)
        amount_matches = term_matches(haystack, amount_terms)
        strict_property_match = bool(property_terms and property_terms[0] in property_matches)
        if not (payee_matches and amount_matches and strict_property_match):
            continue
        score = int(bool(payee_matches)) + int(strict_property_match) + int(bool(amount_matches))
        matches.append(
            {
                "path": str(path),
                "score": score,
                "matched_payee_terms": payee_matches[:5],
                "matched_property_terms": property_matches[:5],
                "matched_amount_terms": amount_matches[:5],
            }
        )
        if len(matches) >= 5:
            break
    limit_reached = bool(max_files and checked >= max_files and len(matches) < 5)
    return {
        "status": "matched" if matches else "no_match",
        "checked_file_count": checked,
        "match_count": len(matches),
        "matches": matches,
        "limit_reached": limit_reached,
        "mail_roots": [str(path) for path in local_mail_roots(root)],
    }


def account_safe(account: str) -> str:
    return account.replace("@", "-").replace(".", "-")


def gws_accounts() -> list[str]:
    value = os.environ.get("BASELANE_SOURCE_FIX_GWS_ACCOUNTS", "ecosystemspm@gmail.com,earlvanze@gmail.com")
    return [account.strip() for account in value.split(",") if account.strip()]


def gws_env_for_account(account: str) -> dict[str, str]:
    env = os.environ.copy()
    config_base = os.environ.get("GWS_CONFIG_BASE", "/home/digit/.openclaw/gws")
    env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = os.environ.get(
        f"GWS_CONFIG_DIR_{account_safe(account).upper().replace('-', '_')}",
        str(Path(config_base) / account_safe(account)),
    )
    env["GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"] = os.environ.get("GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND", "file")
    env.pop("GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE", None)
    return env


def gws_binary() -> str:
    return os.environ.get("GWS_BIN") or "/home/digit/.local/bin/gws"


def gws_parse_json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {"error": {"message": "unparseable gws JSON"}}
    return data if isinstance(data, dict) else {}


def header_value(message: dict[str, Any], name: str) -> str:
    for header in (((message.get("payload") or {}).get("headers")) or []):
        if str(header.get("name") or "").lower() == name.lower():
            return str(header.get("value") or "")
    return ""


def gws_run_json(args: list[str], account: str, timeout: int = 30) -> tuple[int, dict[str, Any], str]:
    try:
        result = subprocess.run(
            args,
            env=gws_env_for_account(account),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return 5, {}, str(exc)[:300]
    data = gws_parse_json(result.stdout)
    error_text = ""
    if result.returncode != 0:
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        error_text = str(error.get("message") or result.stderr or result.stdout or "")[:300]
    return result.returncode, data, error_text


def gws_invoice_queries(email_context: dict[str, Any]) -> list[str]:
    terms = email_context.get("search_terms") if isinstance(email_context.get("search_terms"), dict) else {}
    payee_terms = [str(term) for term in terms.get("payee") or [] if str(term).strip()]
    amount_terms = [str(term) for term in terms.get("amount") or [] if str(term).strip()]
    window = email_context.get("expected_window") if isinstance(email_context.get("expected_window"), dict) else {}
    rail = str(email_context.get("payment_rail") or "")
    sender = "from:cash@square.com " if rail == "Cash App" else ""
    after = f" after:{window['after']}" if window.get("after") else ""
    before = f" before:{window['before']}" if window.get("before") else ""
    selected_amounts = amount_terms[:2] or amount_terms[:1]
    if not payee_terms or not selected_amounts:
        return []
    queries = []
    for payee in payee_terms[:3]:
        for amount in selected_amounts:
            queries.append(f'{sender}{payee} "{amount}"{after}{before}'.strip())
    deduped = []
    seen = set()
    for query in queries:
        if query in seen:
            continue
        seen.add(query)
        deduped.append(query)
    return deduped[:4]


def gws_mail_invoice_evidence(row: dict[str, str]) -> dict[str, Any]:
    email_context = email_invoice_evidence(row)
    if email_context.get("required") is not True:
        return {"status": "not_required", "match_count": 0, "matches": [], "errors": []}
    if os.environ.get("BASELANE_SOURCE_FIX_ENABLE_GWS_MAIL_SEARCH", "1") != "1":
        return {"status": "disabled", "match_count": 0, "matches": [], "errors": []}
    binary = gws_binary()
    if not Path(binary).exists() and not binary.startswith("/"):
        binary = "gws"
    queries = gws_invoice_queries(email_context)
    max_results = max(1, count(os.environ.get("BASELANE_SOURCE_FIX_GWS_MAX_RESULTS") or 5))
    matches: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for account in gws_accounts():
        for query in queries:
            rc, data, error_text = gws_run_json(
                [
                    binary,
                    "gmail",
                    "users",
                    "messages",
                    "list",
                    "--params",
                    json.dumps({"userId": "me", "maxResults": max_results, "q": query}),
                    "--format",
                    "json",
                ],
                account=account,
            )
            if rc != 0:
                errors.append({"account": account, "query": query, "error": error_text})
                break
            messages = data.get("messages") if isinstance(data.get("messages"), list) else []
            for message in messages[:max_results]:
                message_id = str(message.get("id") or "")
                if not message_id:
                    continue
                key = f"{account}:{message_id}"
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                detail_rc, detail, detail_error = gws_run_json(
                    [
                        binary,
                        "gmail",
                        "users",
                        "messages",
                        "get",
                        "--params",
                        json.dumps(
                            {
                                "userId": "me",
                                "id": message_id,
                                "format": "metadata",
                                "metadataHeaders": ["From", "Subject", "Date"],
                            }
                        ),
                        "--format",
                        "json",
                    ],
                    account=account,
                )
                if detail_rc != 0:
                    errors.append({"account": account, "query": query, "error": detail_error})
                    continue
                matches.append(
                    {
                        "account": account,
                        "message_id": message_id,
                        "thread_id": str(detail.get("threadId") or message.get("threadId") or ""),
                        "date": header_value(detail, "Date"),
                        "from": header_value(detail, "From"),
                        "subject": header_value(detail, "Subject"),
                        "labels": detail.get("labelIds") or [],
                        "snippet": str(detail.get("snippet") or "")[:500],
                        "query": query,
                    }
                )
            if matches:
                break
    return {
        "status": "matched" if matches else ("error" if errors and not matches else "no_match"),
        "match_count": len(matches),
        "matches": matches[:10],
        "errors": errors[:5],
        "queries": queries,
        "accounts": gws_accounts(),
    }


def candidate_real_estate_roots(root: Path) -> list[Path]:
    explicit_root = os.environ.get("BASELANE_SOURCE_FIX_REAL_ESTATE_ROOT")
    if explicit_root:
        candidate = Path(explicit_root)
        return [candidate] if candidate.exists() else []
    roots = [root / "Dropbox" / "Real Estate"]
    workspace_root = Path(__file__).absolute().parents[1]
    if os.path.abspath(root) == os.path.abspath(workspace_root):
        roots.extend(
            [
                Path("/mnt/c/Users/digit/Dropbox/Real Estate"),
                Path("/home/digit/Dropbox/Real Estate"),
            ]
        )
    result = []
    seen = set()
    for candidate in roots:
        if not candidate.exists():
            continue
        key = os.path.abspath(candidate)
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    if os.environ.get("BASELANE_SOURCE_FIX_DOCUMENT_SCAN_ALL_ROOTS") != "1":
        return result[:1]
    return result


def iter_dirs_limited(root: Path, max_depth: int = 3):
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        yield current
        if depth >= max_depth:
            continue
        try:
            with os.scandir(current) as entries:
                children = [Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        stack.extend((child, depth + 1) for child in reversed(children))


def property_document_roots(root: Path, property_name: object) -> list[Path]:
    cache_key = (str(root.resolve(strict=False)), str(property_name or ""))
    cached = getattr(property_document_roots, "_cache", {}).get(cache_key)
    if cached is not None:
        return list(cached)
    tokens = property_match_tokens(property_name)
    if not tokens:
        return []
    numeric_tokens = [token for token in tokens if token.isdigit()]
    if not numeric_tokens:
        return []
    runtime_matches = runtime_map_property_roots(root, property_name)
    if runtime_matches:
        cache = getattr(property_document_roots, "_cache", {})
        cache[cache_key] = tuple(runtime_matches)
        setattr(property_document_roots, "_cache", cache)
        return runtime_matches
    matches: list[Path] = []
    for base in candidate_real_estate_roots(root):
        candidates: list[Path] = []
        numeric_token = numeric_tokens[0]
        for path in iter_dirs_limited(base, max_depth=3):
            try:
                relative_text = str(path.relative_to(base))
            except ValueError:
                relative_text = str(path)
            if numeric_token in relative_text:
                candidates.append(path)
        for candidate in candidates:
            normalized = normalized_path_text(candidate.relative_to(base))
            if all(token in normalized.split() for token in tokens):
                matches.append(candidate)
    result = sorted(matches, key=lambda path: (len(str(path)), str(path)))[:5]
    cache = getattr(property_document_roots, "_cache", {})
    cache[cache_key] = tuple(result)
    setattr(property_document_roots, "_cache", cache)
    return result


def public_root_from_artifact_path(path_text: object) -> Path | None:
    path = Path(str(path_text or ""))
    parts = path.parts
    for index, part in enumerate(parts):
        if part == "Public":
            return Path(*parts[: index + 1])
    return None


def runtime_map_property_roots(root: Path, property_name: object) -> list[Path]:
    target_tokens = set(property_match_tokens(property_name))
    if not target_tokens:
        return []
    reports = root / "reports"
    map_paths = [
        reports / "baselane_financials_monthly_lofty_pm_runtime_map.json",
        reports / "lofty-pm-runtime-map.json",
        reports / "baselane_financials_monthly_review_manifest.json",
    ]
    matches: list[Path] = []
    seen: set[str] = set()
    for map_path in map_paths:
        data = read_json(map_path)
        records = []
        for key in ("properties", "records"):
            value = data.get(key)
            if isinstance(value, list):
                records.extend(item for item in value if isinstance(item, dict))
        for record in records:
            haystack = " ".join(
                str(record.get(key) or "")
                for key in ("property_name", "full_address", "property", "updates_md", "financials_md", "draft_path")
            )
            normalized = set(normalized_path_text(haystack).split())
            if not target_tokens.issubset(normalized):
                continue
            for key in ("updates_md", "financials_md", "draft_path", "financial_approval_target", "update_approval_target"):
                public_root = public_root_from_artifact_path(record.get(key))
                if not public_root or not public_root.exists():
                    continue
                path_key = os.path.abspath(public_root)
                if path_key in seen:
                    continue
                seen.add(path_key)
                matches.append(public_root)
    return sorted(matches, key=lambda path: (len(str(path)), str(path)))[:5]


def empty_document_category_evidence() -> dict[str, Any]:
    return {
        "property_document_roots": [],
        "identifier_tokens": [],
        "merchant_tokens": [],
        "category_counts": {},
        "support_count": 0,
        "checked_file_count": 0,
        "limit_reached": False,
        "examples": [],
    }


def iter_files_limited(root: Path, max_files: int, max_depth: int = 5):
    checked = 0
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        current_text = normalized_path_text(current.name)
        if any(token in current_text.split() for token in {"photo", "photos", "media", "video", "videos"}):
            continue
        try:
            with os.scandir(current) as entries:
                dirs = []
                for entry in entries:
                    if max_files and checked >= max_files:
                        return
                    try:
                        if entry.is_file(follow_symlinks=False):
                            checked += 1
                            yield Path(entry.path), checked
                        elif entry.is_dir(follow_symlinks=False) and depth < max_depth:
                            entry_text = normalized_path_text(entry.name)
                            if entry.name in {".dropbox.cache", ".git", "node_modules", "__pycache__"}:
                                continue
                            if any(token in entry_text.split() for token in {"photo", "photos", "media", "video", "videos"}):
                                continue
                            dirs.append(Path(entry.path))
                    except OSError:
                        continue
                stack.extend((path, depth + 1) for path in reversed(dirs))
        except OSError:
            continue


def iter_matching_files_limited(root: Path, match_tokens: list[str], max_files: int):
    token_set = {normalized_path_text(token) for token in match_tokens if token}
    for path, checked in iter_files_limited(root, max_files=max_files):
        path_tokens = set(normalized_path_text(path.name).split())
        if token_set.intersection(path_tokens):
            yield path, checked


def category_from_document_path(path: Path) -> str:
    normalized = normalized_path_text(path.name)
    if "water district" in normalized or "water bill" in normalized or "utility bill" in normalized:
        return "Utilities"
    if "cleaning" in normalized or "cleanup" in normalized:
        return "Cleaning & Maintenance"
    if "repair" in normalized or "handyman" in normalized:
        return "Repairs"
    return ""


def document_category_evidence(target: dict[str, str], root: Path) -> dict[str, Any]:
    identifier_tokens = merchant_identifier_tokens(target.get("merchant"))
    merchant_tokens = (
        merchant_document_tokens(target.get("merchant"))
        if os.environ.get("BASELANE_SOURCE_FIX_ENABLE_MERCHANT_DOC_SCAN") == "1"
        else []
    )
    match_tokens = identifier_tokens or merchant_tokens
    category_counts: Counter[str] = Counter()
    examples: list[dict[str, str]] = []
    max_roots = max(0, count(os.environ.get("BASELANE_SOURCE_FIX_DOCUMENT_MAX_ROOTS") or 3))
    max_files_per_root = max(0, count(os.environ.get("BASELANE_SOURCE_FIX_DOCUMENT_MAX_FILES_PER_ROOT") or 150))
    max_files_total = max(0, count(os.environ.get("BASELANE_SOURCE_FIX_DOCUMENT_MAX_FILES_TOTAL") or 300))
    checked_total = 0
    limit_reached = False
    if not match_tokens:
        return {
            "property_document_roots": [],
            "identifier_tokens": [],
            "merchant_tokens": [],
            "category_counts": {},
            "support_count": 0,
            "checked_file_count": 0,
            "limit_reached": False,
            "examples": [],
        }
    roots = property_document_roots(root, target.get("property"))[:max_roots]
    seen_files: set[str] = set()
    for property_root in roots:
        checked_count = 0
        try:
            for path, checked_count in iter_matching_files_limited(property_root, match_tokens, max_files_per_root):
                if max_files_per_root and checked_count >= max_files_per_root:
                    limit_reached = True
                if max_files_total and checked_total >= max_files_total:
                    limit_reached = True
                    break
                checked_total += 1
                path_key = os.path.abspath(path)
                if path_key in seen_files:
                    continue
                seen_files.add(path_key)
                category = category_from_document_path(path)
                if not category:
                    continue
                normalized = normalized_path_text(path.name)
                if match_tokens and not any(token in normalized.split() for token in match_tokens):
                    continue
                category_counts[category] += 1
                if len(examples) < 5:
                    examples.append(
                        {
                            "category": category,
                            "path": str(path),
                            "matched_identifier_tokens": ",".join(identifier_tokens),
                            "matched_merchant_tokens": ",".join(merchant_tokens),
                        }
                    )
        except OSError:
            continue
        if limit_reached and max_files_total and checked_total >= max_files_total:
            break
    return {
        "property_document_roots": [str(path) for path in roots],
        "identifier_tokens": identifier_tokens,
        "merchant_tokens": merchant_tokens,
        "category_counts": dict(sorted(category_counts.items())),
        "support_count": sum(category_counts.values()),
        "checked_file_count": checked_total,
        "limit_reached": limit_reached,
        "examples": examples,
    }


def contextual_category_evidence(target: dict[str, str], history_rows: list[dict[str, str]]) -> dict[str, Any]:
    target_property = normalize(target.get("property"))
    target_merchant = normalize(target.get("merchant"))
    target_amount = amount_number(target.get("amount"))
    category_counts: Counter[str] = Counter()
    exact_amount_category_counts: Counter[str] = Counter()
    note_category_counts: Counter[str] = Counter()
    exact_amount_note_category_counts: Counter[str] = Counter()
    uncategorized_count = 0
    exact_amount_uncategorized_count = 0
    note_examples: list[dict[str, str]] = []
    categorized_examples: list[dict[str, str]] = []
    for row in history_rows:
        if normalize(row.get("Property") or row.get("property")) != target_property:
            continue
        merchant = normalize(row.get("Merchant") or row.get("merchant"))
        if merchant != target_merchant:
            continue
        row_amount = amount_number(row.get("Amount") or row.get("amount"))
        same_amount = target_amount is not None and row_amount == target_amount
        category = category_value(row)
        note_category = category_from_notes(row.get("Notes") or row.get("notes"))
        note_text = str(row.get("Notes") or row.get("notes") or "").strip()
        if category_is_usable(category):
            category_counts[category] += 1
            if same_amount:
                exact_amount_category_counts[category] += 1
            if len(categorized_examples) < 5:
                categorized_examples.append(
                    {
                        "date": str(row.get("Date") or row.get("date") or ""),
                        "amount": str(row.get("Amount") or row.get("amount") or ""),
                        "category": category,
                        "notes": note_text,
                    }
                )
        else:
            uncategorized_count += 1
            if same_amount:
                exact_amount_uncategorized_count += 1
        if note_category:
            note_category_counts[note_category] += 1
            if same_amount:
                exact_amount_note_category_counts[note_category] += 1
            if len(note_examples) < 5:
                note_examples.append(
                    {
                        "date": str(row.get("Date") or row.get("date") or ""),
                        "amount": str(row.get("Amount") or row.get("amount") or ""),
                        "note_category": note_category,
                        "notes": note_text,
                    }
                )
    return {
        "same_property_merchant_category_counts": dict(sorted(category_counts.items())),
        "same_property_merchant_exact_amount_category_counts": dict(sorted(exact_amount_category_counts.items())),
        "same_property_merchant_uncategorized_count": uncategorized_count,
        "same_property_merchant_exact_amount_uncategorized_count": exact_amount_uncategorized_count,
        "note_inferred_category_counts": dict(sorted(note_category_counts.items())),
        "exact_amount_note_inferred_category_counts": dict(sorted(exact_amount_note_category_counts.items())),
        "categorized_examples": categorized_examples,
        "note_examples": note_examples,
    }


def historical_category_evidence(target: dict[str, str], history_rows: list[dict[str, str]]) -> dict[str, Any]:
    target_property = normalize(target.get("property"))
    target_merchant = normalize(target.get("merchant"))
    target_amount = amount_number(target.get("amount"))
    category_counts: Counter[str] = Counter()
    examples: list[dict[str, str]] = []
    for row in history_rows:
        if normalize(row.get("Property") or row.get("property")) != target_property:
            continue
        merchant = normalize(row.get("Merchant") or row.get("merchant"))
        if merchant != target_merchant:
            continue
        row_amount = amount_number(row.get("Amount") or row.get("amount"))
        if target_amount is not None and row_amount != target_amount:
            continue
        category = category_value(row)
        if not category_is_usable(category):
            continue
        category_counts[category] += 1
        if len(examples) < 5:
            examples.append(
                {
                    "date": str(row.get("Date") or row.get("date") or ""),
                    "amount": str(row.get("Amount") or row.get("amount") or ""),
                    "category": category,
                    "type": str(row.get("Type") or row.get("type") or ""),
                    "notes": str(row.get("Notes") or row.get("notes") or ""),
                }
            )
    if not category_counts:
        return {
            "status": "no_support",
            "suggested_category": "",
            "support_count": 0,
            "conflict_count": 0,
            "category_counts": {},
            "examples": [],
            "automation_safe": False,
            "reason": "No categorized same-property/same-merchant/same-amount source rows found in the latest Baselane export.",
        }
    if len(category_counts) > 1:
        return {
            "status": "conflicting_support",
            "suggested_category": "",
            "support_count": sum(category_counts.values()),
            "conflict_count": len(category_counts),
            "category_counts": dict(sorted(category_counts.items())),
            "examples": examples,
            "automation_safe": False,
            "reason": "Same-property/same-merchant/same-amount rows have conflicting categories.",
        }
    category, support_count = category_counts.most_common(1)[0]
    strong = support_count >= 2
    return {
        "status": "strong_support" if strong else "weak_single_support",
        "suggested_category": category,
        "support_count": support_count,
        "conflict_count": 0,
        "category_counts": dict(sorted(category_counts.items())),
        "examples": examples,
        "automation_safe": strong,
        "reason": (
            "At least two categorized same-property/same-merchant/same-amount source rows support this category."
            if strong
            else "Only one categorized same-property/same-merchant/same-amount source row supports this category; not enough for autonomous source tagging."
        ),
    }


def row_reason(row: dict[str, str]) -> str:
    if row.get("automation_status") == "blocked_specific_category_required":
        return "No deterministic source evidence proves the exact Baselane category; auto-tagging would risk garbage-in downstream reports."
    if not row.get("baselane_category"):
        return "Baselane category is blank in source export."
    return "Review required before source mutation."


def native_split_evidence(row: dict[str, Any]) -> dict[str, str]:
    raw_merchant = str(row.get("merchant") or "")
    if " | " in raw_merchant or " - " in raw_merchant or "#" in raw_merchant:
        return {"status": "not_required", "category": "", "reason": "", "split_rule": ""}
    merchant = normalized_path_text(row.get("merchant"))
    description = normalized_path_text(row.get("description"))
    combined = f"{merchant} {description}"
    if "morgan linen services" in combined:
        return {
            "status": "automation_safe_native_split",
            "category": "Cleaning & Maintenance",
            "reason": "Morgan Linen is a Baselane-native Madison split vendor; split 84/86/88/90 by 4/20,5/20,6/20,5/20 and tag Cleaning & Maintenance.",
            "split_rule": "madison_morgan_linen_4_5_6_5",
        }
    if "spectrum" in combined:
        return {
            "status": "automation_safe_native_split",
            "category": "Phone, Cable & Internet",
            "reason": "Spectrum is a Baselane-native Madison split vendor; split equally across 84/86/88/90 and tag Phone, Cable & Internet.",
            "split_rule": "madison_spectrum_6958_equal",
        }
    return {"status": "not_required", "category": "", "reason": "", "split_rule": ""}


def merchant_rule_evidence(
    row: dict[str, Any],
    historical: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, str]:
    merchant = normalized_path_text(row.get("merchant"))
    description = normalized_path_text(row.get("description"))
    combined = f"{merchant} {description}"
    if "wy secretary of sta" not in combined and "secretary of state" not in combined:
        return {"status": "not_required", "category": "", "reason": "", "rule": ""}

    category = "Tax Licenses & Registrations"
    historical_status = str(historical.get("status") or "")
    if historical_status == "conflicting_support" or count(historical.get("conflict_count")) > 1:
        return {
            "status": "blocked_conflicting_merchant_rule",
            "category": "",
            "reason": "Secretary of State merchant matches a registration rule, but historical category support conflicts.",
            "rule": "secretary_of_state_registration",
        }

    conflicting_categories = {
        canonical_category(name)
        for source_counts in (
            historical.get("category_counts") or {},
            context.get("same_property_merchant_category_counts") or {},
            context.get("same_property_merchant_exact_amount_category_counts") or {},
        )
        for name, support in dict(source_counts).items()
        if canonical_category(name) and canonical_category(name) != category and count(support) > 0
    }
    if conflicting_categories:
        return {
            "status": "blocked_conflicting_merchant_rule",
            "category": "",
            "reason": "Secretary of State merchant matches a registration rule, but context contains conflicting category evidence.",
            "rule": "secretary_of_state_registration",
        }

    return {
        "status": "automation_safe_government_registration",
        "category": category,
        "reason": "Secretary of State payment merchant deterministically supports Tax Licenses & Registrations with no conflicting source category evidence.",
        "rule": "secretary_of_state_registration",
    }


def normalized_property_tokens(value: object) -> set[str]:
    text = re.sub(r"[^A-Z0-9 ]+", " ", normalize(value))
    tokens = {token for token in text.split() if len(token) > 1 and token not in {"AVE", "AVENUE", "ST", "STREET", "RD", "ROAD", "LN", "LANE", "DR", "DRIVE", "N", "S", "E", "W"}}
    return tokens


def hemlane_category_is_rent(tx: dict[str, Any]) -> bool:
    text = normalize(f"{tx.get('payment_category')} {tx.get('payment_subcategory')}")
    words = re.sub(r"[^A-Z0-9]+", " ", text).strip()
    if "NON RENTAL" in words:
        return False
    tokens = set(words.split())
    return "RENT" in tokens or "RENTS" in tokens or "RENTAL INCOME" in words


def hemlane_transaction_is_collected_rent(tx: dict[str, Any]) -> bool:
    if not hemlane_category_is_rent(tx):
        return False
    success_amount = amount_number(tx.get("success_amount"))
    if success_amount is not None and round(success_amount, 2) > 0:
        return True
    status = normalize(tx.get("status"))
    collected_status_terms = {"COMPLETED", "COMPLETE", "COLLECTED", "PAID", "SUCCESS", "SUCCEEDED", "SETTLED"}
    return any(term in status for term in collected_status_terms)


def hemlane_transaction_evidence(row: dict[str, Any], hemlane_report: dict[str, Any]) -> dict[str, Any]:
    if hemlane_report.get("status") != "ok":
        return {"status": "not_available", "category": "", "reason": "Hemlane live transaction report is not ok."}
    merchant_text = normalize(f"{row.get('merchant')} {row.get('description')}")
    if "HEMLANE" not in merchant_text:
        return {"status": "not_required", "category": "", "reason": "Source row is not a Hemlane transaction."}
    row_amount = amount_number(row.get("amount"))
    row_date = parsed_date(row.get("date"))
    row_tokens = normalized_property_tokens(row.get("property"))
    if row_amount is None or row_date is None or not row_tokens:
        return {"status": "blocked_missing_match_key", "category": "", "reason": "Baselane row lacks amount, date, or property tokens required for live Hemlane matching."}
    matches = []
    for tx in hemlane_report.get("transactions") or []:
        if not isinstance(tx, dict) or not hemlane_transaction_is_collected_rent(tx):
            continue
        tx_amount = amount_number(tx.get("amount"))
        if tx_amount is None or round(tx_amount, 2) != round(abs(row_amount), 2):
            continue
        tx_dates = [parsed_date(tx.get(name)) for name in ("posted_at", "transaction_date", "due_date")]
        tx_dates = [date for date in tx_dates if date is not None]
        if not tx_dates or min(abs((date - row_date).days) for date in tx_dates) > 7:
            continue
        tx_tokens = normalized_property_tokens(f"{tx.get('property')} {tx.get('property_address')}")
        if not row_tokens.intersection(tx_tokens):
            continue
        matches.append(tx)
    if len(matches) == 1:
        tx = matches[0]
        return {
            "status": "automation_safe_hemlane_live_transaction",
            "category": "Rents",
            "reason": "Exactly one live Hemlane completed rent transaction matches property tokens, amount, and date window.",
            "transaction_id": tx.get("id"),
            "transaction_date": tx.get("transaction_date"),
            "posted_at": tx.get("posted_at"),
            "transaction_status": tx.get("status"),
            "success_amount": tx.get("success_amount"),
            "payment_category": tx.get("payment_category"),
            "payment_subcategory": tx.get("payment_subcategory"),
            "property": tx.get("property"),
        }
    if len(matches) > 1:
        return {"status": "blocked_ambiguous_hemlane_match", "category": "", "reason": "Multiple live Hemlane rent transactions match this Baselane row; do not auto-tag.", "match_count": len(matches)}
    return {"status": "blocked_no_hemlane_match", "category": "", "reason": "No live Hemlane collected rent transaction matches property, amount, and date window."}


def context_candidate(
    historical: dict[str, Any],
    context: dict[str, Any],
    document_context: dict[str, Any] | None = None,
    receipt_evidence: dict[str, Any] | None = None,
    native_split: dict[str, Any] | None = None,
    merchant_rule: dict[str, Any] | None = None,
    hemlane_evidence: dict[str, Any] | None = None,
) -> dict[str, str]:
    hemlane_evidence = hemlane_evidence or {}
    if hemlane_evidence.get("status") == "automation_safe_hemlane_live_transaction" and hemlane_evidence.get("category"):
        return {
            "status": "automation_safe_hemlane_live_transaction",
            "category": str(hemlane_evidence.get("category") or ""),
            "reason": str(hemlane_evidence.get("reason") or "Live Hemlane transaction evidence supports this category."),
        }
    native_split = native_split or {}
    if native_split.get("status") == "automation_safe_native_split" and native_split.get("category"):
        return {
            "status": "automation_safe_native_split",
            "category": str(native_split.get("category") or ""),
            "reason": str(native_split.get("reason") or "Native split rule supports this category."),
        }
    merchant_rule = merchant_rule or {}
    if merchant_rule.get("status") == "automation_safe_government_registration" and merchant_rule.get("category"):
        return {
            "status": "automation_safe_government_registration",
            "category": str(merchant_rule.get("category") or ""),
            "reason": str(merchant_rule.get("reason") or "Government registration merchant rule supports this category."),
        }
    if historical.get("automation_safe") is True and historical.get("suggested_category"):
        return {
            "status": "automation_safe_exact_history",
            "category": str(historical.get("suggested_category") or ""),
            "reason": "At least two same-property/same-merchant/same-amount categorized source rows agree.",
        }
    exact_note_counts = Counter(context.get("exact_amount_note_inferred_category_counts") or {})
    historical_category = str(historical.get("suggested_category") or "")
    if historical_category and exact_note_counts:
        conflicting_note_counts = Counter(
            {
                category: support_count
                for category, support_count in exact_note_counts.items()
                if category != historical_category and count(support_count) > 0
            }
        )
        if conflicting_note_counts:
            category, support_count = conflicting_note_counts.most_common(1)[0]
            if count(support_count) >= 2 or len(exact_note_counts) > 1:
                return {
                    "status": "conflicting_context",
                    "category": "",
                    "reason": (
                        f"Weak exact historical category {historical_category} conflicts with "
                        f"same-amount note-derived {category}; do not guess."
                    ),
                }
    if historical_category and len(exact_note_counts) == 1:
        note_category = exact_note_counts.most_common(1)[0][0]
        if note_category != historical_category:
            return {
                "status": "conflicting_context",
                "category": "",
                "reason": "Weak exact historical category conflicts with same-amount note-derived category; do not guess.",
            }
    if len(exact_note_counts) == 1:
        category, support_count = exact_note_counts.most_common(1)[0]
        if count(support_count) >= 2:
            return {
                "status": "context_only_exact_amount_notes",
                "category": category,
                "reason": "Historical same-property/same-merchant/same-amount notes repeatedly imply this category, but current source row still needs explicit Baselane source tagging.",
            }
    document_context = document_context or {}
    document_category_counts = Counter(document_context.get("category_counts") or {})
    if len(document_category_counts) == 1 and count(document_context.get("support_count")) > 0:
        category, support_count = document_category_counts.most_common(1)[0]
        return {
            "status": "automation_safe_public_document",
            "category": category,
            "reason": f"Public Dropbox property document filename evidence supports {category} with {support_count} exact identifier match(es).",
        }
    receipt_evidence = receipt_evidence or {}
    if receipt_evidence.get("status") == "automation_safe_email_receipt" and receipt_evidence.get("category"):
        receipt_category = str(receipt_evidence.get("category") or "")
        return {
            "status": "automation_safe_email_receipt",
            "category": receipt_category,
            "reason": str(receipt_evidence.get("reason") or "Email receipt evidence supports this category."),
        }
    same_merchant_counts = Counter(context.get("same_property_merchant_category_counts") or {})
    if (
        historical.get("automation_safe") is not True
        and historical_category
        and len(same_merchant_counts) == 1
    ):
        sm_category, sm_support = same_merchant_counts.most_common(1)[0]
        if sm_category == historical_category and count(sm_support) >= 5:
            return {
                "status": "automation_safe_same_merchant_history",
                "category": sm_category,
                "reason": f"Same-property/same-merchant context has {sm_support} categorized row(s) all agreeing on {sm_category!r}.",
            }
        if sm_category == historical_category and count(sm_support) >= 2:
            return {
                "status": "context_only_same_merchant",
                "category": sm_category,
                "reason": f"Same-property/same-merchant context has {sm_support} categorized row(s) all agreeing on {sm_category!r}, but exact-amount evidence is not strong enough for autonomous source tagging.",
            }
    if historical_category:
        return {
            "status": str(historical.get("status") or "weak_history"),
            "category": historical_category,
            "reason": str(historical.get("reason") or "Historical exact-match support exists but is not automation-safe."),
        }
    note_counts = Counter(context.get("note_inferred_category_counts") or {})
    if len(note_counts) == 1:
        category, support_count = note_counts.most_common(1)[0]
        if count(support_count) >= 2:
            return {
                "status": "context_only_notes",
                "category": category,
                "reason": "Same-property/same-merchant notes repeatedly imply this category, but amount-specific evidence is not strong enough for autonomous source tagging.",
            }
    category_counts = Counter(context.get("same_property_merchant_category_counts") or {})
    if len(category_counts) == 1:
        category, support_count = category_counts.most_common(1)[0]
        if count(support_count) >= 5:
            return {
                "status": "automation_safe_same_merchant_history",
                "category": category,
                "reason": f"Same-property/same-merchant context has {support_count} categorized row(s) all agreeing on {category!r}.",
            }
        if count(support_count) >= 2:
            return {
                "status": "context_only_same_merchant",
                "category": category,
                "reason": "Same-property/same-merchant categorized rows agree, but exact-amount evidence is not strong enough for autonomous source tagging.",
            }
    if len(category_counts) > 1 or len(note_counts) > 1:
        return {
            "status": "conflicting_context",
            "category": "",
            "reason": "Same-property/same-merchant context has conflicting categories or note-derived categories; do not guess.",
        }
    return {
        "status": "no_deterministic_candidate",
        "category": "",
        "reason": "No categorized or note-derived same-property/same-merchant context proves a category.",
    }


def flat_evidence_fields(evidence: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "historical_evidence_status": evidence.get("status"),
        "historical_suggested_category": evidence.get("suggested_category"),
        "historical_support_count": evidence.get("support_count"),
        "historical_conflict_count": evidence.get("conflict_count"),
        "historical_category_counts": evidence.get("category_counts") or {},
        "historical_automation_safe": evidence.get("automation_safe") is True,
        "context_candidate_status": candidate.get("status"),
        "context_candidate_category": candidate.get("category"),
        "context_candidate_reason": candidate.get("reason"),
        "candidate_category": candidate.get("category") or "",
    }


def build_report(root: Path, actions_csv: Path, source_plan: Path) -> dict[str, Any]:
    rows = read_csv(actions_csv)
    source_fix = read_json(source_plan)
    history_csv_paths = export_csvs(root)
    history_csv = history_csv_paths[-1] if history_csv_paths else None
    raw_history_row_count = sum(len(read_csv(path)) for path in history_csv_paths)
    history_rows = read_history_rows(history_csv_paths)
    hemlane_report = read_json(root / "reports" / "hemlane_live_transactions.json")
    normalized_rows = []
    for row in rows:
        evidence = historical_category_evidence(row, history_rows)
        context = contextual_category_evidence(row, history_rows)
        document_context = (
            document_category_evidence(row, root)
            if row.get("automation_status") == "blocked_specific_category_required"
            else empty_document_category_evidence()
        )
        email_context = email_invoice_evidence(row)
        mail_context = local_mail_invoice_evidence(row, root)
        gws_context = gws_mail_invoice_evidence(row)
        receipt_evidence = email_receipt_category_evidence(
            {
                **row,
                "gws_mail_invoice_evidence": gws_context,
                "local_mail_invoice_evidence": mail_context,
            }
        )
        native_split = native_split_evidence(row)
        merchant_rule = merchant_rule_evidence(row, evidence, context)
        hemlane_evidence = hemlane_transaction_evidence(row, hemlane_report)
        candidate = context_candidate(evidence, context, document_context, receipt_evidence, native_split, merchant_rule, hemlane_evidence)
        normalized_rows.append(
            {
                "id": row.get("id"),
                "property": row.get("property"),
                "month": row.get("month"),
                "date": row.get("date"),
                "amount": row.get("amount"),
                "merchant": row.get("merchant"),
                "description": row.get("description"),
                "current_label": row.get("label"),
                "baselane_category": row.get("baselane_category"),
                "automation_status": row.get("automation_status"),
                "required_action": "Set the exact Baselane transaction category at source, then rerun weekly file updates.",
                "why_not_auto_apply": row_reason(row),
                **flat_evidence_fields(evidence, candidate),
                "historical_category_evidence": evidence,
                "contextual_category_evidence": context,
                "document_category_evidence": document_context,
                "email_invoice_evidence": email_context,
                "local_mail_invoice_evidence": mail_context,
                "gws_mail_invoice_evidence": gws_context,
                "email_receipt_category_evidence": receipt_evidence,
                "native_split_evidence": native_split,
                "merchant_rule_evidence": merchant_rule,
                "hemlane_live_transaction_evidence": hemlane_evidence,
                "context_candidate": candidate,
                "source_file": row.get("source_file"),
            }
        )
    status_counts = Counter(row.get("automation_status") or "unknown" for row in rows)
    property_counts = Counter(row.get("property") or "unknown" for row in rows)
    evidence_status_counts = Counter(
        str(row.get("historical_category_evidence", {}).get("status") or "unknown")
        for row in normalized_rows
    )
    automation_safe_suggestion_count = sum(
        1 for row in normalized_rows if row.get("historical_category_evidence", {}).get("automation_safe") is True
    )
    context_candidate_status_counts = Counter(
        str(row.get("context_candidate", {}).get("status") or "unknown")
        for row in normalized_rows
    )
    email_invoice_queries = [
        {
            "id": row.get("id"),
            "property": row.get("property"),
            "date": row.get("date"),
            "amount": row.get("amount"),
            "merchant": row.get("merchant"),
            "payment_rail": (row.get("email_invoice_evidence") or {}).get("payment_rail"),
            "payee_tokens": (row.get("email_invoice_evidence") or {}).get("payee_tokens") or [],
            "search_query": (row.get("email_invoice_evidence") or {}).get("search_query"),
            "expected_window": (row.get("email_invoice_evidence") or {}).get("expected_window") or {},
            "local_mail_status": (row.get("local_mail_invoice_evidence") or {}).get("status"),
            "local_mail_match_count": (row.get("local_mail_invoice_evidence") or {}).get("match_count"),
            "local_mail_checked_file_count": (row.get("local_mail_invoice_evidence") or {}).get("checked_file_count"),
            "local_mail_matches": (row.get("local_mail_invoice_evidence") or {}).get("matches") or [],
            "gws_mail_status": (row.get("gws_mail_invoice_evidence") or {}).get("status"),
            "gws_mail_match_count": (row.get("gws_mail_invoice_evidence") or {}).get("match_count"),
            "gws_mail_matches": (row.get("gws_mail_invoice_evidence") or {}).get("matches") or [],
            "gws_mail_errors": (row.get("gws_mail_invoice_evidence") or {}).get("errors") or [],
        }
        for row in normalized_rows
        if (row.get("email_invoice_evidence") or {}).get("required") is True
    ]
    row_count = len(rows)
    return {
        "generated_at": iso_z(),
        "status": "ok" if row_count == 0 else "review",
        "row_count": row_count,
        "source_month": source_fix.get("source_month") or (rows[0].get("month") if rows else None),
        "autonomous_write_allowed": row_count == 0,
        "downstream_hold": row_count > 0,
        "decision": (
            "No ECO GL source-fix rows remain."
            if row_count == 0
            else f"{row_count} ECO GL rows need exact Baselane source categories before downstream reporting/publish/email can be trusted."
        ),
        "next_action": (
            "Continue scheduled weekly/monthly automation."
            if row_count == 0
            else "Open reports/baselane_ecogl_source_fix_actions.csv, fix those source categories in Baselane/ECO GL, then rerun bash scripts/baselane_weekly_file_updates_cron.sh."
        ),
        "source_fix_plan_status": source_fix.get("status"),
        "source_fix_plan_digest": source_fix.get("idempotency_digest"),
        "automation_status_counts": dict(sorted(status_counts.items())),
        "historical_export_csv": str(history_csv) if history_csv else "",
        "historical_export_csvs": [str(path) for path in history_csv_paths],
        "historical_export_csv_count": len(history_csv_paths),
        "historical_raw_row_count": raw_history_row_count,
        "historical_deduped_row_count": len(history_rows),
        "historical_evidence_status_counts": dict(sorted(evidence_status_counts.items())),
        "historical_evidence_automation_safe_count": automation_safe_suggestion_count,
        "context_candidate_status_counts": dict(sorted(context_candidate_status_counts.items())),
        "email_invoice_query_count": len(email_invoice_queries),
        "email_invoice_local_match_count": sum(1 for row in normalized_rows if (row.get("local_mail_invoice_evidence") or {}).get("status") == "matched"),
        "email_invoice_gws_match_count": sum(1 for row in normalized_rows if (row.get("gws_mail_invoice_evidence") or {}).get("status") == "matched"),
        "email_invoice_queries": email_invoice_queries,
        "property_counts": dict(sorted(property_counts.items())),
        "actions_csv": str(actions_csv),
        "source_fix_plan": str(source_plan),
        "rows": normalized_rows,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# ECO GL Source-Fix Evidence",
        "",
        f"- Status: `{report['status']}`",
        f"- Rows: `{report['row_count']}`",
        f"- Downstream hold: `{str(report['downstream_hold']).lower()}`",
        f"- Autonomous write allowed: `{str(report['autonomous_write_allowed']).lower()}`",
        f"- Decision: {report['decision']}",
        f"- Next action: {report['next_action']}",
        f"- Source queue: `{report['actions_csv']}`",
        f"- Historical export: `{report.get('historical_export_csv') or 'none'}`",
        f"- Historical exports scanned: `{report.get('historical_export_csv_count')}`",
        f"- Historical rows: raw `{report.get('historical_raw_row_count')}` → deduped `{report.get('historical_deduped_row_count')}`",
        f"- Historical evidence: `{report.get('historical_evidence_status_counts')}`",
        f"- Automation-safe suggestions: `{report.get('historical_evidence_automation_safe_count')}`",
        f"- Context candidates: `{report.get('context_candidate_status_counts')}`",
        "",
        "## Rows",
    ]
    for row in report.get("rows") or []:
        evidence = row.get("historical_category_evidence") or {}
        candidate = row.get("context_candidate") or {}
        email_context = row.get("email_invoice_evidence") or {}
        lines.extend(
            [
                "",
                f"- `{row.get('id')}` — {row.get('property')} — {row.get('date')} — {row.get('amount')}",
                f"  - Merchant: `{row.get('merchant')}`",
                f"  - Current label: `{row.get('current_label')}`",
                f"  - Historical evidence: `{evidence.get('status')}`"
                + (f" → `{evidence.get('suggested_category')}` ({evidence.get('support_count')} support)" if evidence.get("suggested_category") else ""),
                f"  - Context candidate: `{candidate.get('status')}`"
                + (f" → `{candidate.get('category')}`" if candidate.get("category") else ""),
                f"  - Required action: {row.get('required_action')}",
                f"  - Why blocked: {row.get('why_not_auto_apply')}",
            ]
        )
        if email_context.get("required"):
            mail_context = row.get("local_mail_invoice_evidence") or {}
            gws_context = row.get("gws_mail_invoice_evidence") or {}
            lines.extend(
                [
                    f"  - Email invoice search: `{email_context.get('search_query') or 'manual search required'}`",
                    f"  - Local mail evidence: `{mail_context.get('status') or 'unknown'}` ({mail_context.get('match_count') or 0} match)",
                    f"  - Gmail evidence: `{gws_context.get('status') or 'unknown'}` ({gws_context.get('match_count') or 0} match)",
                    f"  - Email evidence rule: {email_context.get('reason')}",
                ]
            )
    lines.append("")
    return "\n".join(lines)


def render_email_invoice_queries(report: dict[str, Any]) -> str:
    lines = [
        "# ECO GL Email Invoice Search Queries",
        "",
        "- Scope: person-payment rows only; query artifact does not read email, mutate Baselane, publish files, or send email.",
        f"- Query count: `{report.get('email_invoice_query_count')}`",
        "",
        "## Queries",
    ]
    for record in report.get("email_invoice_queries") or []:
        window = record.get("expected_window") or {}
        lines.extend(
            [
                "",
                f"- `{record.get('id')}` — {record.get('property')} — {record.get('date')} — {record.get('amount')} — {record.get('merchant')}",
                f"  - Rail: `{record.get('payment_rail')}`; window: `{window.get('after') or 'unknown'}` to `{window.get('before') or 'unknown'}`",
                f"  - Query: `{record.get('search_query') or 'manual search required'}`",
                f"  - Local mail: `{record.get('local_mail_status') or 'unknown'}`; checked `{record.get('local_mail_checked_file_count') or 0}`; matches `{record.get('local_mail_match_count') or 0}`",
                f"  - Gmail: `{record.get('gws_mail_status') or 'unknown'}`; matches `{record.get('gws_mail_match_count') or 0}`",
            ]
        )
    if not report.get("email_invoice_queries"):
        lines.append("- None.")
    lines.append("")
    return "\n".join(lines)


def write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a concise evidence packet for remaining ECO GL Baselane source category fixes.")
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--actions-csv", type=Path)
    parser.add_argument("--source-plan", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--email-invoice-json", type=Path)
    parser.add_argument("--email-invoice-markdown", type=Path)
    args = parser.parse_args()

    root = args.root
    actions_csv = args.actions_csv or root / "reports" / "baselane_ecogl_source_fix_actions.csv"
    source_plan = args.source_plan or root / "reports" / "baselane_ecogl_source_fix_plan.json"
    report_path = args.report or root / "reports" / "baselane_ecogl_source_fix_evidence.json"
    markdown_path = args.markdown or root / "reports" / "baselane_ecogl_source_fix_evidence.md"
    email_invoice_json = args.email_invoice_json or root / "reports" / "baselane_ecogl_source_fix_email_invoice_queries.json"
    email_invoice_markdown = args.email_invoice_markdown or root / "reports" / "baselane_ecogl_source_fix_email_invoice_queries.md"
    report = build_report(root, actions_csv, source_plan)
    write_json(report_path, report)
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    write_json(
        email_invoice_json,
        {
            "generated_at": report.get("generated_at"),
            "status": "review" if report.get("email_invoice_query_count") else "ok",
            "policy": "Search-query artifact only; does not read email, mutate Baselane, publish docs, or send email.",
            "query_count": report.get("email_invoice_query_count"),
            "local_match_count": report.get("email_invoice_local_match_count"),
            "gws_match_count": report.get("email_invoice_gws_match_count"),
            "queries": report.get("email_invoice_queries") or [],
        },
    )
    email_invoice_markdown.write_text(render_email_invoice_queries(report), encoding="utf-8")
    print(json.dumps({"status": report["status"], "row_count": report["row_count"], "downstream_hold": report["downstream_hold"]}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
