#!/usr/bin/env python3
"""Generate a safe downloader scaffold from sanitized mortgage HAR analysis."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

SCRIPT_PATH = Path(__file__).absolute()
WORKSPACE_ROOT = SCRIPT_PATH.parents[1]
SCRIPTS_DIR = WORKSPACE_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import generated_mortgage_har_downloader as generated_har
from stable_json_report import stable_report_digest, write_json_report

DEFAULT_OUTPUT_DIR = WORKSPACE_ROOT / "reports" / "mortgage_downloader_scaffolds"
DEFAULT_STUB_DIR = WORKSPACE_ROOT / "scripts" / "generated_mortgage_downloaders"

ALLOWED_ENDPOINT_KEYS = {
    "kind",
    "host",
    "method",
    "path",
    "query_keys",
    "response_body_requirement_path",
    "status",
    "mime_type",
    "content_size",
    "has_embedded_response_body",
    "missing_response_body",
}


def slugify(value: object) -> str:
    text = " ".join(str(value or "").strip().casefold().split())
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "unknown"


def valid_year_month(value: str) -> bool:
    if not (len(value) == 7 and value[:4].isdigit() and value[4] == "-" and value[5:].isdigit()):
        return False
    month = int(value[5:])
    return 1 <= month <= 12


def target_statement_month(configured: str | None = None) -> str:
    configured = str(
        configured
        or os.environ.get("MORTGAGE_STATEMENT_TARGET_MONTH")
        or os.environ.get("BASELANE_MORTGAGE_STATEMENT_TARGET_MONTH")
        or os.environ.get("MORTGAGE_WORKFLOW_TARGET_MONTH")
        or os.environ.get("BASELANE_MONTHLY_TARGET_STAMP")
        or ""
    ).strip()
    if valid_year_month(configured):
        return configured
    return datetime.now(timezone.utc).strftime("%Y-%m")


def load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "JSON root is not an object"
    return data, None


def safe_endpoint(item: dict[str, Any]) -> dict[str, Any]:
    safe = {key: item.get(key) for key in ALLOWED_ENDPOINT_KEYS if key in item}
    if not isinstance(safe.get("query_keys"), list):
        safe["query_keys"] = []
    safe["query_keys"] = [str(key) for key in safe["query_keys"] if str(key or "").strip()]
    return safe


def actionable_endpoints(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    endpoints = []
    for item in analysis.get("candidate_endpoints") or []:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "auth":
            continue
        if item.get("has_embedded_response_body") is not True:
            continue
        endpoints.append(safe_endpoint(item))
    return endpoints


def har_statement_payload_summary(analysis: dict[str, Any]) -> dict[str, Any]:
    har_path_text = str(analysis.get("har_path") or "").strip()
    har_path = Path(har_path_text) if har_path_text else None
    summary: dict[str, Any] = {
        "har_payload_check_path": har_path_text or None,
        "har_payload_check_exists": har_path.exists() if har_path else False,
        "statement_document_candidate_count": 0,
        "statement_document_payload_count": 0,
        "statement_document_metadata_only_count": 0,
        "statement_document_payload_months": [],
        "statement_document_metadata_only_months": [],
    }
    if not har_path or not har_path.exists():
        return summary
    har, error = generated_har.load_json(har_path)
    if error or har is None:
        summary.update(har_payload_check_error=error)
        return summary
    candidates = generated_har.collect_candidates(har)
    payloads = [item for item in candidates if item.get("pdf_available") is True]
    metadata_only = [item for item in candidates if item.get("pdf_available") is not True]
    summary.update(
        {
            "statement_document_candidate_count": len(candidates),
            "statement_document_payload_count": len(payloads),
            "statement_document_metadata_only_count": len(metadata_only),
            "statement_document_payload_months": sorted(
                {str(item.get("statement_month")) for item in payloads if item.get("statement_month")}
            ),
            "statement_document_metadata_only_months": sorted(
                {str(item.get("statement_month")) for item in metadata_only if item.get("statement_month")}
            ),
        }
    )
    return summary


def resolved_cli_path(path: Path) -> Path:
    return Path(os.path.abspath(path))


def stub_path_expression(path: Path) -> str:
    path = resolved_cli_path(path)
    try:
        relative = path.relative_to(WORKSPACE_ROOT)
    except ValueError:
        return f"Path({str(path)!r})"
    return f"WORKSPACE_ROOT / Path({relative.as_posix()!r})"


def build_stub_text(manifest_path: Path, property_name: str) -> str:
    runtime_path = WORKSPACE_ROOT / "scripts" / "generated_mortgage_har_downloader.py"
    manifest_expr = stub_path_expression(manifest_path)
    try:
        resolved_cli_path(manifest_path).relative_to(WORKSPACE_ROOT)
    except ValueError:
        runtime_expr = f"Path({str(runtime_path)!r})"
    else:
        runtime_expr = stub_path_expression(runtime_path)
    return f'''#!/usr/bin/env python3
"""Generated mortgage downloader scaffold for {property_name}."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

