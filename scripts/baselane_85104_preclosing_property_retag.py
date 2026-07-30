#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import tempfile
from urllib.error import URLError
from urllib.request import urlopen
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).absolute().parents[1]
GRAPHQL_HELPER = ROOT / "scripts" / "baselane_graphql_via_cdp.js"
APPLY_ENV = "BASELANE_85104_PRECLOSING_RETAG_APPLY"
APPLY_DIGEST_ENV = "BASELANE_85104_PRECLOSING_RETAG_APPLY_DIGEST"
PARTIAL_ACK_ENV = "BASELANE_85104_PRECLOSING_PARTIAL_WILL_NOT_CLEAR_VALIDATION"
SOURCE_INDEX = ROOT / "reports" / "baselane_source_transaction_index.20260712-070814.csv"
AUDIT_JSON = ROOT / "reports" / "baselane_85104_preclosing_property_retag_audit.json"
PAYLOAD_JSON = ROOT / "reports" / "baselane_85104_preclosing_property_retag_payload.json"
REPORT_JSON = ROOT / "reports" / "baselane_85104_preclosing_property_retag_apply.json"
COMMANDS_FILE = ROOT / "reports" / "baselane_85104_preclosing_property_retag_apply.requires-explicit-approval.sh"
APPLY_READINESS_JSON = ROOT / "reports" / "baselane_85104_preclosing_property_retag_apply_readiness.json"
PARTIAL_APPLY_READINESS_JSON = ROOT / "reports" / "baselane_85104_preclosing_property_retag_partial_apply_readiness.json"
AUTH_REPORT_JSON = ROOT / "reports" / "baselane_cdp_auth_recovery_for_85104_retag.json"
AUTH_RECOVERY_SCRIPT = ROOT / "scripts" / "baselane_cdp_auth_recovery.py"
PROTECTED_REVIEW_CSV = ROOT / "reports" / "baselane_85104_preclosing_protected_row_review.csv"
PROTECTED_REVIEW_JSON = ROOT / "config" / "baselane_85104_preclosing_protected_row_review.json"
PROTECTED_REVIEW_COMMANDS_FILE = ROOT / "reports" / "baselane_85104_preclosing_protected_row_review_import.requires-explicit-approval.sh"
DEFAULT_CDP_VERSION_URLS = (
    "http://127.0.0.1:9222/json/version",
    "http://127.0.0.1:19222/json/version",
    "http://[::1]:9222/json/version",
    "http://[::1]:19222/json/version",
)


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_audit(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_if_present(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def source_rows_by_id(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        rows = {}
        for row in csv.DictReader(handle):
            row_id = str(row.get("BaselaneId") or "").strip()
            if row_id:
                rows[row_id] = {key: str(value or "") for key, value in row.items()}
        return rows


def verify_record(record: dict[str, Any], source_by_id: dict[str, dict[str, str]]) -> dict[str, Any]:
    row = source_by_id.get(str(record.get("baselane_id") or ""))
    checked = {**record, "status": "", "reason": ""}
    if not row:
        checked.update(status="blocked", reason="missing_current_source_row")
        return checked
    expected = (
        str(record.get("date") or ""),
        f"{float(record.get('amount') or 0):.2f}",
        str(record.get("property") or ""),
        str(record.get("property_id") or ""),
    )
    actual = (
        str(row.get("ISODate") or ""),
        f"{float(str(row.get('Amount') or '0').replace(',', '').replace('$', '')):.2f}",
        str(row.get("Property") or ""),
        str(row.get("PropertyId") or ""),
    )
    if actual != expected:
        checked.update(status="blocked", reason=f"current_source_mismatch expected={expected} actual={actual}")
        return checked
    checked.update(
        status="ready",
        reason="current source row matches audit identity",
        current_source_row={
            "date": row.get("Date") or row.get("ISODate") or "",
            "iso_date": row.get("ISODate") or "",
            "amount": row.get("Amount") or "",
            "property": row.get("Property") or "",
            "property_id": row.get("PropertyId") or "",
            "merchant": row.get("Merchant") or "",
            "description": row.get("Description") or "",
            "category": row.get("Category") or "",
            "notes": row.get("Notes") or "",
        },
    )
    return checked


def payload_for(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "operationName": "UpdateTransaction",
        "variables": {
            "input": [
                {
                    "id": str(record["baselane_id"]),
                    "propertyId": None,
                }
                for record in records
            ]
        },
        "query": """
        mutation UpdateTransaction($input: [UpdateTransaction!]) {
          updateTransactions(input: $input) {
            id
            propertyId
            tagId
            amount
            merchantName
            date
          }
        }
        """,
    }


def protected_closing_row_reason(record: dict[str, Any]) -> str | None:
    source = record.get("current_source_row") if isinstance(record.get("current_source_row"), dict) else {}
    category = str(record.get("category") or source.get("category") or "").lower()
    haystack = " ".join(
        str(record.get(key) or source.get(key) or "")
        for key in ("merchant", "description", "notes")
    ).lower()
    if "down payment" in category or "down payments" in category:
        return "protected_closing_funding_category_down_payments"
    title_markers = ("title", "escrow", "trustee", "closing", "nxhi")
    if any(marker in haystack for marker in title_markers):
        return "protected_closing_funding_title_or_escrow_marker"
    return None


def stable_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def protected_record_digest(record: dict[str, Any]) -> str:
    source = record.get("current_source_row") if isinstance(record.get("current_source_row"), dict) else {}
    evidence = {
        "baselane_id": str(record.get("baselane_id") or ""),
        "date": str(record.get("date") or source.get("iso_date") or source.get("date") or ""),
        "amount": f"{float(record.get('amount') or 0.0):.2f}",
        "property": str(record.get("property") or ""),
        "property_id": str(record.get("property_id") or ""),
        "account": str(record.get("account") or ""),
        "merchant": str(record.get("merchant") or source.get("merchant") or ""),
        "description": str(record.get("description") or source.get("description") or ""),
        "category": str(record.get("category") or source.get("category") or ""),
        "notes": str(record.get("notes") or source.get("notes") or ""),
        "reason": str(record.get("reason") or ""),
    }
    return stable_digest(evidence)


def payload_inputs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    variables = payload.get("variables") if isinstance(payload.get("variables"), dict) else {}
    inputs = variables.get("input") if isinstance(variables, dict) else []
    return inputs if isinstance(inputs, list) else []


def validate_payload(payload: dict[str, Any], ready: list[dict[str, Any]]) -> dict[str, Any]:
    inputs = payload_inputs(payload)
    ready_ids = {str(record.get("baselane_id") or "") for record in ready if str(record.get("baselane_id") or "")}
    seen: set[str] = set()
    payload_ids: set[str] = set()
    issues: list[dict[str, Any]] = []
    for index, item in enumerate(inputs):
        if not isinstance(item, dict):
            issues.append({"index": index, "code": "payload_input_not_object"})
            continue
        txn_id = str(item.get("id") or "")
        if not txn_id:
            issues.append({"index": index, "code": "payload_input_missing_id"})
            continue
        if txn_id in seen:
            issues.append({"index": index, "code": "duplicate_payload_id", "id": txn_id})
        seen.add(txn_id)
        payload_ids.add(txn_id)
        if txn_id not in ready_ids:
            issues.append({"index": index, "code": "payload_id_not_ready", "id": txn_id})
        if item.get("propertyId") is not None:
            issues.append(
                {
                    "index": index,
                    "code": "payload_property_id_not_null",
                    "id": txn_id,
                    "propertyId": item.get("propertyId"),
                }
            )
    missing_payload_ids = sorted(ready_ids - payload_ids)
    unexpected_payload_ids = sorted(payload_ids - ready_ids)
    for txn_id in missing_payload_ids:
        issues.append({"code": "ready_id_missing_from_payload", "id": txn_id})
    return {
        "ok": not issues,
        "status": "ok" if not issues else "review",
        "ready_count": len(ready_ids),
        "payload_input_count": len(inputs),
        "payload_id_count": len(payload_ids),
        "missing_payload_ids": missing_payload_ids,
        "unexpected_payload_ids": unexpected_payload_ids,
        "issue_count": len(issues),
        "issues": issues[:200],
    }


def sum_by(records: list[dict[str, Any]], key: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    for record in records:
        name = str(record.get(key) or "").strip() or "unknown"
        totals[name] = round(totals.get(name, 0.0) + float(record.get("amount") or 0.0), 2)
    return dict(sorted(totals.items(), key=lambda item: abs(item[1]), reverse=True))


def compact_retag_record(record: dict[str, Any]) -> dict[str, Any]:
    source = record.get("current_source_row") if isinstance(record.get("current_source_row"), dict) else {}
    return {
        "baselane_id": record.get("baselane_id"),
        "date": record.get("date") or source.get("iso_date") or source.get("date"),
        "amount": record.get("amount"),
        "property": record.get("property"),
        "property_id": record.get("property_id"),
        "account": record.get("account"),
        "merchant": record.get("merchant") or source.get("merchant"),
        "description": record.get("description") or source.get("description"),
        "category": record.get("category") or source.get("category"),
        "notes": record.get("notes") or source.get("notes"),
        "status": record.get("status"),
        "reason": record.get("reason"),
    }


def truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "approved", "reviewed"}


def write_protected_review_csv(path: Path | None, protected_records: list[dict[str, Any]]) -> dict[str, Any]:
    if path is None:
        return {"written": False, "path": None, "row_count": len(protected_records)}
    if not protected_records and path.exists():
        return {"written": False, "preserved": True, "path": str(path), "row_count": 0}
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "baselane_id",
        "date",
        "amount",
        "account",
        "merchant",
        "description",
        "category",
        "reason",
        "review_question",
        "disposition_options",
        "evidence_digest",
        "reviewed",
        "reviewed_at",
        "disposition",
        "review_note",
    ]
    existing_by_id: dict[str, dict[str, str]] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
            for row in csv.DictReader(handle):
                baselane_id = str(row.get("baselane_id") or "").strip()
                if baselane_id:
                    existing_by_id[baselane_id] = row
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in protected_records:
            compact = compact_retag_record(record)
            baselane_id = str(compact.get("baselane_id") or "")
            evidence_digest = protected_record_digest(record)
            existing = existing_by_id.get(baselane_id) or {}
            preserve_existing_review = str(existing.get("evidence_digest") or "") == evidence_digest
            writer.writerow(
                {
                    "baselane_id": baselane_id,
                    "date": compact.get("date") or "",
                    "amount": compact.get("amount") or "",
                    "account": compact.get("account") or "",
                    "merchant": compact.get("merchant") or "",
                    "description": compact.get("description") or "",
                    "category": compact.get("category") or "",
                    "reason": compact.get("reason") or "",
                    "review_question": (
                        "Should this pre-closing protected funding row remain attributed to the DAO as closing capital, "
                        "be untagged from the property, or be excluded from the DAO GL?"
                    ),
                    "disposition_options": "keep_property_tag_closing_capital|untag_preclosing_row|exclude_from_dao_gl",
                    "evidence_digest": evidence_digest,
                    "reviewed": existing.get("reviewed") if preserve_existing_review else "False",
                    "reviewed_at": existing.get("reviewed_at") if preserve_existing_review else "",
                    "disposition": existing.get("disposition") if preserve_existing_review else "",
                    "review_note": existing.get("review_note") if preserve_existing_review else "",
                }
            )
    return {"written": True, "path": str(path), "row_count": len(protected_records)}


def load_protected_review_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"status": "missing", "path": str(path) if path else None, "records": {}}
    payload = read_json_if_present(path)
    records = payload.get("records") if isinstance(payload.get("records"), dict) else {}
    return {**payload, "status": payload.get("status") or "review", "path": str(path), "records": records}


def validate_protected_review(
    protected_records: list[dict[str, Any]],
    review_payload: dict[str, Any],
    *,
    min_note_length: int = 20,
) -> dict[str, Any]:
    records = review_payload.get("records") if isinstance(review_payload.get("records"), dict) else {}
    blockers: list[str] = []
    reviewed_count = 0
    accepted_dispositions = {"keep_property_tag_closing_capital", "untag_preclosing_row", "exclude_from_dao_gl"}
    for record in protected_records:
        baselane_id = str(record.get("baselane_id") or "")
        row = records.get(baselane_id) if isinstance(records.get(baselane_id), dict) else None
        if row is None:
            blockers.append(f"missing_review={baselane_id}")
            continue
        if not truthy(row.get("reviewed")):
            blockers.append(f"not_reviewed={baselane_id}")
            continue
        if str(row.get("evidence_digest") or "") != protected_record_digest(record):
            blockers.append(f"evidence_digest_mismatch={baselane_id}")
            continue
        disposition = str(row.get("disposition") or "").strip()
        if disposition not in accepted_dispositions:
            blockers.append(f"invalid_disposition={baselane_id}")
            continue
        note = str(row.get("review_note") or "").strip()
        if len(note) < min_note_length:
            blockers.append(f"review_note_too_short={baselane_id}")
            continue
        if not str(row.get("reviewed_at") or "").strip():
            blockers.append(f"missing_reviewed_at={baselane_id}")
            continue
        reviewed_count += 1
    return {
        "status": "ok" if not blockers else "review",
        "ok": not blockers,
        "required_count": len(protected_records),
        "reviewed_count": reviewed_count,
        "blocker_count": len(blockers),
        "blockers": blockers,
    }


def apply_reviewed_protected_dispositions(
    records: list[dict[str, Any]],
    review_payload: dict[str, Any],
    validation: dict[str, Any],
) -> list[dict[str, Any]]:
    if validation.get("ok") is not True:
        return records
    reviews = review_payload.get("records") if isinstance(review_payload.get("records"), dict) else {}
    resolved = []
    for record in records:
        if not str(record.get("reason") or "").startswith("protected_closing_funding_"):
            resolved.append(record)
            continue
        baselane_id = str(record.get("baselane_id") or "")
        review = reviews.get(baselane_id) if isinstance(reviews.get(baselane_id), dict) else {}
        disposition = str(review.get("disposition") or "").strip()
        if disposition in {"untag_preclosing_row", "exclude_from_dao_gl"}:
            resolved.append(
                {
                    **record,
                    "status": "ready",
                    "reason": f"reviewed_protected_disposition_{disposition}",
                    "protected_review_disposition": disposition,
                }
            )
        else:
            resolved.append(
                {
                    **record,
                    "status": "blocked",
                    "reason": "protected_closing_funding_reviewed_keep_property_tag_closing_capital",
                    "protected_review_disposition": disposition,
                }
            )
    return resolved


def import_protected_review_csv(csv_path: Path | None, review_json: Path | None, *, min_note_length: int = 20) -> dict[str, Any]:
    if csv_path is None or review_json is None:
        return {"status": "skipped", "imported_count": 0, "blockers": ["missing_csv_or_review_json"]}
    if not csv_path.exists():
        return {"status": "missing", "imported_count": 0, "blockers": [f"missing_csv={csv_path}"]}
    records: dict[str, Any] = {}
    blockers: list[str] = []
    accepted_dispositions = {"keep_property_tag_closing_capital", "untag_preclosing_row", "exclude_from_dao_gl"}
    with csv_path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        for index, row in enumerate(csv.DictReader(handle), start=2):
            baselane_id = str(row.get("baselane_id") or "").strip()
            if not baselane_id:
                continue
            if not truthy(row.get("reviewed")):
                continue
            reviewed_at = str(row.get("reviewed_at") or "").strip()
            if not reviewed_at:
                blockers.append(f"row_{index}_missing_reviewed_at={baselane_id}")
                continue
            note = str(row.get("review_note") or "").strip()
            if len(note) < min_note_length:
                blockers.append(f"row_{index}_review_note_too_short={baselane_id}")
                continue
            disposition = str(row.get("disposition") or "").strip()
            if disposition not in accepted_dispositions:
                blockers.append(f"row_{index}_invalid_disposition={baselane_id}")
                continue
            records[baselane_id] = {
                "reviewed": True,
                "reviewed_at": reviewed_at,
                "disposition": disposition,
                "review_note": note,
                "evidence_digest": str(row.get("evidence_digest") or "").strip(),
                "csv_import": {"path": str(csv_path), "row_number": index, "imported_at": iso_z()},
            }
    payload = {"status": "review", "generated_at": iso_z(), "records": records}
    review_json.parent.mkdir(parents=True, exist_ok=True)
    review_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "status": "review" if blockers else "ok",
        "imported_count": len(records),
        "blocker_count": len(blockers),
        "blockers": blockers,
        "path": str(review_json),
    }


