#!/usr/bin/env python3
"""Create a per-property workflow-evidence capture handoff for mortgage downloaders."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

SCRIPT_PATH = Path(__file__).absolute()
WORKSPACE_ROOT = SCRIPT_PATH.parents[1]
SCRIPTS_DIR = WORKSPACE_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import audit_mortgage_downloader_coverage as coverage
import mortgage_workflow_har_intake_analyzer as analyzer
from stable_json_report import stable_report_digest

DEFAULT_INTAKE = WORKSPACE_ROOT / "config" / "mortgage_downloader_intake.json"
DEFAULT_REGISTRY = WORKSPACE_ROOT / "config" / "mortgage_statement_downloaders.json"
DEFAULT_REPORT_DIR = WORKSPACE_ROOT / "reports"


def is_citadel_item(item: dict[str, Any] | None) -> bool:
    marker = " ".join(
        str((item or {}).get(key) or "")
        for key in ("property", "servicer_hint", "portal_url")
    ).casefold()
    return "citadel" in marker or "loansphere" in marker or "bkiconnect" in marker


def valid_year_month(value: str) -> bool:
    if not (len(value) == 7 and value[:4].isdigit() and value[4] == "-" and value[5:].isdigit()):
        return False
    month = int(value[5:])
    return 1 <= month <= 12


def add_months(month: str, offset: int) -> str:
    year = int(month[:4])
    month_index = int(month[5:]) - 1
    absolute = year * 12 + month_index + offset
    target_year, target_month_index = divmod(absolute, 12)
    return f"{target_year:04d}-{target_month_index + 1:02d}"


def registry_path_for_intake(intake_path: Path | None) -> Path | None:
    override = str(os.environ.get("MORTGAGE_WORKFLOW_REGISTRY_PATH") or "").strip()
    if override:
        return Path(override)
    if intake_path and intake_path.resolve() != DEFAULT_INTAKE.resolve():
        return None
    return DEFAULT_REGISTRY


def registry_env_for_property(item: dict[str, Any] | None, intake_path: Path | None = DEFAULT_INTAKE) -> dict[str, str]:
    prop = coverage.normalize_property((item or {}).get("property"))
    registry_path = registry_path_for_intake(intake_path)
    if not prop or not registry_path or not registry_path.exists():
        return {}
    try:
        data = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    downloaders = data.get("downloaders") if isinstance(data, dict) else None
    if not isinstance(downloaders, list):
        return {}
    for downloader in downloaders:
        if not isinstance(downloader, dict):
            continue
        if coverage.normalize_property(downloader.get("property")) != prop:
            continue
        env = downloader.get("env")
        if not isinstance(env, dict):
            return {}
        return {str(key): str(value) for key, value in env.items() if value is not None}
    return {}


def first_valid_month(values: list[object]) -> str:
    for value in values:
        text = str(value or "").strip()
        if valid_year_month(text):
            return text
    return ""


def offset_target_month(base: str, env: dict[str, str]) -> str:
    if not valid_year_month(base):
        return ""
    for key in sorted(env):
        if not key.endswith("TARGET_MONTH_DEFAULT_OFFSET"):
            continue
        try:
            return add_months(base, int(str(env[key]).strip()))
        except (TypeError, ValueError):
            continue
    return ""


def target_statement_month(item: dict[str, Any] | None = None, intake_path: Path | None = DEFAULT_INTAKE) -> str:
    item = item or {}
    item_month = first_valid_month(
        [
            item.get("target_statement_month"),
            item.get("target_month"),
            item.get("expected_target_month"),
            item.get("latest_report_target_month"),
        ]
    )
    if item_month:
        return item_month

    registry_env = registry_env_for_property(item, intake_path)
    item_env = item.get("env") if isinstance(item.get("env"), dict) else {}
    merged_env = {**registry_env, **{str(key): str(value) for key, value in item_env.items() if value is not None}}

    configured = ""
    citadel_item = is_citadel_item(item)
    if citadel_item:
        configured = str(
            os.environ.get("CITADEL_STATEMENT_TARGET_MONTH")
            or os.environ.get("BASELANE_MORTGAGE_CITADEL_STATEMENT_TARGET_MONTH")
            or ""
        ).strip()
        if valid_year_month(configured):
            return configured
    provider_month = first_valid_month(
        [
            os.environ.get("MORTGAGEQUESTIONS_TARGET_MONTH"),
            os.environ.get("MORTGAGEQUESTIONS_STATEMENT_TARGET_MONTH"),
            os.environ.get("MORTGAGE_GENERATED_HAR_TARGET_MONTH"),
            merged_env.get("CITADEL_TARGET_MONTH"),
            merged_env.get("CITADEL_STATEMENT_TARGET_MONTH"),
            merged_env.get("MORTGAGEQUESTIONS_TARGET_MONTH"),
            merged_env.get("MORTGAGEQUESTIONS_STATEMENT_TARGET_MONTH"),
            merged_env.get("MORTGAGE_GENERATED_HAR_TARGET_MONTH"),
        ]
    )
    if provider_month:
        return provider_month

    configured = configured or str(
        os.environ.get("MORTGAGE_STATEMENT_TARGET_MONTH")
        or os.environ.get("BASELANE_MORTGAGE_STATEMENT_TARGET_MONTH")
        or os.environ.get("MORTGAGE_WORKFLOW_TARGET_MONTH")
        or os.environ.get("BASELANE_MONTHLY_TARGET_STAMP")
        or ""
    ).strip()
    offset_month = "" if citadel_item else offset_target_month(configured, merged_env)
    if offset_month:
        return offset_month
    if valid_year_month(configured):
        return configured
    return datetime.now(timezone.utc).strftime("%Y-%m")


def required_response_paths_for_capture(analysis: dict[str, Any] | None) -> list[str]:
    paths: list[str] = []
    if not isinstance(analysis, dict):
        return paths
    requirements = analysis.get("response_body_requirements")
    if isinstance(requirements, list):
        for item in requirements:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            if path and path not in paths:
                paths.append(path)
    for item in string_list(analysis.get("missing_response_body_paths")):
        if item and item not in paths:
            paths.append(item)
    return paths


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


def build_post_capture_check_script(
    *,
    property_name: str,
    suggested_har_path: str,
    intake_path: Path,
    report_path: Path,
    advance_command: str,
    advance_apply_command: str,
    target_month: str | None = None,
    target_month_document_identifiers: list[str] | None = None,
) -> str:
    expected_document_ids_json = json.dumps(target_month_document_identifiers or [])
    target_month_arg = f" \\\n  --target-month {shlex.quote(target_month)}" if target_month else ""
    expected_document_id_args = "".join(
        f" \\\n  --expected-document-id {shlex.quote(str(item))}"
        for item in (target_month_document_identifiers or [])
        if str(item or "").strip()
    )
    return f"""#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_ROOT={shlex.quote(str(WORKSPACE_ROOT))}
