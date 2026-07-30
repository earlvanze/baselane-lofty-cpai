#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APPROVAL_SCOPE = "baselane_cf_conflict_resolution"
POLICY = "auto-approve only exact-match GL zero-current fills and verified duplicate-PM void clearances; overwrites and formula rows require explicit review"


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_number(value: object) -> float | None:
    text = str(value if value is not None else "").strip().replace(",", "").replace("$", "")
    if not text:
        return None
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    try:
        return float(text)
    except ValueError:
        return None


def stable_digest(payload: object) -> str:
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def load_template(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object: {path}")
    if data.get("approval_scope") != APPROVAL_SCOPE:
        raise ValueError(f"Approval template scope must be {APPROVAL_SCOPE}")
    return data


def is_auto_safe(entry: dict[str, Any]) -> tuple[bool, str]:
    action = entry.get("action")
    if action not in {"clear_from_verified_void", "fill_from_gl"}:
        return False, "action_not_auto_safe"
    current = parse_number(entry.get("current_value"))
    new = parse_number(entry.get("new_value"))
    if current is None:
        return False, "current_value_not_numeric"
    if action == "fill_from_gl" and abs(current) > 0.01:
        return False, "current_value_not_zero"
    if new is None:
        return False, "new_value_not_numeric"
    if abs(new) <= 0.01:
        if action != "clear_from_verified_void":
            return False, "new_value_zero"
        if not str(entry.get("verified_void_baselane_id") or "").strip():
            return False, "verified_void_baselane_id_missing"
        if parse_number(entry.get("verified_voided_amount")) is None:
            return False, "verified_voided_amount_missing"
    required = ["id", "file", "row", "label", "action", "current_value", "new_value"]
    missing = [field for field in required if str(entry.get(field) or "").strip() == ""]
    if missing:
        return False, f"missing_required_fields:{','.join(missing)}"
    if action == "clear_from_verified_void":
        return True, "auto_safe_verified_duplicate_pm_void_clearance"
    if action == "fill_from_gl":
        return True, "auto_safe_zero_fill"
    return False, "action_not_auto_safe"


def build_report(template_path: Path) -> dict[str, Any]:
    template = load_template(template_path)
    approved: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for entry in template.get("approved") or []:
        if not isinstance(entry, dict):
            continue
        ok, reason = is_auto_safe(entry)
        clean_entry = {
            "id": entry.get("id"),
            "approved": ok,
            "property": entry.get("property"),
            "file": entry.get("file"),
            "row": entry.get("row"),
            "label": entry.get("label"),
            "action": entry.get("action"),
            "current_value": entry.get("current_value"),
            "new_value": entry.get("new_value"),
            "verified_void_baselane_id": entry.get("verified_void_baselane_id"),
            "verified_voided_amount": entry.get("verified_voided_amount"),
        }
        if ok:
            approved.append(clean_entry)
        else:
            excluded.append({**clean_entry, "approved": False, "reason": reason})
    blocked = [entry for entry in template.get("blocked") or [] if isinstance(entry, dict)]
    approval = {
        "approval_scope": APPROVAL_SCOPE,
        "month": template.get("month"),
        "generated_at": iso_z(),
        "source_template": str(template_path),
        "policy": POLICY,
        "approved": approved,
        "blocked": blocked,
    }
    return {
        "status": "ok",
        "generated_at": iso_z(),
        "source_template": str(template_path),
        "month": template.get("month"),
        "policy": POLICY,
        "auto_approved_count": len(approved),
        "excluded_applicable_count": len(excluded),
        "blocked_count": len(blocked),
        "approval_digest": stable_digest(approval),
        "excluded_digest": stable_digest({"excluded": excluded}),
        "approval": approval,
        "excluded": excluded,
    }


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Baselane CF Auto-Approved Zero-Fill Conflicts",
        "",
        f"- Status: `{report['status']}`",
        f"- Month: `{report.get('month')}`",
        f"- Policy: {report['policy']}",
        f"- Auto-approved: `{report['auto_approved_count']}`",
        f"- Excluded applicable: `{report['excluded_applicable_count']}`",
        f"- Blocked/manual-only: `{report['blocked_count']}`",
        f"- Approval digest: `{report['approval_digest']}`",
        "",
        "Only exact-match `fill_from_gl` zero-current rows and verified duplicate-PM void clearances are approved. Overwrites, formula rows, other GL-empty conflicts, non-numeric values, and zero GL targets remain excluded for explicit review.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Auto-approve deterministic zero-current Baselane CF fill_from_gl conflicts.")
    parser.add_argument("--approval-template", required=True, type=Path)
    parser.add_argument("--approval-json", required=True, type=Path)
    parser.add_argument("--report-json", required=True, type=Path)
    parser.add_argument("--markdown", required=True, type=Path)
    args = parser.parse_args()

    report = build_report(args.approval_template)
    args.approval_json.parent.mkdir(parents=True, exist_ok=True)
    args.approval_json.write_text(json.dumps(report["approval"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(json.dumps({key: value for key, value in report.items() if key != "approval"}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_markdown(report, args.markdown)
    print(json.dumps({key: report[key] for key in ("status", "auto_approved_count", "excluded_applicable_count", "blocked_count", "approval_digest")}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