def write_protected_review_import_commands(path: Path | None, csv_path: Path, report_path: Path) -> dict[str, Any]:
    if path is None:
        return {"written": False, "path": None}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                "# Requires explicit human approval after completing the 85-104 protected-row review CSV.",
                "# Imports reviewed protected-row dispositions, reruns the retag audit, and refuses dirty validation.",
                'echo "[85104-protected-review] importing reviewed protected-row CSV and rerunning retag audit"',
                (
                    "python3 scripts/baselane_85104_preclosing_property_retag.py "
                    f"--protected-review-csv {str(csv_path)!r} "
                    f"--report {str(report_path)!r} "
                    "--import-protected-review-csv"
                ),
                "STATUS=\"$(python3 - <<'PY'",
                "import json",
                f"payload=json.load(open({str(report_path)!r}, encoding='utf-8'))",
                "validation=payload.get('protected_closing_row_review_validation') or {}",
                "status=validation.get('status') or ''",
                "print(status)",
                "if status != 'ok':",
                "    print('required_count=' + str(validation.get('required_count') or 0))",
                "    print('reviewed_count=' + str(validation.get('reviewed_count') or 0))",
                "    for blocker in validation.get('blockers') or []:",
                "        print('blocker=' + str(blocker))",
                "PY",
                ")\"",
                'if [ "${STATUS%%$\'\\n\'*}" != "ok" ]; then',
                '  echo "[85104-protected-review] protected-row review status is $STATUS; refusing monthly close" >&2',
                "  exit 1",
                "fi",
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return {"written": True, "path": str(path)}