HAR_PATH={shlex.quote(suggested_har_path)}
REPORT_PATH={shlex.quote(str(report_path))}
ADVANCE_COMMAND={shlex.quote(advance_command)}
ADVANCE_APPLY_COMMAND={shlex.quote(advance_apply_command)}
EXPECTED_TARGET_DOCUMENT_IDS={shlex.quote(expected_document_ids_json)}

if [ ! -s "$HAR_PATH" ]; then
  echo "[mortgage] HAR is missing or empty: $HAR_PATH" >&2
  exit 1
fi

cd "$WORKSPACE_ROOT"
set +e
python3 scripts/mortgage_workflow_har_intake_analyzer.py \\
  --property {shlex.quote(property_name)} \\
  --har "$HAR_PATH" \\
  --intake {shlex.quote(str(intake_path))} \\
  --report "$REPORT_PATH"{target_month_arg}{expected_document_id_args}
ANALYSIS_RC=$?
set -e

python3 - "$REPORT_PATH" "$ADVANCE_COMMAND" "$ADVANCE_APPLY_COMMAND" "$EXPECTED_TARGET_DOCUMENT_IDS" <<'PY' >&2
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
advance_command = sys.argv[2]
advance_apply_command = sys.argv[3]
try:
    expected_document_ids = json.loads(sys.argv[4])
except Exception:
    expected_document_ids = []
if not isinstance(expected_document_ids, list):
    expected_document_ids = []
expected_document_ids = [str(item).strip() for item in expected_document_ids if str(item or "").strip()]
try:
    report = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"[mortgage] Unable to read HAR analysis report: {{exc}}")
    raise SystemExit(0)

status = report.get("status")
reason = report.get("reason")
next_action = report.get("suggested_next_action")
print(f"[mortgage] HAR analysis status={{status}} reason={{reason}} next_action={{next_action}}")
if expected_document_ids:
    print(f"[mortgage] expected_target_document_ids={{expected_document_ids}}")
payload_details = report.get("statement_document_payload_details")
payload_document_ids = []
if isinstance(payload_details, list):
    for item in payload_details:
        if not isinstance(item, dict):
            continue
        text = str(item.get("document_identifier") or "").strip()
        if text and text not in payload_document_ids:
            payload_document_ids.append(text)
if payload_document_ids:
    print(f"[mortgage] captured_payload_document_ids={{payload_document_ids}}")
structured_missing_document_ids = report.get("target_month_missing_payload_document_identifiers")
if isinstance(structured_missing_document_ids, list):
    missing_expected_document_ids = [
        str(item) for item in structured_missing_document_ids if str(item or "").strip()
    ]
else:
    missing_expected_document_ids = [
        item for item in expected_document_ids if item not in payload_document_ids
    ]
coverage_status = report.get("target_month_document_identifier_payload_coverage_status")
if coverage_status:
    print(f"[mortgage] target_month_document_identifier_payload_coverage_status={{coverage_status}}")
if missing_expected_document_ids:
    print(f"[mortgage] target_month_missing_payload_document_identifiers={{missing_expected_document_ids}}")