DEFAULT_WORKSPACE_ROOT = Path(__file__).absolute().parents[2]
WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT") or DEFAULT_WORKSPACE_ROOT).resolve()
MANIFEST_PATH = {manifest_expr}
RUNTIME_PATH = {runtime_expr}


def main() -> int:
    spec = importlib.util.spec_from_file_location("generated_mortgage_har_downloader", RUNTIME_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load generated mortgage runtime: {{RUNTIME_PATH}}")
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    return runtime.main(["--manifest", str(MANIFEST_PATH)])


if __name__ == "__main__":
    raise SystemExit(main())
'''


def with_idempotency_digest(report: dict[str, Any]) -> dict[str, Any]:
    report = dict(report)
    report["idempotency_digest"] = stable_report_digest(report)
    return report


def build_scaffold(
    analysis_path: Path,
    *,
    output_dir: Path,
    stub_dir: Path,
    write_stub: bool,
    write_review_manifest: bool = False,
    target_month: str | None = None,
) -> dict[str, Any]:
    data, error = load_json(analysis_path)
    report: dict[str, Any] = {
        "job": "mortgage-workflow-downloader-scaffold",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "analysis_report": str(analysis_path),
        "analysis_report_exists": analysis_path.exists(),
        "safe_to_run_automatically": True,
    }
    if error or data is None:
        report.update(
            {
                "status": "review",
                "reason": "analysis_report_unreadable",
                "error": error,
                "safe_to_build_downloader_automatically": False,
            }
        )
        return with_idempotency_digest(report)

    property_name = str(data.get("property") or "").strip() or analysis_path.stem
    prop_slug = slugify(property_name)
    target_month = target_statement_month(target_month)
    endpoints = actionable_endpoints(data)
    payload_summary = har_statement_payload_summary(data)
    ready = (
        data.get("status") == "ok"
        and bool(endpoints)
        and int(payload_summary.get("statement_document_payload_count") or 0) > 0
    )
    manifest_path = output_dir / f"{prop_slug}_downloader_scaffold_manifest.json"
    registry_entry_path = output_dir / f"{prop_slug}_mortgage_statement_downloader_registry_entry.json"
    stub_path = stub_dir / f"download_{prop_slug}_statements.py"
    proposed_config_entry = {
        "id": f"generated-{prop_slug}",
        "enabled": False,
        "property": property_name,
        "servicer": data.get("servicer_hint"),
        "co_owner_paid_mortgage": True,
        "env": {
            "MORTGAGE_GENERATED_HAR_DOWNLOADER_APPLY": "1",
            "MORTGAGE_GENERATED_HAR_PATH": str(data.get("har_path") or ""),
            "MORTGAGE_GENERATED_HAR_TARGET_MONTH_DEFAULT_OFFSET": "-1",
        },
        "runtime": "python",
        "script": str(stub_path.relative_to(WORKSPACE_ROOT)) if stub_path.is_relative_to(WORKSPACE_ROOT) else str(stub_path),
        "report": f"reports/{prop_slug}_statements_download_report.json",
        "notes": f"Generated HAR-backed scaffold only. Disabled until reviewed. Use MORTGAGE_GENERATED_HAR_DOWNLOADER_APPLY=1 only with sanitized full-response HAR evidence after verifying {target_month} target-month selection. Registry runs resolve the statement month from the workflow target month with MORTGAGE_GENERATED_HAR_TARGET_MONTH_DEFAULT_OFFSET=-1.",
    }
    report.update(
        {
            "property": property_name,
            "servicer_hint": data.get("servicer_hint"),
            "portal_url": data.get("portal_url"),
            "target_statement_dir": data.get("target_statement_dir"),
            "target_statement_month": target_month,
            "har_path": data.get("har_path"),
            "analysis_status": data.get("status"),
            "analysis_reason": data.get("reason"),
            "analysis_suggested_next_action": data.get("suggested_next_action"),
            "endpoint_count": len(endpoints),
            "endpoints": endpoints,
            **payload_summary,
            "manifest_path": str(manifest_path),
            "registry_entry_path": str(registry_entry_path),
            "registry_entry_written": False,
            "stub_path": str(stub_path),
            "write_stub": write_stub,
            "proposed_config_entry": proposed_config_entry,
            "implementation_steps": [
                "Use the generated HAR-backed dry run to verify that captured full-response evidence contains target-month statement PDFs.",
                f"Select only the configured target month ({target_month}) before writing PDFs.",
                "Keep the registry entry disabled until dry-run output proves target-month availability and output naming.",
                "Enable apply mode only after reviewing the sanitized full-response HAR; do not replay captured auth tokens.",
                "Replace the generated HAR-backed scaffold with a servicer-specific authenticated downloader when live automation is ready.",
            ],
            "safe_to_build_downloader_automatically": ready,
        }
    )
    if not ready:
        reason = data.get("reason") or "embedded_statement_pdf_payload_missing"
        next_action = data.get("suggested_next_action") or "capture_target_month_statement"
        if data.get("status") == "ok" and int(payload_summary.get("statement_document_payload_count") or 0) <= 0:
            next_action = "capture_target_month_statement"
        report.update(
            {
                "status": "review",
                "reason": reason,
                "next_action": next_action,
            }
        )
        if write_review_manifest:
            manifest = {
                **report,
                "manifest_written": True,
                "registry_entry_written": False,
                "stub_written": write_stub,
                "safe_to_register_automatically": False,
                "proposed_config_entry": proposed_config_entry,
            }
            output_dir.mkdir(parents=True, exist_ok=True)
            manifest = write_json_report(manifest_path, with_idempotency_digest(manifest))
            if write_stub:
                stub_dir.mkdir(parents=True, exist_ok=True)
                stub_path.write_text(build_stub_text(manifest_path, property_name), encoding="utf-8")
                stub_path.chmod(0o755)
            report.update(
                {
                    "manifest_written": True,
                    "registry_entry_written": False,
                    "stub_written": write_stub,
                    "safe_to_register_automatically": False,
                }
            )
        return with_idempotency_digest(report)

    registry_entry = {
        **proposed_config_entry,
        "enabled": False,
        "notes": f"Generated disabled HAR-backed registry entry. Review dry-run output and enable apply mode only after verifying {target_month} target-month statement selection. Registry runs resolve the statement month from the workflow target month with MORTGAGE_GENERATED_HAR_TARGET_MONTH_DEFAULT_OFFSET=-1.",
    }
    manifest = {
        **report,
        "status": "ok",
        "reason": None,
        "registry_entry": registry_entry,
        "manifest_written": True,
        "registry_entry_written": True,
        "stub_written": write_stub,
        "safe_to_register_automatically": False,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = with_idempotency_digest(manifest)
    manifest = write_json_report(manifest_path, manifest)
    registry_entry_path.write_text(json.dumps(registry_entry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if write_stub:
        stub_dir.mkdir(parents=True, exist_ok=True)
        stub_path.write_text(build_stub_text(manifest_path, property_name), encoding="utf-8")
        stub_path.chmod(0o755)
    report.update(
        {
            "status": "ok",
            "reason": None,
            "manifest_written": True,
            "registry_entry_written": True,
            "stub_written": write_stub,
            "safe_to_register_automatically": False,
            "next_action": "implement_generated_scaffold",
        }
    )
    return with_idempotency_digest(report)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-report", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--stub-dir", type=Path, default=DEFAULT_STUB_DIR)
    parser.add_argument("--write-stub", action="store_true")
    parser.add_argument(
        "--write-review-manifest",
        action="store_true",
        help="Persist a non-registerable review manifest for incomplete captures so generated runtimes use current HAR evidence.",
    )
    parser.add_argument("--target-month", help="Target statement month, YYYY-MM, to persist into generated manifests")
    parser.add_argument("--report", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    report = build_scaffold(
        args.analysis_report,
        output_dir=args.output_dir,
        stub_dir=args.stub_dir,
        write_stub=args.write_stub,
        write_review_manifest=args.write_review_manifest,
        target_month=args.target_month,
    )
    if args.report:
        report = write_json_report(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True), file=stdout)
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