def run_graphql(payload: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    if not GRAPHQL_HELPER.exists():
        raise FileNotFoundError(f"missing GraphQL helper: {GRAPHQL_HELPER}")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle)
        handle.flush()
        payload_path = handle.name
    try:
        proc = subprocess.run(
            ["node", str(GRAPHQL_HELPER), payload_path],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    finally:
        Path(payload_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        return {"ok": False, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"ok": False, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr, "error": str(exc)}
    return {"ok": not bool(data.get("errors")), "returncode": proc.returncode, "data": data, "stderr": proc.stderr}


def validate_apply_result(result: dict[str, Any] | None, expected_ids: set[str]) -> dict[str, Any]:
    if not result:
        return {"ok": False, "updated_count": 0, "missing_ids": sorted(expected_ids), "unexpected_property_ids": []}
    if result.get("ok") is not True:
        return {
            "ok": False,
            "updated_count": 0,
            "missing_ids": sorted(expected_ids),
            "unexpected_property_ids": [],
            "error": result.get("error"),
        }
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    payload_data = data.get("data") if isinstance(data.get("data"), dict) else {}
    updated = payload_data.get("updateTransactions")
    if not isinstance(updated, list):
        return {
            "ok": False,
            "updated_count": 0,
            "missing_ids": sorted(expected_ids),
            "unexpected_property_ids": [],
            "error": "missing_updateTransactions_result",
        }
    updated_ids = {str(item.get("id") or "") for item in updated if isinstance(item, dict) and str(item.get("id") or "")}
    unexpected_property_ids = [
        {"id": item.get("id"), "propertyId": item.get("propertyId")}
        for item in updated
        if isinstance(item, dict) and item.get("propertyId") is not None
    ]
    missing_ids = sorted(expected_ids - updated_ids)
    return {
        "ok": not missing_ids and not unexpected_property_ids,
        "updated_count": len(updated_ids),
        "expected_count": len(expected_ids),
        "missing_ids": missing_ids,
        "unexpected_property_ids": unexpected_property_ids,
    }


def refresh_auth_report(report: Path, cdp_version_url: str | None, timeout_seconds: int) -> dict[str, Any]:
    if not AUTH_RECOVERY_SCRIPT.exists():
        return {"status": "missing_auth_recovery_script", "path": str(AUTH_RECOVERY_SCRIPT)}
    command = ["python3", str(AUTH_RECOVERY_SCRIPT), "--report", str(report)]
    if cdp_version_url:
        cdp_base = cdp_version_url.replace("/json/version", "").rstrip("/")
        command.extend(["--cdp-url", cdp_base])
    try:
        proc = subprocess.run(
            command,
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return {"status": "auth_refresh_timeout", "error": str(exc)}
    refreshed = read_json_if_present(report)
    refreshed["_refresh_returncode"] = proc.returncode
    refreshed["_refresh_stdout"] = proc.stdout[-2000:]
    refreshed["_refresh_stderr"] = proc.stderr[-2000:]
    return refreshed


def cdp_preflight(version_urls: tuple[str, ...] = DEFAULT_CDP_VERSION_URLS, timeout_seconds: float = 1.5) -> dict[str, Any]:
    configured = os.environ.get("BASELANE_CDP_VERSION_URL")
    urls = (configured,) if configured else version_urls
    checks: list[dict[str, Any]] = []
    for url in urls:
        try:
            with urlopen(url, timeout=timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
            checks.append(
                {
                    "url": url,
                    "reachable": True,
                    "browser": data.get("Browser"),
                    "web_socket_debugger_url_present": bool(data.get("webSocketDebuggerUrl")),
                }
            )
        except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            checks.append({"url": url, "reachable": False, "error": str(exc)[:240]})
    reachable = [check for check in checks if check.get("reachable")]
    return {
        "configured_version_url": configured,
        "status": "reachable" if reachable else "unreachable",
        "reachable_count": len(reachable),
        "selected_version_url": reachable[0]["url"] if reachable else None,
        "checks": checks,
    }


def apply_readiness(
    *,
    ready_count: int,
    blocked_count: int,
    payload_transaction_count: int,
    payload_validation: dict[str, Any] | None = None,
    payload_digest: str | None = None,
    cdp_preflight_status: str | None,
    auth_report: dict[str, Any],
    protected_blocked_count: int = 0,
    allow_partial_apply_with_blocked_records: bool = False,
) -> dict[str, Any]:
    blockers: list[str] = []
    auth_status = str(auth_report.get("status") or "missing")
    expected_digest = str(payload_digest or "").strip().lower()
    provided_digest = str(os.environ.get(APPLY_DIGEST_ENV) or "").strip().lower()
    if ready_count <= 0 or payload_transaction_count <= 0:
        blockers.append("payload_empty")
    if payload_validation and payload_validation.get("ok") is not True:
        blockers.append("payload_validation_not_ok")
    if not expected_digest:
        blockers.append("payload_digest_missing")
    if provided_digest != expected_digest:
        blockers.append(f"{APPLY_DIGEST_ENV}_mismatch")
    unprotected_blocked_count = max(0, blocked_count - protected_blocked_count)
    if blocked_count > 0 and not allow_partial_apply_with_blocked_records:
        blockers.append(f"blocked_records_present={blocked_count}")
    if allow_partial_apply_with_blocked_records and unprotected_blocked_count > 0:
        blockers.append(f"unprotected_blocked_records_present={unprotected_blocked_count}")
    if cdp_preflight_status != "reachable":
        blockers.append(f"cdp_preflight_status={cdp_preflight_status or 'missing'}")
    if auth_status != "ok":
        blockers.append(f"cdp_auth_status={auth_status}")
    return {
        "apply_ready": not blockers,
        "apply_readiness_blockers": blockers,
        "apply_readiness_status": "ready" if not blockers else "blocked",
        "apply_digest_env": APPLY_DIGEST_ENV,
        "apply_digest_required_value": expected_digest,
        "apply_digest_provided": provided_digest,
        "apply_digest_present": bool(provided_digest),
        "apply_digest_ok": bool(expected_digest) and provided_digest == expected_digest,
        "allow_partial_apply_with_blocked_records": allow_partial_apply_with_blocked_records,
        "protected_blocked_count": protected_blocked_count,
        "unprotected_blocked_count": unprotected_blocked_count,
        "blocked_records_apply_policy": (
            "partial_ready_records_allowed_only_when_all_blocked_rows_are_protected_closing_rows"
            if allow_partial_apply_with_blocked_records
            else "blocked_records_prevent_apply"
        ),
    }


def approval_command(digest: str, *, allow_partial_apply_with_blocked_records: bool = False) -> str:
    partial_flag = " --allow-partial-apply-with-blocked-records" if allow_partial_apply_with_blocked_records else ""
    partial_ack = f"{PARTIAL_ACK_ENV}=1 " if allow_partial_apply_with_blocked_records else ""
    return (
        f"{partial_ack}{APPLY_ENV}=1 {APPLY_DIGEST_ENV}={digest} "
        f"python3 scripts/baselane_85104_preclosing_property_retag.py --refresh-auth-report --apply{partial_flag}"
    )


def write_commands_file(path: Path, digest: str, report_path: Path, *, allow_partial_apply_with_blocked_records: bool = False) -> None:
    command = approval_command(digest, allow_partial_apply_with_blocked_records=allow_partial_apply_with_blocked_records)
    refresh_command = f'{APPLY_DIGEST_ENV}="$EXPECTED_DIGEST" python3 "$SCRIPT_PATH" --refresh-auth-report'
    if allow_partial_apply_with_blocked_records:
        refresh_command += " --allow-partial-apply-with-blocked-records"
    script_path = "scripts/baselane_85104_preclosing_property_retag.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                "# Requires explicit human approval. Do not run until the payload and CDP auth report are reviewed.",
                "# This script rechecks dry-run readiness immediately before any upstream Baselane mutation.",
                *(
                    [
                        "# PARTIAL APPLY WARNING: this only clears ready non-protected rows.",
                        "# It will not clear coownership validation while protected pre-closing rows remain property-tagged.",
                        f": \"${{{PARTIAL_ACK_ENV}:?Set {PARTIAL_ACK_ENV}=1 to acknowledge partial apply will not clear validation}}\"",
                    ]
                    if allow_partial_apply_with_blocked_records
                    else []
                ),
                f"# Review report: {report_path}",
                f"EXPECTED_DIGEST={digest!r}",
                f"REPORT_PATH={str(report_path)!r}",
                f"SCRIPT_PATH={script_path!r}",
                "",
                'echo "[85104-retag] refreshing dry-run readiness and CDP auth report"',
                refresh_command,
                'ACTUAL_DIGEST="$(python3 - "$REPORT_PATH" <<\'PY\'',
                "import json, sys",
                "payload = json.load(open(sys.argv[1], encoding='utf-8'))",
                "print(payload.get('payload_digest') or '')",
                "PY",
                ')"',
                'APPLY_READY="$(python3 - "$REPORT_PATH" <<\'PY\'',
                "import json, sys",
                "payload = json.load(open(sys.argv[1], encoding='utf-8'))",
                "print('1' if payload.get('apply_ready') is True else '0')",
                "PY",
                ')"',
                'if [ "$ACTUAL_DIGEST" != "$EXPECTED_DIGEST" ]; then',
                '  echo "[85104-retag] digest mismatch: expected=$EXPECTED_DIGEST actual=$ACTUAL_DIGEST" >&2',
                "  exit 1",
                "fi",
                'if [ "$APPLY_READY" != "1" ]; then',
                '  echo "[85104-retag] apply_ready is false after preflight; refusing mutation" >&2',
                "  exit 1",
                "fi",
                'echo "[85104-retag] digest and readiness confirmed; applying explicit mutation"',
                command,
                "# After success: rerun Baselane sync, public financial split, coownership validation, and monthly transfer reconciliation.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def default_commands_file_for_report(report_path: Path) -> Path:
    if report_path == REPORT_JSON:
        return COMMANDS_FILE
    return report_path.with_suffix(".requires-explicit-approval.sh")


def default_readiness_report_for_report(report_path: Path, *, partial: bool) -> Path:
    if report_path == REPORT_JSON:
        return PARTIAL_APPLY_READINESS_JSON if partial else APPLY_READINESS_JSON
    return report_path.with_suffix(".partial-apply-readiness.json" if partial else ".apply-readiness.json")


def write_readiness_report(path: Path, report: dict[str, Any]) -> None:
    cdp_preflight_report = report.get("cdp_preflight") if isinstance(report.get("cdp_preflight"), dict) else {}
    payload = {
        "generated_at": report.get("generated_at"),
        "status": report.get("apply_readiness_status"),
        "apply_ready": report.get("apply_ready"),
        "apply_readiness_status": report.get("apply_readiness_status"),
        "apply_readiness_blockers": report.get("apply_readiness_blockers") or [],
        "apply_digest_env": report.get("apply_digest_env"),
        "apply_digest_required_value": report.get("apply_digest_required_value"),
        "apply_digest_present": report.get("apply_digest_present"),
        "apply_digest_ok": report.get("apply_digest_ok"),
        "allow_partial_apply_with_blocked_records": report.get("allow_partial_apply_with_blocked_records"),
        "blocked_records_apply_policy": report.get("blocked_records_apply_policy"),
        "ready_count": report.get("ready_count"),
        "blocked_count": report.get("blocked_count"),
        "protected_blocked_count": report.get("protected_blocked_count"),
        "unprotected_blocked_count": report.get("unprotected_blocked_count"),
        "payload": report.get("payload"),
        "payload_digest": report.get("payload_digest"),
        "payload_transaction_count": report.get("payload_transaction_count"),
        "payload_validation_status": report.get("payload_validation_status"),
        "payload_blocked": report.get("payload_blocked"),
        "cdp_preflight_status": cdp_preflight_report.get("status"),
        "cdp_auth_status": report.get("cdp_auth_status"),
        "source_report": report.get("report"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Unassign 85-104 Alawa pre-closing funding rows from the property tag.")
    parser.add_argument("--audit", type=Path, default=AUDIT_JSON)
    parser.add_argument("--source-index", type=Path, default=SOURCE_INDEX)
    parser.add_argument("--payload", type=Path, default=PAYLOAD_JSON)
    parser.add_argument("--report", type=Path, default=REPORT_JSON)
    parser.add_argument("--commands-file", type=Path, default=None)
    parser.add_argument("--apply-readiness-report", type=Path, default=None)
    parser.add_argument("--auth-report", type=Path, default=AUTH_REPORT_JSON)
    parser.add_argument("--protected-review-csv", type=Path, default=PROTECTED_REVIEW_CSV)
    parser.add_argument("--protected-review-json", type=Path, default=PROTECTED_REVIEW_JSON)
    parser.add_argument("--protected-review-import-commands", type=Path, default=PROTECTED_REVIEW_COMMANDS_FILE)
    parser.add_argument("--import-protected-review-csv", action="store_true")
    parser.add_argument("--refresh-auth-report", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-partial-apply-with-blocked-records", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    args = parser.parse_args(argv)
    commands_file = args.commands_file or default_commands_file_for_report(args.report)
    readiness_report = args.apply_readiness_report or default_readiness_report_for_report(
        args.report,
        partial=args.allow_partial_apply_with_blocked_records,
    )

    audit = read_audit(args.audit)
    source_by_id = source_rows_by_id(args.source_index)
    checked = [verify_record(record, source_by_id) for record in audit.get("records") or []]
    policy_checked: list[dict[str, Any]] = []
    for record in checked:
        if record.get("status") == "ready":
            protected_reason = protected_closing_row_reason(record)
            if protected_reason:
                record = {**record, "status": "blocked", "reason": protected_reason}
        policy_checked.append(record)
    applied = None
    preflight = cdp_preflight()
    auth_report = (
        refresh_auth_report(args.auth_report, str(preflight.get("selected_version_url") or ""), args.timeout_seconds)
        if args.refresh_auth_report
        else read_json_if_present(args.auth_report)
    )
    protected_records = [
        record for record in policy_checked if str(record.get("reason") or "").startswith("protected_closing_funding_")
    ]
    protected_review_csv = write_protected_review_csv(args.protected_review_csv, protected_records)
    protected_review_import = (
        import_protected_review_csv(args.protected_review_csv, args.protected_review_json)
        if args.import_protected_review_csv
        else {"status": "skipped", "imported_count": 0}
    )
    protected_review_payload = load_protected_review_json(args.protected_review_json)
    protected_review_validation = validate_protected_review(protected_records, protected_review_payload)
    policy_checked = apply_reviewed_protected_dispositions(
        policy_checked,
        protected_review_payload,
        protected_review_validation,
    )
    ready = [record for record in policy_checked if record["status"] == "ready"]
    blocked = [record for record in policy_checked if record["status"] != "ready"]
    payload = payload_for(ready)
    payload_validation = validate_payload(payload, ready)
    digest = stable_digest(payload)
    args.payload.parent.mkdir(parents=True, exist_ok=True)
    args.payload.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    ready_amount_sum = round(sum(float(record.get("amount") or 0.0) for record in ready), 2)
    blocked_amount_sum = round(sum(float(record.get("amount") or 0.0) for record in blocked), 2)
    protected_blocked_count = sum(
        1 for record in blocked if str(record.get("reason") or "").startswith("protected_closing_funding_")
    )
    protected_review_import_commands = write_protected_review_import_commands(
        args.protected_review_import_commands,
        args.protected_review_csv,
        args.report,
    )
    readiness = apply_readiness(
        ready_count=len(ready),
        blocked_count=len(blocked),
        protected_blocked_count=protected_blocked_count,
        payload_transaction_count=len(payload["variables"]["input"]),
        payload_validation=payload_validation,
        payload_digest=digest,
        cdp_preflight_status=str(preflight.get("status") or ""),
        auth_report=auth_report,
        allow_partial_apply_with_blocked_records=args.allow_partial_apply_with_blocked_records,
    )
    payload_blocked = len(ready) == 0
    write_commands_file(
        commands_file,
        digest,
        args.report,
        allow_partial_apply_with_blocked_records=args.allow_partial_apply_with_blocked_records,
    )
    apply_result_validation = None
    if args.apply:
        if not readiness["apply_ready"]:
            applied = {
                "ok": False,
                "error": "apply readiness blocked; refusing upstream Baselane mutation",
                "blockers": readiness["apply_readiness_blockers"],
            }
        elif os.environ.get(APPLY_ENV) != "1":
            applied = {"ok": False, "error": f"set {APPLY_ENV}=1 to apply"}
        else:
            applied = run_graphql(payload, args.timeout_seconds)
            apply_result_validation = validate_apply_result(
                applied,
                {str(record["baselane_id"]) for record in ready},
            )
            if not apply_result_validation["ok"]:
                applied = {**applied, "ok": False, "validation": apply_result_validation}
    report = {
        "generated_at": iso_z(),
        "mode": "apply" if args.apply else "dry_run",
        "status": "ok"
        if not blocked
        and not payload_blocked
        and (not args.apply or ((applied or {}).get("ok") and (apply_result_validation or {}).get("ok")))
        else "review",
        "policy": "pre-closing rows before 2025-05-09 are unassigned from 85-104 property so they do not flow into property-level post-closing FINANCIALS.md",
        "next_action": (
            "Review payload, authenticate Baselane CDP session, then verify readiness with "
            "python3 scripts/baselane_85104_preclosing_property_retag.py --refresh-auth-report. "
            "Only after apply_ready=true, apply with "
            f"{approval_command(digest, allow_partial_apply_with_blocked_records=args.allow_partial_apply_with_blocked_records)}; "
            "rerun Baselane sync, public financial split, coownership validation, and monthly transfer reconciliation."
        ),
        "source_index": str(args.source_index),
        "audit": str(args.audit),
        "payload": str(args.payload),
        "commands_file": str(commands_file),
        "apply_readiness_report": str(readiness_report),
        "approval_command": approval_command(
            digest,
            allow_partial_apply_with_blocked_records=args.allow_partial_apply_with_blocked_records,
        ),
        "payload_digest": digest,
        "ready_count": len(ready),
        "blocked_count": len(blocked),
        "ready_amount_sum": ready_amount_sum,
        "blocked_amount_sum": blocked_amount_sum,
        "ready_amount_by_category": sum_by(ready, "category"),
        "ready_amount_by_account": sum_by(ready, "account"),
        "ready_amount_by_date": sum_by(ready, "date"),
        "ready_record_samples": [compact_retag_record(record) for record in ready[:10]],
        "blocked_record_samples": [compact_retag_record(record) for record in blocked[:10]],
        "payload_transaction_count": len(payload["variables"]["input"]),
        "payload_validation": payload_validation,
        "payload_validation_status": payload_validation["status"],
        "payload_blocked": payload_blocked,
        "apply_allowed_env": APPLY_ENV,
        "apply_digest_env": APPLY_DIGEST_ENV,
        "apply_digest_required_value": digest,
        **readiness,
        "cdp_preflight": preflight,
        "cdp_auth_report": str(args.auth_report),
        "cdp_auth_report_refreshed": bool(args.refresh_auth_report),
        "cdp_auth_status": auth_report.get("status"),
        "cdp_auth_issue_summary": auth_report.get("issue_summary"),
        "cdp_auth_next_action": auth_report.get("next_action"),
        "cdp_verified_authenticated_tab_count": int(auth_report.get("verified_authenticated_tab_count") or 0),
        "apply_blocked_reason": (
            "Apply readiness blocked; resolve blockers before mutating upstream Baselane rows."
            if args.apply and not readiness["apply_ready"]
            else None
        ),
        "apply_result_validation": apply_result_validation,
        "protected_closing_row_count": protected_blocked_count,
        "protected_closing_row_review_csv": protected_review_csv,
        "protected_closing_row_review_json": str(args.protected_review_json),
        "protected_closing_row_review_import_commands": protected_review_import_commands,
        "protected_closing_row_review_import": protected_review_import,
        "protected_closing_row_review_validation": protected_review_validation,
        "protected_closing_row_review_status": protected_review_validation["status"],
        "records": policy_checked,
        "applied": applied,
    }
    report["report"] = str(args.report)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_readiness_report(readiness_report, report)
    print(f"status={report['status']} mode={report['mode']} ready={len(ready)} blocked={len(blocked)} report={args.report}")
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