requirements = report.get("response_body_requirements")
if status == "ok":
    print("[mortgage] HAR has enough embedded response bodies to scaffold a downloader.")
    if expected_document_ids and missing_expected_document_ids:
        print(
            "[mortgage] Captured PDF payload did not include expected target document IDs: "
            f"{{missing_expected_document_ids}}"
        )
        raise SystemExit(3)
    if isinstance(requirements, list) and requirements:
        print("[mortgage] Additional candidate response-body gaps were found but are not blocking this HAR.")
        for item in requirements:
            if not isinstance(item, dict):
                continue
            roles = item.get("roles") or item.get("role") or []
            if isinstance(roles, list):
                roles_text = ",".join(str(role) for role in roles)
            else:
                roles_text = str(roles)
            count = item.get("missing_response_body_count")
            print(f"[mortgage] - advisory {{item.get('path')}} roles={{roles_text}} missing_count={{count}}")
    print(f"[mortgage] advance_workflow_evidence_command={{advance_command}}")
    print(f"[mortgage] advance_workflow_evidence_apply_command={{advance_apply_command}}")
elif isinstance(requirements, list) and requirements:
    print("[mortgage] Required response bodies still missing:")
    for item in requirements:
        if not isinstance(item, dict):
            continue
        roles = item.get("roles") or item.get("role") or []
        if isinstance(roles, list):
            roles_text = ",".join(str(role) for role in roles)
        else:
            roles_text = str(roles)
        count = item.get("missing_response_body_count")
        print(f"[mortgage] - {{item.get('path')}} roles={{roles_text}} missing_count={{count}}")
elif reason == "embedded_statement_pdf_payload_missing":
    candidate_count = report.get("statement_document_candidate_count")
    payload_count = report.get("statement_document_payload_count")
    metadata_months = report.get("statement_document_metadata_only_months")
    target_details = report.get("target_statement_document_metadata_only_details")
    if isinstance(metadata_months, list):
        metadata_month_values = sorted(str(item) for item in metadata_months if str(item or "").strip())
        latest_metadata_month = metadata_month_values[-1] if metadata_month_values else None
    else:
        latest_metadata_month = None
    print("[mortgage] Statement/document metadata is present, but no embedded PDF payload was captured.")
    print(f"[mortgage] statement_document_candidate_count={{candidate_count}}")
    print(f"[mortgage] statement_document_payload_count={{payload_count}}")
    print(f"[mortgage] latest_statement_document_metadata_only_month={{latest_metadata_month}}")
    print(f"[mortgage] statement_document_metadata_only_months={{metadata_months}}")
    if isinstance(target_details, list) and target_details:
        print(f"[mortgage] target_statement_document_metadata_only_details={{target_details}}")
    if expected_document_ids:
        print(
            "[mortgage] Still waiting for PDF payload for target document IDs: "
            f"{{missing_expected_document_ids or expected_document_ids}}"
        )
else:
    print("[mortgage] No actionable statement/document response bodies were identified.")
PY

