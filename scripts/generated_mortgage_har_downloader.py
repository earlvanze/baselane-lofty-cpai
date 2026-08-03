#!/usr/bin/env python3
"""Runtime for generated mortgage downloaders backed by full-response HAR evidence.

This runtime never replays authentication. It only reads an already captured HAR,
extracts embedded statement PDF payloads, and optionally writes target-month PDFs
when explicitly run with --apply or MORTGAGE_GENERATED_HAR_DOWNLOADER_APPLY=1.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlparse

SCRIPT_PATH = Path(__file__).absolute()
WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", SCRIPT_PATH.parents[1]))

PDF_KEYS = {
    "pdf",
    "pdfbase64",
    "pdfcontent",
    "pdfdata",
    "file",
    "filecontent",
    "filedata",
    "document",
    "documentcontent",
    "documentdata",
    "content",
    "data",
}
NAME_KEYS = {
    "name",
    "documentname",
    "filename",
    "title",
    "type",
    "documenttype",
    "documenttypename",
    "documenttitle",
    "label",
    "description",
}
DATE_KEYS = {
    "date",
    "statementdate",
    "createddate",
    "posteddate",
    "documentdate",
    "documentcreationdate",
    "creationdate",
}
ID_KEYS = {
    "id",
    "docid",
    "documentid",
    "documentidentifier",
    "documentidentifierid",
}
BODY_RECAPTURE_CAPTURE_METHOD = "visible_cdp_capture_helper_required"
BODY_RECAPTURE_CAPTURE_INSTRUCTION = (
    "Run capture_command before opening/downloading the target statement PDF in the visible browser; "
    "the helper records Network.getResponseBody so binary PDF payloads are retained."
)
BODY_RECAPTURE_MANUAL_HAR_EXPORT_WARNING = (
    "A browser DevTools HAR export may show HTTP 200 document responses while omitting content.text "
    "for application/octet-stream PDF bodies."
)
DEFAULT_JSON_PARSE_SIZE_LIMIT = 2_000_000
DOCUMENT_JSON_PATH_TOKENS = {
    "document",
    "download",
    "statement",
    "servicing",
    "loan",
    "mortgage",
    "sedm",
    "ecmdoc",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def without_volatile_report_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: without_volatile_report_fields(item)
            for key, item in value.items()
            if key not in {"generated_at", "idempotency_digest"}
        }
    if isinstance(value, list):
        return [without_volatile_report_fields(item) for item in value]
    return value


def restore_volatile_report_fields(current: Any, previous: Any) -> Any:
    if isinstance(current, dict) and isinstance(previous, dict):
        restored = dict(current)
        for key, value in current.items():
            if key == "generated_at" and key in previous:
                restored[key] = previous[key]
            elif key in previous:
                restored[key] = restore_volatile_report_fields(value, previous[key])
        return restored
    if isinstance(current, list) and isinstance(previous, list) and len(current) == len(previous):
        return [restore_volatile_report_fields(item, previous[index]) for index, item in enumerate(current)]
    return current


def stable_report_digest(value: Any) -> str:
    stable_value = without_volatile_report_fields(value)
    payload = json.dumps(stable_value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def preserve_volatile_fields_if_unchanged(report: dict[str, Any], path: Path) -> dict[str, Any]:
    if not path.exists():
        return report
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return report
    if not isinstance(previous, dict):
        return report
    if without_volatile_report_fields(previous) != without_volatile_report_fields(report):
        return report
    restored = restore_volatile_report_fields(report, previous)
    return restored if isinstance(restored, dict) else report


def write_json_report(path: Path, report: dict[str, Any]) -> dict[str, Any]:
    report = dict(report)
    report["idempotency_digest"] = stable_report_digest(report)
    stable_report = preserve_volatile_fields_if_unchanged(report, path)
    content = json.dumps(stable_report, indent=2, sort_keys=True) + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        path.write_text(content, encoding="utf-8")
    return stable_report


def valid_year_month(value: str) -> bool:
    if re.fullmatch(r"20\d{2}-\d{2}", value) is None:
        return False
    month = int(value.split("-", 1)[1])
    return 1 <= month <= 12


def add_months(month: str, offset: int) -> str:
    year, mon = [int(part) for part in month.split("-")]
    absolute = year * 12 + (mon - 1) + offset
    target_year, target_month_zero = divmod(absolute, 12)
    return f"{target_year:04d}-{target_month_zero + 1:02d}"


def manifest_env_value(manifest: dict[str, Any] | None, key: str) -> str:
    if not manifest:
        return ""
    env_maps = [
        manifest.get("env"),
        (manifest.get("registry_entry") if isinstance(manifest.get("registry_entry"), dict) else {}).get("env"),
        (manifest.get("downloader_config") if isinstance(manifest.get("downloader_config"), dict) else {}).get("env"),
    ]
    for env_map in env_maps:
        if not isinstance(env_map, dict):
            continue
        value = str(env_map.get(key) or "").strip()
        if value:
            return value
    return ""


def target_month_default_offset(manifest: dict[str, Any] | None = None, *, include_manifest: bool = True) -> int:
    offset_text = str(
        os.environ.get("MORTGAGE_GENERATED_HAR_TARGET_MONTH_DEFAULT_OFFSET")
        or (manifest_env_value(manifest, "MORTGAGE_GENERATED_HAR_TARGET_MONTH_DEFAULT_OFFSET") if include_manifest else "")
        or "0"
    ).strip()
    try:
        return int(offset_text)
    except ValueError:
        return 0


def target_statement_month(manifest: dict[str, Any] | None = None) -> str:
    explicit = str(os.environ.get("MORTGAGE_GENERATED_HAR_TARGET_MONTH") or "").strip()
    if valid_year_month(explicit):
        return explicit
    statement_base = str(
        os.environ.get("MORTGAGE_STATEMENT_TARGET_MONTH")
        or os.environ.get("BASELANE_MORTGAGE_STATEMENT_TARGET_MONTH")
        or ""
    ).strip()
    if valid_year_month(statement_base):
        return statement_base
    workflow_base = str(
        os.environ.get("MORTGAGE_WORKFLOW_TARGET_MONTH")
        or os.environ.get("BASELANE_MONTHLY_TARGET_STAMP")
        or ""
    ).strip()
    if valid_year_month(workflow_base):
        offset = target_month_default_offset(manifest)
        return add_months(workflow_base, offset)
    offset = target_month_default_offset(manifest)
    if offset:
        current_month = datetime.now(timezone.utc).strftime("%Y-%m")
        return add_months(current_month, offset)
    if manifest:
        manifest_month = str(manifest.get("target_statement_month") or "").strip()
        if valid_year_month(manifest_month):
            return manifest_month
    return datetime.now(timezone.utc).strftime("%Y-%m")


def load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "JSON root is not an object"
    return data, None


def iter_har_entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    log = data.get("log") if isinstance(data.get("log"), dict) else {}
    entries = log.get("entries") if isinstance(log.get("entries"), list) else []
    return [entry for entry in entries if isinstance(entry, dict)]


def header_value(headers: Any, name: str) -> str:
    if not isinstance(headers, list):
        return ""
    wanted = name.casefold()
    for item in headers:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "").casefold() == wanted:
            return str(item.get("value") or "")
    return ""


def content_text(entry: dict[str, Any]) -> tuple[str, str, str]:
    response = entry.get("response") if isinstance(entry.get("response"), dict) else {}
    content = response.get("content") if isinstance(response.get("content"), dict) else {}
    mime_type = str(content.get("mimeType") or response.get("mimeType") or "").split(";")[0].strip().lower()
    text = content.get("text")
    encoding = str(content.get("encoding") or "").strip().lower()
    return (text if isinstance(text, str) else "", mime_type, encoding)


def response_content_size(entry: dict[str, Any]) -> int | None:
    response = entry.get("response") if isinstance(entry.get("response"), dict) else {}
    content = response.get("content") if isinstance(response.get("content"), dict) else {}
    try:
        return int(content.get("size"))
    except (TypeError, ValueError):
        return None


def request_post_json(entry: dict[str, Any]) -> dict[str, Any] | None:
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    post_data = request.get("postData") if isinstance(request.get("postData"), dict) else {}
    text = post_data.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        value = json.loads(text)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def omitted_binary_download_metadata(entry: dict[str, Any], *, mime_type: str, text: str) -> dict[str, Any] | None:
    response = entry.get("response") if isinstance(entry.get("response"), dict) else {}
    if response.get("status") != 200 or text:
        return None
    size = response_content_size(entry)
    if size is None or size <= 0:
        return None
    if "json" in mime_type or mime_type in {"text/html", "text/plain"}:
        return None
    body = request_post_json(entry)
    if not body:
        return None
    name = first_field(body, NAME_KEYS)
    date = first_field(body, DATE_KEYS)
    identifier = normalize_identifier(first_field(body, ID_KEYS))
    if not identifier or not (statement_name(name) or date):
        return None
    return {
        "name": name,
        "date": date,
        "document_identifier": identifier,
        "har_download_response_status": response.get("status"),
        "har_download_response_mime_type": mime_type or None,
        "har_download_response_size": size,
        "har_download_response_body_exported": False,
    }


def decode_pdf_text(text: str, encoding: str = "") -> bytes | None:
    if not text:
        return None
    if text.startswith("%PDF"):
        return text.encode("latin-1", errors="ignore")
    match = re.match(r"data:application/pdf;base64,(.+)", text, re.IGNORECASE | re.DOTALL)
    candidate = match.group(1) if match else text
    if encoding and encoding != "base64" and not match:
        return None
    compact = re.sub(r"\s+", "", candidate)
    if len(compact) < 8:
        return None
    try:
        data = base64.b64decode(compact, validate=True)
    except Exception:
        return None
    if not data.startswith(b"%PDF"):
        return None
    return data


def decoded_response_prefix(text: str, encoding: str = "", limit: int = 512) -> bytes:
    if not text:
        return b""
    match = re.match(r"data:[^;,]+;base64,(.+)", text, re.IGNORECASE | re.DOTALL)
    candidate = match.group(1) if match else text
    if encoding == "base64" or match:
        compact = re.sub(r"\s+", "", candidate)
        try:
            return base64.b64decode(compact, validate=True)[:limit]
        except Exception:
            return b""
    return text[:limit].encode("utf-8", errors="ignore")


def pdf_payload_status(text: str, encoding: str = "") -> str:
    if decode_pdf_text(text, encoding) is not None:
        return "available"
    if not text:
        return "missing"
    prefix = decoded_response_prefix(text, encoding)
    lowered = prefix.lstrip().lower()
    if lowered.startswith((b"<!doctype html", b"<html")) or b"chrome-extension://" in lowered:
        return "html_viewer_shell"
    return "missing_or_invalid"


def json_parse_size_limit() -> int:
    raw = str(os.environ.get("MORTGAGE_GENERATED_HAR_JSON_PARSE_SIZE_LIMIT") or "").strip()
    if not raw:
        return DEFAULT_JSON_PARSE_SIZE_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_JSON_PARSE_SIZE_LIMIT
    return max(value, 0)


def should_parse_large_json(url: str, text: str, limit: int) -> bool:
    if limit <= 0 or len(text) <= limit:
        return True
    parsed = urlparse(url)
    haystack = f"{parsed.netloc} {parsed.path}".casefold()
    return any(token in haystack for token in DOCUMENT_JSON_PATH_TOKENS)


def parse_json_body(text: str, *, url: str = "") -> Any | None:
    if not text:
        return None
    if not should_parse_large_json(url, text, json_parse_size_limit()):
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def normalized_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def first_field(data: dict[str, Any], keys: set[str]) -> str:
    for key, value in data.items():
        if normalized_key(key) in keys and value not in (None, "", [], {}):
            return str(value)
    return ""


def normalize_identifier(value: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d+\.0", text):
        return text[:-2]
    return text


def statement_name(value: str) -> bool:
    lowered = value.casefold()
    return any(token in lowered for token in ["statement", "billing", "mortgage"])


def find_statement_date(*values: object) -> str:
    candidates = [str(value or "") for value in values if str(value or "").strip()]
    for value in candidates:
        match = re.search(r"(20\d{2})[-_/](\d{1,2})[-_/](\d{1,2})", value)
        if match:
            return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
        match = re.search(r"(\d{1,2})[-_/](\d{1,2})[-_/](20\d{2})", value)
        if match:
            return f"{match.group(3)}-{int(match.group(1)):02d}-{int(match.group(2)):02d}"
        match = re.search(r"(20\d{2})(\d{2})(\d{2})(?:\d{6})?", value)
        if match:
            month = int(match.group(2))
            day = int(match.group(3))
            if 1 <= month <= 12 and 1 <= day <= 31:
                return f"{match.group(1)}-{month:02d}-{day:02d}"
    for value in candidates:
        match = re.search(r"(20\d{2})[-_/](\d{1,2})", value)
        if match:
            return f"{match.group(1)}-{int(match.group(2)):02d}-01"
    return ""


def walk_json(value: Any, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 8:
        return []
    records: list[dict[str, Any]] = []
    if isinstance(value, dict):
        name = first_field(value, NAME_KEYS)
        date = first_field(value, DATE_KEYS)
        pdf = pdf_from_json(value)
        if pdf and (statement_name(name) or date):
            records.append({"name": name, "date": date, "pdf": pdf})
        for child in value.values():
            records.extend(walk_json(child, depth + 1))
    elif isinstance(value, list):
        for child in value:
            records.extend(walk_json(child, depth + 1))
    return records


def walk_statement_metadata(value: Any, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 8:
        return []
    records: list[dict[str, Any]] = []
    if isinstance(value, dict):
        name = first_field(value, NAME_KEYS)
        date = first_field(value, DATE_KEYS)
        identifier = normalize_identifier(first_field(value, ID_KEYS))
        has_pdf = pdf_from_json(value) is not None
        if not has_pdf and (statement_name(name) or (date and name)):
            records.append({"name": name, "date": date, "document_identifier": identifier})
        for child in value.values():
            records.extend(walk_statement_metadata(child, depth + 1))
    elif isinstance(value, list):
        for child in value:
            records.extend(walk_statement_metadata(child, depth + 1))
    return records


def pdf_from_json(value: Any, depth: int = 0) -> bytes | None:
    if depth > 8:
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            if normalized_key(key) in PDF_KEYS and isinstance(item, str):
                pdf = decode_pdf_text(item)
                if pdf:
                    return pdf
        for item in value.values():
            pdf = pdf_from_json(item, depth + 1)
            if pdf:
                return pdf
    elif isinstance(value, list):
        for item in value:
            pdf = pdf_from_json(item, depth + 1)
            if pdf:
                return pdf
    return None


def existing_target_month_files(output_dir: Path, target_month: str) -> list[str]:
    if not output_dir.exists():
        return []
    files = []
    for path in output_dir.rglob("*.pdf"):
        if target_month in path.name:
            files.append(str(path))
    return sorted(files)


def resolve_output_dir(manifest: dict[str, Any]) -> Path:
    configured = str(os.environ.get("MORTGAGE_GENERATED_HAR_OUTPUT_DIR") or "").strip()
    if configured:
        return Path(configured)
    target_dir = str(manifest.get("target_statement_dir") or "").strip()
    if target_dir:
        path = Path(target_dir)
        if path.is_absolute():
            return path
        root = Path(os.environ.get("DROPBOX_REAL_ESTATE_ROOT", "/mnt/c/Users/digit/Dropbox/Real Estate"))
        return root / path
    return WORKSPACE_ROOT / "reports" / "generated_mortgage_downloads" / str(manifest.get("property") or "unknown")


def resolve_target_output_dir(output_dir: Path, target_month: str) -> Path:
    year = target_month.split("-", 1)[0] if valid_year_month(target_month) else ""
    return output_dir / year if year else output_dir


def resolve_har_path(manifest: dict[str, Any]) -> Path:
    configured = str(
        os.environ.get("MORTGAGE_GENERATED_HAR_PATH")
        or os.environ.get("MORTGAGE_GENERATED_HAR_SOURCE")
        or ""
    ).strip()
    if configured:
        return Path(configured)
    return Path(str(manifest.get("har_path") or ""))


def resolve_report_path(value: object) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text)
    if path.is_absolute():
        return path
    return WORKSPACE_ROOT / path


def manifest_report_path(manifest: dict[str, Any]) -> Path | None:
    for key in ["registry_entry", "proposed_config_entry"]:
        value = manifest.get(key)
        if isinstance(value, dict):
            path = resolve_report_path(value.get("report"))
            if path:
                return path
    return None


def safe_file_stem(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._ -]+", "-", value).strip(" .-_")
    return text or "mortgage-statement"


def build_statement_filename(property_name: str, date_str: str, index: int) -> str:
    suffix = f"-{index}" if index > 1 else ""
    return f"Mortgage Statement - {date_str} - {safe_file_stem(property_name)}{suffix}.pdf"


def source_counts(candidates: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in candidates:
        source = str(item.get("source") or "unknown")
        counts[source] = counts.get(source, 0) + 1
    return dict(sorted(counts.items()))


def candidate_months(candidates: list[dict[str, Any]], *, pdf_available: bool | None = None) -> list[str]:
    months = set()
    for item in candidates:
        if pdf_available is not None and bool(item.get("pdf_available")) is not pdf_available:
            continue
        month = str(item.get("statement_month") or "").strip()
        if valid_year_month(month):
            months.add(month)
    return sorted(months)


def latest_month(months: list[str]) -> str | None:
    return months[-1] if months else None


def candidate_summary(item: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "source": item.get("source"),
        "name": item.get("name"),
        "statement_date": item.get("date"),
        "statement_month": item.get("statement_month"),
        "pdf_available": bool(item.get("pdf_available")),
        "pdf_payload_status": item.get("pdf_payload_status"),
    }
    identifier = item.get("document_identifier")
    if identifier:
        summary["document_identifier"] = identifier
    for key in [
        "har_download_response_status",
        "har_download_response_mime_type",
        "har_download_response_size",
        "har_download_response_body_exported",
    ]:
        if key in item:
            summary[key] = item.get(key)
    return {key: value for key, value in summary.items() if value not in (None, "")}


def document_identifiers(candidates: list[dict[str, Any]]) -> list[str]:
    identifiers: list[str] = []
    for item in candidates:
        identifier = str(item.get("document_identifier") or "").strip()
        if identifier and identifier not in identifiers:
            identifiers.append(identifier)
    return identifiers


def collect_candidates(har: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    direct_pdf_metadata: list[dict[str, Any]] = []
    omitted_download_metadata: dict[str, dict[str, Any]] = {}
    direct_pdf_index = 0
    for entry in iter_har_entries(har):
        request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
        response = entry.get("response") if isinstance(entry.get("response"), dict) else {}
        if response.get("status") != 200:
            continue
        text, mime_type, encoding = content_text(entry)
        url = str(request.get("url") or "")
        parsed_path = urlparse(url).path or url
        disposition = header_value(response.get("headers"), "content-disposition")
        omitted_metadata = omitted_binary_download_metadata(entry, mime_type=mime_type, text=text)
        if omitted_metadata and omitted_metadata.get("document_identifier"):
            omitted_download_metadata[str(omitted_metadata["document_identifier"])] = omitted_metadata
        if mime_type == "application/pdf":
            direct_pdf_index += 1
            pdf = decode_pdf_text(text, encoding)
            payload_status = "available" if pdf is not None else pdf_payload_status(text, encoding)
            metadata = direct_pdf_metadata.pop(0) if direct_pdf_metadata else {}
            metadata_name = str(metadata.get("name") or "")
            metadata_date = str(metadata.get("date") or "")
            date_str = find_statement_date(disposition, parsed_path, metadata_date, metadata_name)
            candidates.append(
                {
                    "source": "har:direct_pdf",
                    "name": safe_file_stem(metadata_name or disposition or Path(parsed_path).name or "statement.pdf"),
                    "date": date_str,
                    "statement_month": date_str[:7] if date_str else None,
                    "document_identifier": metadata.get("document_identifier"),
                    "pdf": pdf,
                    "pdf_available": pdf is not None,
                    "pdf_payload_status": payload_status,
                    "source_index": direct_pdf_index,
                    "date_inferred_from_metadata": bool(metadata and not find_statement_date(disposition, parsed_path)),
                }
            )
            continue
        if "json" not in mime_type:
            continue
        body = parse_json_body(text, url=url)
        statement_metadata = walk_statement_metadata(body)
        direct_pdf_metadata.extend(statement_metadata)
        for index, item in enumerate(walk_json(body), start=1):
            date_str = find_statement_date(item.get("date"), item.get("name"))
            candidates.append(
                {
                    "source": "har:json_embedded_pdf",
                    "name": item.get("name") or "statement",
                    "date": date_str,
                    "statement_month": date_str[:7] if date_str else None,
                    "pdf": item.get("pdf"),
                    "pdf_available": item.get("pdf") is not None,
                    "pdf_payload_status": "available",
                    "source_index": index,
                }
            )
    for index, item in enumerate(direct_pdf_metadata, start=1):
        date_str = find_statement_date(item.get("date"), item.get("name"))
        omitted_metadata = omitted_download_metadata.pop(str(item.get("document_identifier") or ""), None)
        candidates.append(
            {
                "source": "har:json_statement_metadata",
                "name": item.get("name") or "statement",
                "date": date_str,
                "statement_month": date_str[:7] if date_str else None,
                "document_identifier": item.get("document_identifier"),
                "pdf": None,
                "pdf_available": False,
                "pdf_payload_status": (
                    "download_response_body_omitted_from_har"
                    if omitted_metadata
                    else "metadata_without_embedded_pdf_payload"
                ),
                "source_index": index,
                **(omitted_metadata or {}),
            }
        )
    for index, item in enumerate(omitted_download_metadata.values(), start=len(direct_pdf_metadata) + 1):
        date_str = find_statement_date(item.get("date"), item.get("name"))
        candidates.append(
            {
                "source": "har:download_response_metadata",
                "name": item.get("name") or "statement",
                "date": date_str,
                "statement_month": date_str[:7] if date_str else None,
                "document_identifier": item.get("document_identifier"),
                "pdf": None,
                "pdf_available": False,
                "pdf_payload_status": "download_response_body_omitted_from_har",
                "source_index": index,
                **item,
            }
        )
    return candidates


def run_manifest(manifest_path: Path, *, apply: bool) -> tuple[dict[str, Any], int]:
    manifest, manifest_error = load_json(manifest_path)
    target_month = target_statement_month(manifest if manifest else None)
    report: dict[str, Any] = {
        "job": "generated-mortgage-har-downloader",
        "generated_at": utc_now(),
        "manifest_path": str(manifest_path),
        "manifest_exists": manifest_path.exists(),
        "apply": apply,
        "target_month": target_month,
        "status": "review",
        "reason": None,
        "errors": [],
        "warnings": [],
        "downloaded_files": [],
        "skipped_files": [],
        "existing_target_month_files": [],
        "downloaded_target_month_files": [],
        "skipped_target_month_files": [],
        "target_month_existing_count": 0,
        "target_month_downloaded_count": 0,
        "target_month_skipped_count": 0,
        "target_month_downloadable_count": 0,
        "target_month_statement_available": False,
        "safe_to_run_automatically": True,
        "idempotent_skip": False,
        "idempotent_skip_reason": None,
    }
    if manifest_error or manifest is None:
        report.update(reason="manifest_unreadable", error=manifest_error)
        return report, 2

    property_name = str(manifest.get("property") or "unknown").strip() or "unknown"
    har_path = resolve_har_path(manifest)
    output_dir = resolve_output_dir(manifest)
    target_output_dir = resolve_target_output_dir(output_dir, target_month)
    report.update(
        {
            "property": property_name,
            "servicer_hint": manifest.get("servicer_hint"),
            "har_path": str(har_path),
            "har_path_exists": har_path.exists(),
            "output_dir": str(output_dir),
            "target_output_dir": str(target_output_dir),
            "configured_report_path": str(report_path) if (report_path := manifest_report_path(manifest)) else None,
            "target_statement_dir": manifest.get("target_statement_dir"),
            "endpoint_count": len(manifest.get("endpoints") or []),
        }
    )
    report["existing_target_month_files"] = existing_target_month_files(output_dir, target_month)
    report["target_month_existing_count"] = len(report["existing_target_month_files"])
    report["target_month_statement_available"] = report["target_month_existing_count"] > 0
    if not har_path.exists():
        report["reason"] = "har_missing"
        if report["target_month_statement_available"]:
            report["status"] = "ok"
            report["idempotent_skip"] = True
            report["idempotent_skip_reason"] = "target_month_statement_already_available_har_missing"
            report["warnings"].append(f"HAR path missing after target-month statement was already available: {har_path}")
            return report, 0
        report.update(
            safe_to_run_automatically=False,
            target_month_recapture_required=True,
            required_capture_quality="full_response_bodies",
            target_month_recapture_reason="har_missing",
            suggested_next_action="place_har_at_suggested_path",
        )
        report["errors"].append(f"HAR path missing: {har_path}")
        return report, 2
    har, har_error = load_json(har_path)
    if har_error or har is None:
        report.update(
            reason="har_unreadable",
            error=har_error,
            safe_to_run_automatically=False,
            target_month_recapture_required=True,
            required_capture_quality="full_response_bodies",
            target_month_recapture_reason="har_unreadable",
            suggested_next_action="recapture_full_response_har",
        )
        return report, 2

    candidates = collect_candidates(har)
    target_candidates = [item for item in candidates if item.get("statement_month") == target_month]
    downloadable = [item for item in target_candidates if item.get("pdf_available")]
    all_statement_months = candidate_months(candidates)
    downloadable_statement_months = candidate_months(candidates, pdf_available=True)
    metadata_only_statement_months = candidate_months(candidates, pdf_available=False)
    metadata_without_payload = [
        item
        for item in target_candidates
        if item.get("source") == "har:json_statement_metadata" and not item.get("pdf_available")
    ]
    html_viewer_shell_payloads = [
        item for item in target_candidates if item.get("pdf_payload_status") == "html_viewer_shell"
    ]
    omitted_har_download_bodies = [
        item for item in target_candidates if item.get("pdf_payload_status") == "download_response_body_omitted_from_har"
    ]
    invalid_pdf_payloads = [
        item
        for item in target_candidates
        if not item.get("pdf_available")
        and item.get("pdf_payload_status") not in {None, "metadata_without_embedded_pdf_payload"}
    ]
    report.update(
        {
            "candidate_count": len(candidates),
            "candidate_source_counts": source_counts(candidates),
            "available_statement_months": all_statement_months,
            "downloadable_statement_months": downloadable_statement_months,
            "metadata_only_statement_months": metadata_only_statement_months,
            "latest_statement_month": latest_month(all_statement_months),
            "latest_downloadable_statement_month": latest_month(downloadable_statement_months),
            "target_month_candidate_count": len(target_candidates),
            "target_month_statement_candidates": [candidate_summary(item) for item in target_candidates],
            "target_month_document_identifiers": document_identifiers(target_candidates),
            "target_month_downloadable_count": len(downloadable),
            "target_month_candidate_source_counts": source_counts(target_candidates),
            "target_month_downloadable_source_counts": source_counts(downloadable),
            "target_month_metadata_without_payload_count": len(metadata_without_payload),
            "target_month_html_viewer_shell_payload_count": len(html_viewer_shell_payloads),
            "target_month_har_download_response_body_omitted_count": len(omitted_har_download_bodies),
            "target_month_invalid_pdf_payload_count": len(invalid_pdf_payloads),
            "target_month_recapture_required": False,
            "required_capture_quality": None,
            "target_month_recapture_reason": None,
        }
    )

    used_names: set[str] = {Path(path).name for path in report["existing_target_month_files"]}
    for index, item in enumerate(target_candidates, start=1):
        date_str = str(item.get("date") or "")
        file_name = build_statement_filename(property_name, date_str, index)
        item_summary = {
            "name": file_name,
            "source": item.get("source"),
            "statement_date": date_str,
            "statement_month": item.get("statement_month"),
        }
        if item.get("pdf_payload_status"):
            item_summary["pdf_payload_status"] = item.get("pdf_payload_status")
        if item.get("document_identifier"):
            item_summary["document_identifier"] = item.get("document_identifier")
        if not item.get("pdf_available"):
            report["skipped_files"].append({**item_summary, "reason": "no_embedded_pdf_payload"})
            continue
        if file_name in used_names:
            report["skipped_files"].append({**item_summary, "reason": "already_exists"})
            continue
        if not apply:
            report["skipped_files"].append({**item_summary, "reason": "dry_run_apply_required"})
            continue
        target_output_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_output_dir / file_name
        file_path.write_bytes(item["pdf"])
        used_names.add(file_name)
        report["downloaded_files"].append({**item_summary, "path": str(file_path), "size": len(item["pdf"])})

    report["downloaded_target_month_files"] = [
        item for item in report["downloaded_files"] if item.get("statement_month") == target_month
    ]
    report["skipped_target_month_files"] = [
        item for item in report["skipped_files"] if item.get("statement_month") == target_month
    ]
    report["target_month_downloaded_count"] = len(report["downloaded_target_month_files"])
    report["target_month_skipped_count"] = len(report["skipped_target_month_files"])
    report["target_month_statement_available"] = (
        report["target_month_existing_count"] + report["target_month_downloaded_count"]
    ) > 0
    if report["target_month_existing_count"] > 0 and report["target_month_downloaded_count"] == 0:
        report["idempotent_skip"] = True
        report["idempotent_skip_reason"] = "target_month_statement_already_available"
    if report["target_month_statement_available"]:
        report["status"] = "ok"
        return report, 0
    if downloadable:
        report.update(status="review", reason="apply_required_to_write_target_month_statement")
        return report, 1
    if target_candidates:
        target_document_identifiers = document_identifiers(target_candidates)
        report.update(
            status="target_month_missing",
            reason="target_month_statement_pdf_payload_missing",
            safe_to_run_automatically=False,
            target_month_recapture_required=True,
            required_capture_quality="target_month_statement_pdf",
            target_month_recapture_reason=(
                "html_viewer_shell_without_pdf_payload"
                if html_viewer_shell_payloads
                else (
                    "download_response_body_omitted_from_har"
                    if omitted_har_download_bodies
                    else "metadata_without_embedded_pdf_payload"
                )
            ),
            suggested_next_action="recapture_target_month_statement_pdf",
            body_recapture_capture_method=BODY_RECAPTURE_CAPTURE_METHOD,
            body_recapture_capture_instruction=BODY_RECAPTURE_CAPTURE_INSTRUCTION,
            body_recapture_manual_har_export_warning=BODY_RECAPTURE_MANUAL_HAR_EXPORT_WARNING,
            expected_document_ids=target_document_identifiers,
            operator_next_action=(
                "Run the visible CDP capture helper, then in the authenticated visible portal open or "
                "download the target-month mortgage statement so Network.getResponseBody captures the "
                "PDF payload; rerun the downloader after capture."
            ),
        )
        report["errors"].append(f"Target-month statement PDF payload missing from HAR: {target_month}")
        return report, 1
    report.update(status="target_month_missing", reason="target_month_statement_unavailable")
    report["errors"].append(f"Target-month statement unavailable: {target_month}")
    return report, 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    apply = args.apply or os.environ.get("MORTGAGE_GENERATED_HAR_DOWNLOADER_APPLY") == "1"
    report, rc = run_manifest(args.manifest, apply=apply)
    explicit_report_path = args.report or resolve_report_path(os.environ.get("MORTGAGE_GENERATED_HAR_REPORT"))
    configured_report_path = resolve_report_path(report.get("configured_report_path"))
    persist_blocker_report = report.get("reason") in {
        "har_missing",
        "har_unreadable",
        "target_month_statement_pdf_payload_missing",
        "target_month_statement_unavailable",
    }
    report_path = explicit_report_path or (configured_report_path if apply or persist_blocker_report else None)
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report["report_path"] = str(report_path)
        report = write_json_report(report_path, report)
    print(json.dumps(report, indent=2, sort_keys=True), file=stdout)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
