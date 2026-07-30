#!/usr/bin/env python3
"""Analyze mortgage workflow HAR captures without replaying credentials."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import parse_qsl, urlparse

SCRIPT_PATH = Path(__file__).absolute()
WORKSPACE_ROOT = SCRIPT_PATH.parents[1]
SCRIPTS_DIR = WORKSPACE_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import audit_mortgage_downloader_coverage as coverage
import generated_mortgage_har_downloader as generated_har
from stable_json_report import write_json_report

DEFAULT_INTAKE = WORKSPACE_ROOT / "config" / "mortgage_downloader_intake.json"
DEFAULT_REPORT_DIR = WORKSPACE_ROOT / "reports"

STATEMENT_KEYWORDS = ("statement", "statements", "estatement", "estatements")
DOCUMENT_KEYWORDS = ("document", "documents", "/doc", "/docs")
DOWNLOAD_KEYWORDS = ("download", "export", "print")
AUTH_KEYWORDS = ("login", "logout", "auth", "oauth", "token", "session", "saml", "mfa", "otp")
SENSITIVE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{32,}$")
STATIC_ASSET_SUFFIXES = (
    ".css",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".mjs",
    ".png",
    ".svg",
    ".ttf",
    ".woff",
    ".woff2",
)


def load_intake(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    properties = data.get("properties") if isinstance(data, dict) else None
    if not isinstance(properties, list):
        raise ValueError("intake properties is not a list")
    return [item for item in properties if isinstance(item, dict)]


def find_property(items: list[dict[str, Any]], property_name: str) -> dict[str, Any] | None:
    target = coverage.normalize_property(property_name)
    for item in items:
        if coverage.normalize_property(item.get("property")) == target:
            return item
    return None


def first_existing_evidence(item: dict[str, Any]) -> str | None:
    for value in item.get("workflow_evidence") or []:
        path = Path(str(value))
        if path.exists():
            return str(path)
    return None


def workflow_evidence_paths(item: dict[str, Any] | None) -> list[Path]:
    paths: list[Path] = []
    if not item:
        return paths
    for value in item.get("workflow_evidence") or []:
        text = str(value or "").strip()
        if text:
            paths.append(Path(text))
    return paths


def existing_workflow_evidence_paths(item: dict[str, Any] | None) -> list[Path]:
    return [path for path in workflow_evidence_paths(item) if path.exists()]


def int_field(data: dict[str, Any], key: str) -> int:
    try:
        return int(data.get(key) or 0)
    except Exception:
        return 0


def workflow_evidence_analysis_score(analysis: dict[str, Any]) -> int:
    if analysis.get("har_path_exists") is not True:
        return -1
    score = 0
    if analysis.get("status") == "ok":
        score += 100_000
    score += int_field(analysis, "statement_document_payload_count") * 5_000
    score += int_field(analysis, "candidate_pdf_response_count") * 2_000
    score += int_field(analysis, "actionable_missing_response_body_count") * 1_000
    score += int_field(analysis, "statement_document_metadata_only_count") * 10
    score += int_field(analysis, "candidate_endpoint_count")
    return score


def select_workflow_evidence_analysis(analyses: list[dict[str, Any]]) -> dict[str, Any] | None:
    existing = [item for item in analyses if item.get("har_path_exists") is True]
    if not existing:
        return None
    return max(
        existing,
        key=lambda item: (
            workflow_evidence_analysis_score(item),
            str(item.get("har_path") or ""),
        ),
    )


def workflow_evidence_analysis_summary(analysis: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "har_path",
        "har_path_exists",
        "har_selection_reason",
        "status",
        "reason",
        "suggested_next_action",
        "required_capture_quality",
        "candidate_endpoint_count",
        "candidate_pdf_response_count",
        "candidate_json_response_count",
        "actionable_missing_response_body_count",
        "missing_response_body_paths",
        "statement_document_candidate_count",
        "statement_document_metadata_only_count",
        "statement_document_payload_count",
        "statement_document_months",
        "statement_document_metadata_only_months",
        "statement_document_payload_months",
        "safe_to_build_downloader_automatically",
    ]
    return {key: analysis.get(key) for key in keys if key in analysis}


def analyze_workflow_evidence(
    item: dict[str, Any] | None,
    *,
    property_name: str | None = None,
) -> list[dict[str, Any]]:
    analyses: list[dict[str, Any]] = []
    for index, path in enumerate(workflow_evidence_paths(item), start=1):
        analyses.append(
            analyze_har(
                path,
                property_name=property_name,
                item=item,
                selection_reason=f"workflow_evidence[{index}]",
            )
        )
    return analyses


def default_har_path(item: dict[str, Any] | None, property_name: str | None) -> tuple[str | None, str]:
    if not item:
        return None, "har_argument_required"
    existing = first_existing_evidence(item)
    if existing:
        return existing, "first_existing_workflow_evidence"
    prop = str(item.get("property") or property_name or "")
    return coverage.suggested_workflow_har_path(prop, item.get("portal_url")), "suggested_workflow_har_path"


def default_report_path(report_dir: Path, property_name: str | None, har_path: Path | None) -> Path:
    if property_name:
        return report_dir / f"mortgage_workflow_har_intake_analysis_{coverage.slugify(property_name)}.json"
    if har_path:
        return report_dir / f"mortgage_workflow_har_intake_analysis_{coverage.slugify(har_path.stem)}.json"
    return report_dir / "mortgage_workflow_har_intake_analysis.json"


def scrub_path_segment(segment: str) -> str:
    if len(segment) > 80:
        return "[redacted-long-segment]"
    if "." in segment and len(segment) > 40:
        return "[redacted-sensitive-segment]"
    if SENSITIVE_SEGMENT_RE.match(segment) and not segment.isdigit():
        return "[redacted-sensitive-segment]"
    return segment


def sanitized_path(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    parts = [scrub_path_segment(part) for part in path.split("/")]
    return "/".join(parts) or "/"


def query_keys(url: str) -> list[str]:
    parsed = urlparse(url)
    keys = {key for key, _value in parse_qsl(parsed.query, keep_blank_values=True) if key}
    return sorted(keys)


def safe_query_key(key: object) -> str | None:
    text = str(key or "").strip()
    if not text:
        return None
    if len(text) > 64:
        return "[redacted-long-key]"
    cleaned = re.sub(r"[^A-Za-z0-9_.~-]+", "_", text)
    return cleaned or None


def include_query_keys_in_requirement_path(path: str, kind: object, keys: list[str]) -> bool:
    if not keys:
        return False
    lowered = path.casefold()
    if lowered.endswith(("lisviewdoc.aspx", "viewdoc.aspx")):
        return True
    return str(kind or "") == "pdf" and lowered.endswith((".aspx", ".ashx"))


def response_body_requirement_path(path: str, kind: object, keys: list[str]) -> str:
    if not include_query_keys_in_requirement_path(path, kind, keys):
        return path
    safe_keys = [key for key in (safe_query_key(item) for item in keys) if key]
    if not safe_keys:
        return path
    return f"{path}?{'&'.join(sorted(set(safe_keys)))}"


def safe_host(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.netloc or "").lower()


def content_state(response: dict[str, Any]) -> dict[str, Any]:
    content = response.get("content") if isinstance(response.get("content"), dict) else {}
    mime_type = str(content.get("mimeType") or response.get("mimeType") or "").split(";")[0].strip().lower()
    encoding = str(content.get("encoding") or "").strip().lower()
    size = content.get("size")
    if not isinstance(size, int):
        body_text = content.get("text")
        size = len(body_text) if isinstance(body_text, str) else 0
    body_text = content.get("text")
    has_body = isinstance(body_text, str) and body_text != ""
    pdf_payload_status = None
    if mime_type == "application/pdf" and has_body:
        pdf_payload_status = generated_har.pdf_payload_status(body_text, encoding)
        has_body = pdf_payload_status == "available"
    return {
        "mime_type": mime_type,
        "content_size": size,
        "has_embedded_response_body": has_body,
        "missing_response_body": bool(response.get("status") == 200 and size and not has_body),
        "pdf_payload_status": pdf_payload_status,
    }


def endpoint_kind(path: str, mime_type: str) -> str | None:
    lowered = path.casefold()
    if mime_type != "application/pdf" and lowered.endswith(STATIC_ASSET_SUFFIXES):
        return None
    if any(keyword in lowered for keyword in AUTH_KEYWORDS):
        return "auth"
    if "pdf" in mime_type or lowered.endswith(".pdf"):
        return "pdf"
    if any(keyword in lowered for keyword in STATEMENT_KEYWORDS):
        return "statement_index"
    if any(keyword in lowered for keyword in DOCUMENT_KEYWORDS):
        return "document_detail"
    if any(keyword in lowered for keyword in DOWNLOAD_KEYWORDS):
        return "download"
    return None


def counter_dict(counter: Counter[str]) -> dict[str, int]:
    return dict(sorted(counter.items(), key=lambda item: item[0]))


def unique_sorted_strings(values: list[object]) -> list[str]:
    return sorted({str(value).strip() for value in values if str(value or "").strip()})


def statement_document_metadata_details(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for item in items:
        detail = {
            "source": item.get("source"),
            "source_index": item.get("source_index"),
            "name": item.get("name"),
            "date": item.get("date"),
            "statement_month": item.get("statement_month"),
            "document_identifier": item.get("document_identifier"),
            "pdf_payload_status": item.get("pdf_payload_status"),
        }
        details.append({key: value for key, value in detail.items() if value not in (None, "", [], {})})
    return details


def statement_document_identifiers(details: list[dict[str, Any]]) -> list[str]:
    identifiers: list[str] = []
    for item in details:
        text = str(item.get("document_identifier") or "").strip()
        if text and text not in identifiers:
            identifiers.append(text)
    return identifiers


def expected_document_identifier_list(values: list[str] | None) -> list[str]:
    identifiers: list[str] = []
    for item in values or []:
        text = str(item or "").strip()
        if text and text not in identifiers:
            identifiers.append(text)
    return identifiers


def statement_details_for_month(details: list[dict[str, Any]], target_month: str | None) -> list[dict[str, Any]]:
    if not target_month:
        return []
    return [item for item in details if item.get("statement_month") == target_month]


def target_document_payload_coverage(
    *,
    target_month: str | None,
    metadata_details: list[dict[str, Any]],
    payload_details: list[dict[str, Any]],
    expected_document_identifiers: list[str] | None = None,
) -> dict[str, Any]:
    if not target_month:
        return {}
    target_metadata_details = statement_details_for_month(metadata_details, target_month)
    target_payload_details = statement_details_for_month(payload_details, target_month)
    explicit_expected_ids = expected_document_identifier_list(expected_document_identifiers)
    expected_ids = explicit_expected_ids or statement_document_identifiers(target_metadata_details)
    payload_ids = statement_document_identifiers(target_payload_details)
    missing_ids = [item for item in expected_ids if item not in payload_ids]
    if expected_ids and not missing_ids:
        status = "ok"
    elif expected_ids:
        status = "missing_payload"
    elif target_payload_details:
        status = "ok_payload_without_metadata_identifier"
    else:
        status = "no_target_document_identifiers"
    return {
        "target_statement_month": target_month,
        "target_statement_document_metadata_only_details": target_metadata_details,
        "target_statement_document_payload_details": target_payload_details,
        "target_month_document_identifiers": expected_ids,
        "target_month_payload_document_identifiers": payload_ids,
        "target_month_missing_payload_document_identifiers": missing_ids,
        "target_month_document_identifier_payload_coverage_status": status,
    }


def parse_request_json(entry: dict[str, Any]) -> dict[str, Any]:
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    post_data = request.get("postData") if isinstance(request.get("postData"), dict) else {}
    text = post_data.get("text")
    if not isinstance(text, str) or not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def request_document_identifier(entry: dict[str, Any]) -> str | None:
    data = parse_request_json(entry)
    for key in ("documentId", "documentIdentifier", "document_id", "id"):
        text = str(data.get(key) or "").strip()
        if text:
            return text
    return None


def response_body_requirements(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_path: dict[str, dict[str, Any]] = {}
    for item in candidates:
        if not item.get("missing_response_body"):
            continue
        path = str(item.get("response_body_requirement_path") or item.get("path") or "").strip()
        if not path:
            continue
        existing = by_path.setdefault(
            path,
            {
                "path": path,
                "roles": [],
                "missing_response_body_count": 0,
                "required_capture_quality": "full_response_body",
            },
        )
        role = str(item.get("kind") or "workflow_response")
        if role not in existing["roles"]:
            existing["roles"].append(role)
        existing["missing_response_body_count"] += 1
    return [
        {
            **item,
            "roles": sorted(item["roles"]),
        }
        for item in sorted(by_path.values(), key=lambda value: value["path"])
    ]


def response_body_requirement_progress(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_path: dict[str, dict[str, Any]] = {}
    for item in candidates:
        if item.get("kind") == "auth":
            continue
        path = str(item.get("response_body_requirement_path") or item.get("path") or "").strip()
        if not path:
            continue
        existing = by_path.setdefault(
            path,
            {
                "path": path,
                "roles": [],
                "source_candidate_count": 0,
                "captured_count": 0,
                "missing_count": 0,
                "required_capture_quality": "full_response_body",
            },
        )
        role = str(item.get("kind") or "workflow_response")
        if role not in existing["roles"]:
            existing["roles"].append(role)
        existing["source_candidate_count"] += 1
        if item.get("has_embedded_response_body"):
            existing["captured_count"] += 1
        if item.get("missing_response_body"):
            existing["missing_count"] += 1

    progress = []
    for item in sorted(by_path.values(), key=lambda value: value["path"]):
        captured_count = int(item["captured_count"])
        missing_count = int(item["missing_count"])
        source_candidate_count = int(item["source_candidate_count"])
        progress.append(
            {
                "path": item["path"],
                "roles": sorted(item["roles"]),
                "source_candidate_count": source_candidate_count,
                "captured_count": captured_count,
                "missing_count": missing_count,
                "satisfied": source_candidate_count > 0 and missing_count == 0 and captured_count > 0,
                "required_capture_quality": item["required_capture_quality"],
            }
        )
    return progress


def target_document_response_body_gap(
    candidates: list[dict[str, Any]],
    expected_document_identifiers: list[str] | None,
) -> dict[str, Any]:
    expected_ids = expected_document_identifier_list(expected_document_identifiers)
    if not expected_ids:
        return {}
    expected = set(expected_ids)
    target_candidates = [
        item for item in candidates if str(item.get("document_identifier") or "").strip() in expected
    ]
    target_missing = [item for item in target_candidates if item.get("missing_response_body")]
    path_counts = Counter(
        str(item.get("response_body_requirement_path") or item.get("path") or "")
        for item in target_missing
    )
    path_counts.pop("", None)
    return {
        "target_expected_document_ids": expected_ids,
        "target_expected_document_response_body_candidate_count": len(target_candidates),
        "target_expected_document_missing_response_body_count": len(target_missing),
        "target_expected_document_missing_response_body_paths": sorted(path_counts),
        "target_expected_document_missing_response_body_path_counts": counter_dict(path_counts),
        "target_expected_document_response_body_requirements": response_body_requirements(target_missing),
        "target_expected_document_response_body_requirement_progress": response_body_requirement_progress(
            target_candidates
        ),
    }


def iter_har_entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    log = data.get("log") if isinstance(data.get("log"), dict) else {}
    entries = log.get("entries") if isinstance(log.get("entries"), list) else []
    return [entry for entry in entries if isinstance(entry, dict)]


def analyze_har(
    har_path: Path | None,
    *,
    property_name: str | None = None,
    item: dict[str, Any] | None = None,
    selection_reason: str | None = None,
    target_month: str | None = None,
    expected_document_identifiers: list[str] | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "job": "mortgage-workflow-har-intake-analysis",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "property": property_name or (str(item.get("property") or "") if item else None),
        "servicer_hint": item.get("servicer_hint") if item else None,
        "portal_url": item.get("portal_url") if item else None,
        "target_statement_dir": item.get("target_statement_dir") if item else None,
        "har_path": str(har_path) if har_path else None,
        "har_path_exists": bool(har_path and har_path.exists()),
        "har_selection_reason": selection_reason,
        "safe_to_run_automatically": True,
        "safe_to_build_downloader_automatically": False,
    }
    if not har_path:
        report.update(
            {
                "status": "review",
                "reason": "har_argument_required",
                "suggested_next_action": "provide_har_path",
            }
        )
        return report
    if not har_path.exists():
        report.update(
            {
                "status": "review",
                "reason": "har_missing",
                "suggested_next_action": "place_har_at_suggested_path",
            }
        )
        return report
    try:
        data = json.loads(har_path.read_text(encoding="utf-8"))
    except Exception as exc:
        report.update(
            {
                "status": "review",
                "reason": "har_unreadable",
                "error": str(exc),
                "suggested_next_action": "export_valid_har",
            }
        )
        return report
    if not isinstance(data, dict):
        report.update(
            {
                "status": "review",
                "reason": "har_root_not_object",
                "suggested_next_action": "export_valid_har",
            }
        )
        return report

    entries = iter_har_entries(data)
    statement_documents = generated_har.collect_candidates(data)
    statement_document_payloads = [
        item for item in statement_documents if item.get("pdf_available") is True
    ]
    statement_document_metadata_only = [
        item for item in statement_documents if item.get("pdf_available") is not True
    ]
    statement_document_metadata_only_details = statement_document_metadata_details(
        statement_document_metadata_only
    )
    statement_document_payload_details = statement_document_metadata_details(statement_document_payloads)
    host_counts: Counter[str] = Counter()
    method_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    content_type_counts: Counter[str] = Counter()
    candidates: list[dict[str, Any]] = []
    embedded_body_count = 0
    missing_body_count = 0

    for entry in entries:
        request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
        response = entry.get("response") if isinstance(entry.get("response"), dict) else {}
        url = str(request.get("url") or "")
        method = str(request.get("method") or "GET").upper()
        status = response.get("status")
        path = sanitized_path(url)
        keys = query_keys(url)
        state = content_state(response)
        host = safe_host(url)
        if host:
            host_counts[host] += 1
        method_counts[method] += 1
        if isinstance(status, int):
            status_counts[str(status)] += 1
        if state["mime_type"]:
            content_type_counts[state["mime_type"]] += 1
        if state["has_embedded_response_body"]:
            embedded_body_count += 1
        if state["missing_response_body"]:
            missing_body_count += 1

        kind = endpoint_kind(path, state["mime_type"])
        if not kind:
            continue
        if isinstance(status, int) and not (200 <= status < 400):
            continue
        requirement_path = response_body_requirement_path(path, kind, keys)
        document_identifier = request_document_identifier(entry)
        candidates.append(
            {
                "kind": kind,
                "host": host,
                "method": method,
                "path": path,
                "query_keys": keys,
                "response_body_requirement_path": requirement_path,
                "status": status,
                "mime_type": state["mime_type"] or None,
                "content_size": state["content_size"],
                "has_embedded_response_body": state["has_embedded_response_body"],
                "missing_response_body": state["missing_response_body"],
                "pdf_payload_status": state.get("pdf_payload_status"),
                "document_identifier": document_identifier,
            }
        )

    actionable = [item for item in candidates if item.get("kind") != "auth"]
    actionable_with_body = [item for item in actionable if item.get("has_embedded_response_body")]
    actionable_missing_body = [item for item in actionable if item.get("missing_response_body")]
    missing_body_path_counts = Counter(
        str(item.get("response_body_requirement_path") or item.get("path") or "")
        for item in actionable_missing_body
    )
    missing_body_path_counts.pop("", None)
    requirements = response_body_requirements(actionable_missing_body)
    requirement_progress = response_body_requirement_progress(actionable)
    if actionable_with_body and statement_document_payloads:
        status = "ok"
        reason = None
        next_action = "build_servicer_downloader"
        required_capture_quality = None
    elif actionable_with_body:
        status = "review"
        reason = "embedded_statement_pdf_payload_missing"
        next_action = "capture_target_month_statement"
        required_capture_quality = "target_month_statement_pdf"
    elif actionable_missing_body:
        status = "review"
        reason = "candidate_endpoints_missing_response_bodies"
        next_action = "capture_full_response_bodies"
        required_capture_quality = "full_response_bodies"
    else:
        status = "review"
        reason = "statement_workflow_endpoints_not_identified"
        next_action = "collect_more_workflow_evidence"
        required_capture_quality = "full_response_bodies"

    report.update(
        {
            "status": status,
            "reason": reason,
            "entry_count": len(entries),
            "host_counts": counter_dict(host_counts),
            "method_counts": counter_dict(method_counts),
            "status_counts": counter_dict(status_counts),
            "content_type_counts": counter_dict(content_type_counts),
            "embedded_response_body_count": embedded_body_count,
            "missing_response_body_count": missing_body_count,
            "actionable_missing_response_body_count": len(actionable_missing_body),
            "missing_response_body_paths": sorted(missing_body_path_counts),
            "missing_response_body_path_counts": counter_dict(missing_body_path_counts),
            "response_body_requirements": requirements,
            "response_body_requirement_progress": requirement_progress,
            "statement_document_candidate_count": len(statement_documents),
            "statement_document_metadata_only_count": len(statement_document_metadata_only),
            "statement_document_payload_count": len(statement_document_payloads),
            "statement_document_months": unique_sorted_strings(
                [item.get("statement_month") for item in statement_documents]
            ),
            "statement_document_payload_months": unique_sorted_strings(
                [item.get("statement_month") for item in statement_document_payloads]
            ),
            "statement_document_metadata_only_months": unique_sorted_strings(
                [item.get("statement_month") for item in statement_document_metadata_only]
            ),
            "statement_document_metadata_only_details": statement_document_metadata_only_details,
            "statement_document_payload_details": statement_document_payload_details,
            **target_document_payload_coverage(
                target_month=target_month,
                metadata_details=statement_document_metadata_only_details,
                payload_details=statement_document_payload_details,
                expected_document_identifiers=expected_document_identifiers,
            ),
            **target_document_response_body_gap(actionable, expected_document_identifiers),
            "required_capture_quality": required_capture_quality,
            "candidate_endpoint_count": len(candidates),
            "candidate_endpoints": candidates,
            "candidate_statement_endpoint_count": sum(
                1 for item in candidates if item.get("kind") == "statement_index"
            ),
            "candidate_document_endpoint_count": sum(
                1 for item in candidates if item.get("kind") == "document_detail"
            ),
            "candidate_pdf_response_count": sum(1 for item in candidates if item.get("kind") == "pdf"),
            "candidate_json_response_count": sum(
                1 for item in candidates if str(item.get("mime_type") or "").endswith("json")
            ),
            "suggested_next_action": next_action,
            "safe_to_build_downloader_automatically": status == "ok",
        }
    )
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--property", help="Property name from config/mortgage_downloader_intake.json")
    parser.add_argument("--har", type=Path, help="HAR path to analyze")
    parser.add_argument("--intake", type=Path, default=DEFAULT_INTAKE)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--target-month", help="Expected statement month to verify as YYYY-MM")
    parser.add_argument(
        "--expected-document-id",
        action="append",
        default=[],
        help="Expected target-month document identifier; repeatable.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    item = None
    if args.property:
        items = load_intake(args.intake)
        item = find_property(items, args.property)
        if not item:
            print(f"Property not found in intake: {args.property}", file=sys.stderr)
            return 2
    selection_reason = "explicit_har_argument" if args.har else None
    har_path = args.har
    if not har_path:
        selected, selection_reason = default_har_path(item, args.property)
        har_path = Path(selected) if selected else None
    report_path = args.report or default_report_path(args.report_dir, args.property, har_path)
    report = analyze_har(
        har_path,
        property_name=args.property,
        item=item,
        selection_reason=selection_reason,
        target_month=args.target_month,
        expected_document_identifiers=args.expected_document_id,
    )
    report["report_path"] = str(report_path)
    report = write_json_report(report_path, report)
    print(json.dumps(report, indent=2, sort_keys=True), file=stdout)
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