exit "$ANALYSIS_RC"
"""


def advance_workflow_evidence_command(
    *,
    property_name: str,
    har_path: str,
    intake_path: Path,
    report_dir: Path,
    target_month: str,
    apply: bool = False,
) -> str:
    slug = coverage.slugify(property_name)
    command = (
        "python3 scripts/advance_mortgage_workflow_evidence.py "
        f"--property {shlex.quote(property_name)} "
        f"--har {shlex.quote(f'{property_name}={har_path}')} "
        f"--intake {shlex.quote(str(intake_path))} "
        f"--registry {shlex.quote(str(WORKSPACE_ROOT / 'config' / 'mortgage_statement_downloaders.json'))} "
        f"--report-dir {shlex.quote(str(report_dir))} "
        f"--report {shlex.quote(str(report_dir / f'mortgage_workflow_evidence_advance_{slug}.json'))} "
        f"--target-month {shlex.quote(target_month)} "
        "--write-stubs "
        "--install-registry-entries"
    )
    if apply:
        return f"{command} --apply-evidence --apply-registry"
    return command


def markdown_value(value: Any) -> str:
    if value in (None, "", [], {}):
        return "none"
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def without_generated_at(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: without_generated_at(item) for key, item in value.items() if key != "generated_at"}
    if isinstance(value, list):
        return [without_generated_at(item) for item in value]
    return value


def preserve_generated_at_if_unchanged(report: dict[str, Any], path: Path) -> dict[str, Any]:
    if not path.exists() or "generated_at" not in report:
        return report
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return report
    if not isinstance(previous, dict) or "generated_at" not in previous:
        return report
    if without_generated_at(previous) != without_generated_at(report):
        return report
    updated = dict(report)
    updated["generated_at"] = previous["generated_at"]
    return updated


def write_text_if_changed(path: Path, content: str, *, mode: int | None = None) -> None:
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        path.write_text(content, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


def write_json_report(path: Path, report: dict[str, Any]) -> dict[str, Any]:
    report = dict(report)
    report["idempotency_digest"] = stable_report_digest(report)
    stable_report = preserve_generated_at_if_unchanged(report, path)
    write_text_if_changed(path, json.dumps(stable_report, indent=2, sort_keys=True) + "\n")
    return stable_report


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def latest_year_month(values: list[str]) -> str | None:
    months = sorted(value for value in values if valid_year_month(value))
    return months[-1] if months else None


def aggregate_analysis_counts(analyses: list[dict[str, Any]], key: str) -> int | None:
    if not analyses:
        return None
    total = 0
    for item in analyses:
        try:
            total += int(item.get(key) or 0)
        except Exception:
            pass
    return total


def aggregate_analysis_months(analyses: list[dict[str, Any]], key: str) -> list[str]:
    values: list[str] = []
    for item in analyses:
        values.extend(string_list(item.get(key)))
    return sorted(set(value for value in values if valid_year_month(value)))


def aggregate_statement_metadata_details(
    analyses: list[dict[str, Any]],
    *,
    target_month: str = "",
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    seen: set[str] = set()
    for analysis in analyses:
        raw_items = analysis.get("statement_document_metadata_only_details")
        if not isinstance(raw_items, list):
            continue
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            month = str(raw.get("statement_month") or "").strip()
            if target_month and month != target_month:
                continue
            item = {
                key: raw.get(key)
                for key in [
                    "document_identifier",
                    "date",
                    "statement_month",
                    "name",
                    "source",
                    "source_index",
                    "pdf_payload_status",
                ]
                if raw.get(key) not in (None, "", [], {})
            }
            key = json.dumps(item, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            details.append(item)
    return details


def statement_document_identifiers(details: list[dict[str, Any]]) -> list[str]:
    identifiers: list[str] = []
    seen: set[str] = set()
    for item in details:
        text = str(item.get("document_identifier") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        identifiers.append(text)
    return identifiers


def statement_detail_descriptor(detail: dict[str, Any], target_month: str) -> str:
    identifier = str(detail.get("document_identifier") or "").strip()
    date = str(detail.get("date") or "").strip()
    descriptor = target_month
    if date:
        descriptor = f"{target_month} statement dated {date}"
    if identifier:
        descriptor = f"{descriptor} (document_identifier {identifier})"
    return descriptor


def primary_capture_statement_details(details: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not details:
        return None, []
    preferred_statuses = {
        "download_response_body_omitted_from_har",
        "download_response_without_pdf_payload",
    }
    primary = next(
        (
            item
            for item in details
            if str(item.get("pdf_payload_status") or "").strip() in preferred_statuses
        ),
        details[0],
    )
    alternates = [item for item in details if item is not primary]
    return primary, alternates


def selected_workflow_analysis(item: dict[str, Any], prop: str) -> tuple[str, str, dict[str, Any], list[dict[str, Any]]]:
    analyses = analyzer.analyze_workflow_evidence(item, property_name=prop)
    selected = analyzer.select_workflow_evidence_analysis(analyses)
    if selected:
        path = str(selected.get("har_path") or "")
        reason = str(selected.get("har_selection_reason") or "workflow_evidence")
        if len([item for item in analyses if item.get("har_path_exists") is True]) > 1:
            reason = "best_existing_workflow_evidence"
        else:
            reason = "first_existing_workflow_evidence"
        return path, reason, selected, analyses
    suggested_har_path, har_selection_reason = selected_workflow_har_path(item)
    analysis: dict[str, Any] = {}
    if suggested_har_path and Path(suggested_har_path).exists():
        analysis = analyzer.analyze_har(
            Path(suggested_har_path),
            property_name=prop,
            item=item,
            selection_reason=har_selection_reason,
        )
        analyses = [analysis]
    return suggested_har_path, har_selection_reason, analysis, analyses


def capture_requirements_for_handoff(
    *,
    target_month: str,
    analysis: dict[str, Any],
    target_statement_document_metadata_only_details: list[dict[str, Any]] | None = None,
) -> list[str]:
    metadata_only_months = string_list(analysis.get("statement_document_metadata_only_months"))
    latest_metadata_only_month = latest_year_month(metadata_only_months)
    details = target_statement_document_metadata_only_details or []
    if analysis.get("reason") == "embedded_statement_pdf_payload_missing" and details:
        primary, alternates = primary_capture_statement_details(details)
        descriptor = statement_detail_descriptor(primary or details[0], target_month)
        statement_instruction = (
            "Authenticate normally and open/download the "
            f"{descriptor} so the HAR captures the embedded PDF payload."
        )
        alternate_descriptors = [
            statement_detail_descriptor(item, target_month)
            for item in alternates
        ]
        alternate_descriptors = list(dict.fromkeys(alternate_descriptors))
        if alternate_descriptors:
            statement_instruction = (
                f"{statement_instruction} Same-month alternate metadata-only statement(s): "
                f"{'; '.join(alternate_descriptors)}."
            )
    elif analysis.get("reason") == "embedded_statement_pdf_payload_missing" and latest_metadata_only_month:
        if latest_metadata_only_month == target_month:
            statement_instruction = (
                "Authenticate normally and open/download the "
                f"{latest_metadata_only_month} statement so the HAR captures the embedded PDF payload."
            )
        else:
            statement_instruction = (
                "Authenticate normally and open/download the "
                f"{target_month} statement so the HAR captures the embedded PDF payload."
            )
    else:
        statement_instruction = f"Authenticate normally and navigate to the {target_month} mortgage statement."
    return [
        "Use a visible browser session, not a headless browser.",
        "Run capture_command from the workspace root to open or reuse a visible CDP browser tab.",
        statement_instruction,
        "Do not substitute an older statement; stale statement evidence blocks tokenomics workbook writes.",
        "Keep capture_command running while opening/downloading the statement; the CDP helper records Network.getResponseBody so binary PDF payloads are retained.",
        "Do not rely on a manual DevTools HAR export for binary PDF statements; it can show HTTP 200 responses while omitting content.text.",
        "Include statement index/list requests and document/PDF detail requests.",
        "Do not replay captured auth tokens; use the HAR only as workflow evidence.",
    ]


def build_handoff_markdown(handoff: dict[str, Any]) -> str:
    capture_requirements = handoff.get("capture_requirements") if isinstance(handoff.get("capture_requirements"), list) else []
    next_steps = handoff.get("next_steps_after_capture") if isinstance(handoff.get("next_steps_after_capture"), list) else []
    lines = [
        "# Mortgage Workflow Evidence Capture",
        "",
        f"- property: `{markdown_value(handoff.get('property'))}`",
        f"- status: `{markdown_value(handoff.get('status'))}`",
        f"- reason: `{markdown_value(handoff.get('reason'))}`",
        f"- suggested_next_action: `{markdown_value(handoff.get('suggested_next_action'))}`",
        f"- servicer_hint: `{markdown_value(handoff.get('servicer_hint'))}`",
        f"- portal_url: `{markdown_value(handoff.get('portal_url'))}`",
        f"- target_statement_month: `{markdown_value(handoff.get('target_statement_month'))}`",
        f"- target_statement_dir: `{markdown_value(handoff.get('target_statement_dir'))}`",
        f"- suggested_workflow_har_path: `{markdown_value(handoff.get('suggested_workflow_har_path'))}`",
        f"- suggested_workflow_har_path_exists: `{markdown_value(handoff.get('suggested_workflow_har_path_exists'))}`",
        f"- suggested_workflow_har_path_size: `{markdown_value(handoff.get('suggested_workflow_har_path_size'))}`",
        f"- har_selection_reason: `{markdown_value(handoff.get('har_selection_reason'))}`",
        f"- workflow_evidence_path_count: `{markdown_value(handoff.get('workflow_evidence_path_count'))}`",
        f"- workflow_evidence_analysis_count: `{markdown_value(handoff.get('workflow_evidence_analysis_count'))}`",
        f"- analysis_report_path: `{markdown_value(handoff.get('analysis_report_path'))}`",
        f"- capture_command: `{markdown_value(handoff.get('capture_command'))}`",
        f"- capture_command_ready_to_run_now: `{markdown_value(handoff.get('capture_command_ready_to_run_now'))}`",
        f"- capture_command_safe_to_run_automatically: `{markdown_value(handoff.get('capture_command_safe_to_run_automatically'))}`",
        f"- post_capture_check_command: `{markdown_value(handoff.get('post_capture_check_command'))}`",
        f"- post_capture_check_ready_to_run_now: `{markdown_value(handoff.get('post_capture_check_ready_to_run_now'))}`",
        f"- register_workflow_evidence_command: `{markdown_value(handoff.get('register_workflow_evidence_command'))}`",
        f"- register_workflow_evidence_apply_command: `{markdown_value(handoff.get('register_workflow_evidence_apply_command'))}`",
        f"- register_workflow_evidence_ready_to_run_now: `{markdown_value(handoff.get('register_workflow_evidence_ready_to_run_now'))}`",
        f"- advance_workflow_evidence_command: `{markdown_value(handoff.get('advance_workflow_evidence_command'))}`",
        f"- advance_workflow_evidence_apply_command: `{markdown_value(handoff.get('advance_workflow_evidence_apply_command'))}`",
        f"- advance_workflow_evidence_ready_to_run_now: `{markdown_value(handoff.get('advance_workflow_evidence_ready_to_run_now'))}`",
        f"- capture_required_before_offline_next_step: `{markdown_value(handoff.get('capture_required_before_offline_next_step'))}`",
        f"- required_capture_quality: `{markdown_value(handoff.get('required_capture_quality'))}`",
        f"- statement_document_metadata_only_count: `{markdown_value(handoff.get('statement_document_metadata_only_count'))}`",
        f"- statement_document_payload_count: `{markdown_value(handoff.get('statement_document_payload_count'))}`",
        f"- latest_statement_document_metadata_only_month: `{markdown_value(handoff.get('latest_statement_document_metadata_only_month'))}`",
        f"- target_statement_document_metadata_only_details: `{markdown_value(handoff.get('target_statement_document_metadata_only_details'))}`",
        f"- safe_to_run_automatically: `{markdown_value(handoff.get('safe_to_run_automatically'))}`",
        "",
        "## Capture Requirements",
        "",
    ]
    lines.extend(f"- {item}" for item in capture_requirements)
    lines.extend(
        [
            "",
            "## After Capture",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in next_steps)
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Use a visible browser session; do not use a headless browser.",
            "- Do not replay captured auth tokens; use the HAR only as workflow evidence.",
            "- Keep secrets, cookies, authorization headers, and raw credentials out of committed files and chat.",
            "",
        ]
    )
    return "\n".join(lines)


def selected_workflow_har_path(item: dict[str, Any]) -> tuple[str, str]:
    prop = str(item.get("property") or "")
    selected, reason = analyzer.default_har_path(item, prop)
    if selected:
        return selected, reason
    return coverage.suggested_workflow_har_path(prop, item.get("portal_url")), "suggested_workflow_har_path"


def build_handoff(
    item: dict[str, Any],
    report_dir: Path,
    intake_path: Path = DEFAULT_INTAKE,
    target_month_override: str | None = None,
) -> dict[str, Any]:
    prop = str(item.get("property") or "").strip()
    slug = coverage.slugify(prop)
    portal_url = item.get("portal_url")
    target_month = target_month_override or target_statement_month(item, intake_path)
    handoff_path = report_dir / f"mortgage_workflow_evidence_handoff_{slug}.json"
    handoff_markdown_path = report_dir / f"mortgage_workflow_evidence_handoff_{slug}.md"
    post_capture_check_script_path = report_dir / f"mortgage_workflow_evidence_check_{slug}.sh"
    analysis_report_path = report_dir / f"mortgage_workflow_har_intake_analysis_{slug}.json"
    suggested_har_path, har_selection_reason, analysis, workflow_analyses = selected_workflow_analysis(item, prop)
    suggested_har_exists = bool(suggested_har_path and Path(suggested_har_path).exists())
    suggested_har_size = Path(suggested_har_path).stat().st_size if suggested_har_exists else None
    if not suggested_har_exists:
        status = "review"
        reason = "har_missing"
        suggested_next_action = "capture_workflow_har_with_full_response_bodies"
    elif analysis.get("status") == "ok":
        status = "ok"
        reason = None
        suggested_next_action = "advance_workflow_evidence"
    else:
        status = "review"
        reason = str(analysis.get("reason") or "workflow_evidence_not_ready")
        suggested_next_action = str(analysis.get("suggested_next_action") or "recapture_workflow_har_with_full_response_bodies")
    analysis_command = coverage.workflow_evidence_analysis_command(prop)
    register_command = (
        "python3 scripts/register_mortgage_workflow_evidence.py "
        f"--property {shlex.quote(prop)} "
        f"--har {shlex.quote(suggested_har_path)} "
        f"--intake {shlex.quote(str(intake_path))} "
        f"--report-dir {shlex.quote(str(report_dir))}"
    )
    advance_command = advance_workflow_evidence_command(
        property_name=prop,
        har_path=suggested_har_path,
        intake_path=intake_path,
        report_dir=report_dir,
        target_month=target_month,
        apply=False,
    )
    advance_apply_command = advance_workflow_evidence_command(
        property_name=prop,
        har_path=suggested_har_path,
        intake_path=intake_path,
        report_dir=report_dir,
        target_month=target_month,
        apply=True,
    )
    required_response_paths = required_response_paths_for_capture(analysis)
    analysis_sources = workflow_analyses or ([analysis] if analysis else [])
    target_statement_document_metadata_only_details = aggregate_statement_metadata_details(
        analysis_sources,
        target_month=target_month,
    )
    target_month_document_identifiers = statement_document_identifiers(
        target_statement_document_metadata_only_details
    )
    if is_citadel_item(item):
        capture_command = "scripts/citadel_manual_capture_har.sh"
        capture_command_ready = True
    else:
        capture_command = coverage.workflow_evidence_capture_command(
            prop,
            portal_url,
            suggested_har_path,
            required_response_paths=required_response_paths,
            expected_document_ids=target_month_document_identifiers,
        )
        capture_command_ready = bool(portal_url and suggested_har_path)
    required_capture_quality = analysis.get("required_capture_quality") if analysis else None
    statement_document_metadata_only_months = (
        aggregate_analysis_months(analysis_sources, "statement_document_metadata_only_months")
        if analysis_sources
        else string_list(analysis.get("statement_document_metadata_only_months"))
    )
    statement_document_payload_months = (
        aggregate_analysis_months(analysis_sources, "statement_document_payload_months")
        if analysis_sources
        else string_list(analysis.get("statement_document_payload_months"))
    )
    latest_metadata_only_month = latest_year_month(statement_document_metadata_only_months)
    if reason == "candidate_endpoints_missing_response_bodies" and (
        target_statement_document_metadata_only_details or latest_metadata_only_month
    ):
        reason = "embedded_statement_pdf_payload_missing"
        suggested_next_action = "capture_target_month_statement"
        required_capture_quality = "target_month_statement_pdf"
    capture_requirements_analysis = dict(analysis)
    capture_requirements_analysis["reason"] = reason
    capture_requirements_analysis["statement_document_metadata_only_months"] = (
        statement_document_metadata_only_months
    )
    if required_capture_quality:
        capture_requirements_analysis["required_capture_quality"] = required_capture_quality
    return {
        "job": "mortgage-workflow-evidence-handoff",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reason": reason,
        "suggested_next_action": suggested_next_action,
        "property": prop,
        "servicer_hint": item.get("servicer_hint"),
        "portal_url": portal_url,
        "target_statement_dir": item.get("target_statement_dir"),
        "target_statement_month": target_month,
        "suggested_workflow_har_path": suggested_har_path,
        "suggested_workflow_har_path_exists": suggested_har_exists,
        "suggested_workflow_har_path_size": suggested_har_size,
        "har_selection_reason": har_selection_reason,
        "workflow_evidence_paths": [str(path) for path in analyzer.workflow_evidence_paths(item)],
        "workflow_evidence_path_count": len(analyzer.workflow_evidence_paths(item)),
        "workflow_evidence_analysis_count": len(workflow_analyses),
        "workflow_evidence_analyses": [
            analyzer.workflow_evidence_analysis_summary(item) for item in workflow_analyses
        ],
        "analysis_command": analysis_command,
        "register_workflow_evidence_command": register_command,
        "register_workflow_evidence_apply_command": f"{register_command} --apply",
        "advance_workflow_evidence_command": advance_command,
        "advance_workflow_evidence_apply_command": advance_apply_command,
        "analysis_report_path": str(analysis_report_path),
        "handoff_path": str(handoff_path),
        "handoff_markdown_path": str(handoff_markdown_path),
        "post_capture_check_script_path": str(post_capture_check_script_path),
        "capture_command": capture_command,
        "capture_command_ready_to_run_now": capture_command_ready,
        "capture_command_safe_to_run_automatically": False,
        "post_capture_check_command": f"bash {shlex.quote(str(post_capture_check_script_path))}",
        "post_capture_check_safe_to_run_after_har_capture": True,
        "post_capture_check_ready_to_run_now": suggested_har_exists,
        "register_workflow_evidence_ready_to_run_now": suggested_har_exists,
        "advance_workflow_evidence_ready_to_run_now": suggested_har_exists,
        "capture_required_before_offline_next_step": not suggested_har_exists,
        "workflow_har_analysis_status": analysis.get("status") if analysis else None,
        "workflow_har_analysis_reason": analysis.get("reason") if analysis else None,
        "workflow_har_analysis_suggested_next_action": analysis.get("suggested_next_action") if analysis else None,
        "statement_document_candidate_count": aggregate_analysis_counts(
            analysis_sources,
            "statement_document_candidate_count",
        ),
        "statement_document_metadata_only_count": aggregate_analysis_counts(
            analysis_sources,
            "statement_document_metadata_only_count",
        ),
        "statement_document_payload_count": aggregate_analysis_counts(
            analysis_sources,
            "statement_document_payload_count",
        ),
        "statement_document_metadata_only_months": statement_document_metadata_only_months,
        "statement_document_payload_months": statement_document_payload_months,
        "latest_statement_document_metadata_only_month": latest_metadata_only_month,
        "target_statement_document_metadata_only_details": target_statement_document_metadata_only_details,
        "target_month_document_identifiers": target_month_document_identifiers,
        "required_capture_quality": required_capture_quality or "full_response_bodies",
        "workflow_evidence_update": {
            "config_path": str(intake_path),
            "property": prop,
            "append_workflow_evidence": suggested_har_path,
            "dry_run_command": register_command,
            "apply_command": f"{register_command} --apply",
        },
        "capture_requirements": capture_requirements_for_handoff(
            target_month=target_month,
            analysis=capture_requirements_analysis,
            target_statement_document_metadata_only_details=target_statement_document_metadata_only_details,
        ),
        "next_steps_after_capture": [
            "Place the HAR at suggested_workflow_har_path.",
            "Run post_capture_check_command to verify endpoint/body coverage without using live credentials.",
            "Run register_workflow_evidence_command to validate the HAR and preview the intake update.",
            "Run register_workflow_evidence_apply_command only after the dry-run report is acceptable.",
            "Run advance_workflow_evidence_command to dry-run evidence registration, scaffold generation, disabled registry-entry validation, and generated downloader target-month verification.",
            "Run advance_workflow_evidence_apply_command only after the dry-run advance report is acceptable.",
            f"Run {analysis_command} to inspect endpoint/body coverage without replaying auth.",
            "Build or update the servicer-specific downloader from the captured request/response flow.",
            "Run scripts/audit_mortgage_downloader_coverage.py and the focused mortgage tests.",
        ],
        "safe_to_run_automatically": False,
    }


def pending_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    for item in items:
        status = str(item.get("status") or "").strip()
        evidence = item.get("workflow_evidence")
        evidence_count = len(evidence) if isinstance(evidence, list) else 0
        if status == "needs_workflow_evidence" or evidence_count == 0:
            pending.append(item)
            continue
        prop = str(item.get("property") or "")
        har_path_text, selection_reason = selected_workflow_har_path(item)
        analysis = analyzer.analyze_har(
            Path(har_path_text) if har_path_text else None,
            property_name=prop,
            item=item,
            selection_reason=selection_reason,
        )
        if analysis.get("status") != "ok":
            pending.append(item)
    return pending


def write_handoff(
    item: dict[str, Any],
    report_dir: Path,
    intake_path: Path = DEFAULT_INTAKE,
    target_month_override: str | None = None,
) -> dict[str, Any]:
    handoff = build_handoff(item, report_dir, intake_path, target_month_override)
    handoff = write_json_report(Path(handoff["handoff_path"]), handoff)
    write_text_if_changed(Path(handoff["handoff_markdown_path"]), build_handoff_markdown(handoff))
    check_script_path = Path(handoff["post_capture_check_script_path"])
    write_text_if_changed(
        check_script_path,
        build_post_capture_check_script(
            property_name=handoff["property"],
            suggested_har_path=handoff["suggested_workflow_har_path"],
            intake_path=intake_path,
            report_path=Path(handoff["analysis_report_path"]),
            advance_command=handoff["advance_workflow_evidence_command"],
            advance_apply_command=handoff["advance_workflow_evidence_apply_command"],
            target_month=handoff.get("target_statement_month"),
            target_month_document_identifiers=handoff["target_month_document_identifiers"],
        ),
        mode=0o755,
    )
    return handoff


def build_batch_handoff(
    items: list[dict[str, Any]],
    report_dir: Path,
    intake_path: Path = DEFAULT_INTAKE,
    target_month_override: str | None = None,
) -> dict[str, Any]:
    selected = pending_items(items)
    handoffs = [write_handoff(item, report_dir, intake_path, target_month_override) for item in selected]
    manual_capture_handoffs = [
        item for item in handoffs
        if item.get("capture_command_safe_to_run_automatically") is not True
    ]
    missing_har_handoffs = [
        item for item in handoffs
        if item.get("suggested_workflow_har_path_exists") is not True
    ]
    existing_har_needs_bodies_handoffs = [
        item for item in handoffs
        if item.get("suggested_workflow_har_path_exists") is True
        and item.get("workflow_har_analysis_status") != "ok"
    ]
    ready_to_advance_handoffs = [
        item for item in handoffs
        if item.get("status") == "ok"
        and item.get("advance_workflow_evidence_ready_to_run_now") is True
    ]
    index_path = report_dir / "mortgage_workflow_evidence_handoff_index.json"
    report = {
        "job": "mortgage-workflow-evidence-handoff-index",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "review" if handoffs else "ok",
        "reason": "workflow_evidence_needed" if handoffs else None,
        "pending_property_count": len(handoffs),
        "pending_properties": [item.get("property") for item in handoffs],
        "manual_capture_count": len(manual_capture_handoffs),
        "manual_capture_properties": [item.get("property") for item in manual_capture_handoffs],
        "missing_har_count": len(missing_har_handoffs),
        "missing_har_properties": [item.get("property") for item in missing_har_handoffs],
        "existing_har_needs_bodies_count": len(existing_har_needs_bodies_handoffs),
        "existing_har_needs_bodies_properties": [
            item.get("property") for item in existing_har_needs_bodies_handoffs
        ],
        "ready_to_advance_count": len(ready_to_advance_handoffs),
        "ready_to_advance_properties": [item.get("property") for item in ready_to_advance_handoffs],
        "handoff_paths": [item.get("handoff_path") for item in handoffs],
        "handoff_markdown_paths": [item.get("handoff_markdown_path") for item in handoffs],
        "post_capture_check_script_paths": [item.get("post_capture_check_script_path") for item in handoffs],
        "suggested_workflow_har_paths": [item.get("suggested_workflow_har_path") for item in handoffs],
        "suggested_workflow_har_path_exists_by_property": {
            str(item.get("property") or ""): item.get("suggested_workflow_har_path_exists")
            for item in handoffs
        },
        "capture_commands": [item.get("capture_command") for item in handoffs],
        "post_capture_check_commands": [item.get("post_capture_check_command") for item in handoffs],
        "handoffs": handoffs,
        "index_path": str(index_path),
        "safe_to_run_automatically": not handoffs or all(
            item.get("safe_to_run_automatically") is True for item in handoffs
        ),
    }
    return write_json_report(index_path, report)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--property", help="Property name from config/mortgage_downloader_intake.json")
    group.add_argument("--all-pending", action="store_true", help="Write handoffs for every pending intake property")
    parser.add_argument("--intake", type=Path, default=DEFAULT_INTAKE)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--target-month", help="Override target statement month as YYYY-MM.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    items = load_intake(args.intake)
    report_dir = args.report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    if args.all_pending:
        report = build_batch_handoff(items, report_dir, args.intake, args.target_month)
        print(json.dumps(report, indent=2, sort_keys=True), file=stdout)
        return 1 if report["pending_property_count"] else 0
    item = find_property(items, args.property)
    if not item:
        print(f"Property not found in intake: {args.property}", file=sys.stderr)
        return 2
    handoff = write_handoff(item, report_dir, args.intake, args.target_month)
    print(json.dumps(handoff, indent=2, sort_keys=True), file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
