#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from baselane_ecogl_data_quality_autonomy import stable_digest


ROOT = Path(os.environ.get("WORKSPACE_ROOT") or Path(__file__).absolute().parents[1])
DEFAULT_QUEUE = ROOT / "reports" / "baselane_source_cleanup_queue.json"
DEFAULT_REPORT = ROOT / "reports" / "baselane_source_cleanup_apply_report.json"
DEFAULT_PAYLOADS = ROOT / "reports" / "baselane_source_cleanup_apply_payloads.json"
DEFAULT_STATE = ROOT / "scripts" / ".baselane_native_split_apply_state.json"
DEFAULT_APPLY_STATE = ROOT / "scripts" / ".baselane_source_cleanup_apply_state.json"
DEFAULT_GRAPHQL_HELPER = ROOT / "scripts" / "baselane_graphql_via_cdp.js"
APPLY_ENV = "BASELANE_SOURCE_CLEANUP_APPLY"
ALLOWED_ACTIONS = {"delete_duplicate_split_child", "remove_no_dao_mortgage_source_row", "remove_first_day_pm_fee_source_row"}


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return [{key: str(value or "") for key, value in row.items()} for row in reader]


def decimal_amount(value: object) -> Decimal:
    raw = str(value or "0").replace("$", "").replace(",", "").strip() or "0"
    try:
        return Decimal(raw).quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0.00")


def amount_float(value: object) -> float:
    return float(decimal_amount(value))


def source_by_id(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {
        str(row.get("BaselaneId") or "").strip(): row
        for row in rows
        if str(row.get("BaselaneId") or "").strip()
    }


def normalize(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def load_applied_state(path: Path) -> dict[str, Any]:
    data = read_json(path)
    if isinstance(data, dict):
        data.setdefault("applied", {})
        return data
    return {"applied": {}}


def child_property_key(child: dict[str, Any]) -> tuple[str, str]:
    return (normalize(child.get("merchantName")), str(decimal_amount(child.get("amount"))))


def split_state_records(state_path: Path) -> list[dict[str, Any]]:
    data = read_json(state_path)
    if not isinstance(data, dict):
        return []
    records = []
    for record_id, record in (data.get("applied") or {}).items():
        if isinstance(record, dict):
            records.append({"state_record_id": record_id, **record})
    return records


def parent_records_by_duplicate_child(
    state_path: Path,
    duplicate_child_ids: set[str],
) -> dict[str, dict[str, Any]]:
    by_parent: dict[str, dict[str, Any]] = {}
    for record in split_state_records(state_path):
        response = record.get("response") if isinstance(record.get("response"), dict) else {}
        parent_id = str(response.get("parentId") or record.get("baselane_id") or "").strip()
        children = [child for child in response.get("children") or [] if isinstance(child, dict)]
        child_ids = {str(child.get("id") or "").strip() for child in children}
        matched_duplicate_ids = sorted(child_ids & duplicate_child_ids)
        if not parent_id or not matched_duplicate_ids:
            continue
        by_parent[parent_id] = {**record, "parent_id": parent_id, "matched_duplicate_ids": matched_duplicate_ids}
    return by_parent


def split_input_from_source(child_id: str, fallback: dict[str, Any], source_rows_by_id: dict[str, dict[str, str]], *, is_deleted: bool) -> tuple[dict[str, Any] | None, list[str]]:
    source = source_rows_by_id.get(child_id)
    issues: list[str] = []
    if not source:
        return None, [f"split_child_missing_current_source:{child_id}"]
    property_id = str(source.get("PropertyId") or "").strip()
    tag_id = str(source.get("TagId") or fallback.get("tagId") or "").strip()
    date = str(source.get("ISODate") or source.get("Date") or "").strip()
    if not property_id:
        issues.append(f"split_child_missing_property_id:{child_id}")
    if not tag_id:
        issues.append(f"split_child_missing_tag_id:{child_id}")
    if not date:
        issues.append(f"split_child_missing_date:{child_id}")
    row = {
        "id": child_id,
        "tagId": tag_id,
        "propertyId": property_id,
        "propertyUnitId": None,
        "date": date,
        "amount": amount_float(source.get("Amount") or fallback.get("amount")),
        "merchantName": str(source.get("Merchant") or fallback.get("merchantName") or "").strip(),
    }
    if is_deleted:
        row["isDelete"] = True
    return row, issues


def kept_children_for_parent(record: dict[str, Any], source_rows_by_id: dict[str, dict[str, str]]) -> tuple[list[dict[str, Any]], list[str], list[str], list[dict[str, Any]]]:
    response = record.get("response") if isinstance(record.get("response"), dict) else {}
    children = [child for child in response.get("children") or [] if isinstance(child, dict)]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for child in children:
        child_id = str(child.get("id") or "").strip()
        if child_id:
            grouped[child_property_key(child)].append(child)

    kept: list[dict[str, Any]] = []
    dropped_ids: list[str] = []
    issues: list[str] = []
    for group_children in grouped.values():
        ordered = sorted(group_children, key=lambda item: int(str(item.get("id") or "0")))
        kept.append(ordered[0])
        dropped_ids.extend(str(item.get("id") or "") for item in ordered[1:])

    payload_children: list[dict[str, Any]] = []
    deleted_payload_children: list[dict[str, Any]] = []
    for child in sorted(kept, key=lambda item: str(item.get("merchantName") or "")):
        child_id = str(child.get("id") or "").strip()
        row, row_issues = split_input_from_source(child_id, child, source_rows_by_id, is_deleted=False)
        issues.extend(row_issues)
        if row is None:
            continue
        payload_children.append(row)
    child_by_id = {str(child.get("id") or "").strip(): child for child in children if str(child.get("id") or "").strip()}
    for child_id in sorted(dropped_ids, key=lambda value: int(value)):
        row, row_issues = split_input_from_source(child_id, child_by_id.get(child_id, {}), source_rows_by_id, is_deleted=True)
        issues.extend(row_issues)
        if row is not None:
            deleted_payload_children.append(row)
    return payload_children, sorted(dropped_ids, key=lambda value: int(value)), issues, deleted_payload_children


def split_payload(parent_id: str, children: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "operationName": "createOrUpdateSplitTx",
        "variables": {
            "parentTransactionId": str(parent_id),
            "splitType": "AMOUNT",
            "transactionSplitInputs": children,
        },
        "query": (
            "mutation createOrUpdateSplitTx($parentTransactionId: ID!, $splitType: SplitType!, "
            "$transactionSplitInputs: [TransactionSplitInput!]!) { "
            "createOrUpdateSplitTx(input: {parentTransactionId: $parentTransactionId, "
            "transactionSplitInputs: $transactionSplitInputs, splitType: $splitType}) { "
            "id splitTransactions { id tagId propertyId amount merchantName date isDeleted parentId } } }"
        ),
    }


def unassign_mortgage_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    note = (
        "ECO cleanup: co-owner mortgage payment is not DAO operating cash for this no-DAO-mortgage property; "
        "property tag cleared to keep downstream investor reporting accurate."
    )
    return {
        "operationName": "UpdateTransaction",
        "variables": {
            "input": [
                {
                    "id": str(row["baselane_id"]),
                    "propertyId": None,
                    "unitId": None,
                    "note": note,
                    "isReviewedByUser": True,
                }
                for row in rows
            ]
        },
        "query": (
            "mutation UpdateTransaction($input: [UpdateTransaction!]) { "
            "updateTransactions(input: $input) { id tagId date propertyId unitId note amount merchantName isDeleted parentId } }"
        ),
    }


def unassign_first_day_pm_fee_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    note = (
        "ECO cleanup: legacy 1st-day AOPS PM-fee accrual is not DAO operating cash; "
        "property tag cleared to prevent PM-fee double-counting in downstream investor reporting."
    )
    return {
        "operationName": "UpdateTransaction",
        "variables": {
            "input": [
                {
                    "id": str(row["baselane_id"]),
                    "propertyId": None,
                    "unitId": None,
                    "note": note,
                    "isReviewedByUser": True,
                }
                for row in rows
            ]
        },
        "query": (
            "mutation UpdateTransaction($input: [UpdateTransaction!]) { "
            "updateTransactions(input: $input) { id tagId date propertyId unitId note amount merchantName isDeleted parentId } }"
        ),
    }


def action_digest(action: dict[str, Any]) -> str:
    return stable_digest(
        {
            "action": action.get("action"),
            "baselane_id": action.get("baselane_id"),
            "date": action.get("date"),
            "property": action.get("property"),
            "amount": str(decimal_amount(action.get("amount"))),
            "merchant": action.get("merchant"),
        }
    )


def native_split_parent_ids(split_state: dict[str, Any]) -> set[str]:
    applied = split_state.get("applied") if isinstance(split_state.get("applied"), dict) else {}
    parent_ids = set()
    for record in applied.values():
        if not isinstance(record, dict) or str(record.get("rule") or "") != "no_dao_mortgage_statement_split":
            continue
        response = record.get("response") if isinstance(record.get("response"), dict) else {}
        parent_id = str(record.get("baselane_id") or response.get("parentId") or "").strip()
        if parent_id:
            parent_ids.add(parent_id)
    return parent_ids


def is_native_mortgage_parent_action(action: dict[str, Any], split_parent_ids: set[str]) -> bool:
    if action.get("action") != "remove_no_dao_mortgage_source_row":
        return False
    if str(action.get("baselane_id") or "").strip() not in split_parent_ids:
        return False
    text = normalize(
        " ".join(
            str(action.get(key) or "")
            for key in ("merchant", "description", "category", "subcategory", "reason")
        )
    )
    return any(token in text for token in ("mtg", "mortgage", "freedom", "newrez", "loandepot", "loan depot"))


def validate_queue(queue: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if queue.get("status") != "ready":
        issues.append(f"queue_status_not_ready:{queue.get('status')}")
    if int(queue.get("missing_id_count") or 0) != 0:
        issues.append(f"queue_missing_id_count:{queue.get('missing_id_count')}")
    for action in queue.get("actions") or []:
        if action.get("action") not in ALLOWED_ACTIONS:
            issues.append(f"action_not_allowed:{action.get('action')}")
        if not str(action.get("baselane_id") or "").strip():
            issues.append(f"action_missing_baselane_id:{action.get('id')}")
    return issues


def build_plan(queue_path: Path, source_index: Path | None, split_state: Path) -> dict[str, Any]:
    queue = read_json(queue_path)
    if not isinstance(queue, dict):
        return {"status": "review", "issues": [f"queue_unreadable:{queue_path}"], "records": []}
    queue_issues = validate_queue(queue)
    source_path = source_index or Path(queue.get("source_index") or "")
    source_rows = read_csv(source_path) if source_path.is_file() else []
    rows_by_id = source_by_id(source_rows)
    actions = [action for action in queue.get("actions") or [] if isinstance(action, dict)]
    duplicate_actions = [action for action in actions if action.get("action") == "delete_duplicate_split_child"]
    mortgage_actions = [action for action in actions if action.get("action") == "remove_no_dao_mortgage_source_row"]
    split_state_payload = read_json(split_state)
    split_parent_ids = native_split_parent_ids(split_state_payload if isinstance(split_state_payload, dict) else {})
    native_mortgage_parent_actions = [
        action for action in mortgage_actions if is_native_mortgage_parent_action(action, split_parent_ids)
    ]
    mortgage_cleanup_actions = [action for action in mortgage_actions if action not in native_mortgage_parent_actions]
    first_day_pm_fee_actions = [action for action in actions if action.get("action") == "remove_first_day_pm_fee_source_row"]
    duplicate_ids = {str(action.get("baselane_id") or "").strip() for action in duplicate_actions}
    parent_records = parent_records_by_duplicate_child(split_state, duplicate_ids)

    records: list[dict[str, Any]] = []
    payloads: list[dict[str, Any]] = []
    covered_duplicate_ids: set[str] = set()
    for parent_id, record in sorted(parent_records.items()):
        children, dropped_ids, issues, deleted_children = kept_children_for_parent(record, rows_by_id)
        covered_duplicate_ids.update(set(dropped_ids) & duplicate_ids)
        queued_ids = sorted(set(record.get("matched_duplicate_ids") or []))
        ready = not issues and bool(children) and set(queued_ids).issubset(set(dropped_ids))
        payload = split_payload(parent_id, children + deleted_children)
        payloads.append({"kind": "replace_split_children", "parent_id": parent_id, "payload": payload})
        records.append(
            {
                "kind": "replace_split_children",
                "status": "ready_to_apply" if ready else "blocked",
                "parent_id": parent_id,
                "rule": record.get("rule"),
                "queued_duplicate_child_ids": queued_ids,
                "dropped_child_ids": dropped_ids,
                "retained_child_ids": [child["id"] for child in children],
                "deleted_child_ids": [child["id"] for child in deleted_children],
                "retained_child_count": len(children),
                "dropped_child_count": len(dropped_ids),
                "deleted_child_count": len(deleted_children),
                "issues": issues
                + ([] if set(queued_ids).issubset(set(dropped_ids)) else ["queued_duplicate_not_in_dropped_set"]),
                "payload_digest": stable_digest(payload),
            }
        )

    for action in duplicate_actions:
        duplicate_id = str(action.get("baselane_id") or "").strip()
        if duplicate_id and duplicate_id not in covered_duplicate_ids:
            records.append(
                {
                    "kind": "replace_split_children",
                    "status": "blocked",
                    "queued_duplicate_child_ids": [duplicate_id],
                    "issues": ["duplicate_child_not_found_in_native_split_state"],
                    "source_action": action,
                }
            )

    missing_source_ids = [
        str(action.get("baselane_id") or "").strip()
        for action in mortgage_cleanup_actions
        if str(action.get("baselane_id") or "").strip() not in rows_by_id
    ]
    if native_mortgage_parent_actions:
        records.append(
            {
                "kind": "native_split_no_dao_mortgage_parent_guard",
                "status": "blocked",
                "baselane_ids": [str(action.get("baselane_id")) for action in native_mortgage_parent_actions],
                "row_count": len(native_mortgage_parent_actions),
                "issues": ["native_mortgage_split_required_or_already_handled"],
                "source_actions": native_mortgage_parent_actions,
            }
        )
    if mortgage_cleanup_actions:
        payload = unassign_mortgage_payload(mortgage_cleanup_actions)
        payloads.append({"kind": "unassign_no_dao_mortgage", "payload": payload})
        records.append(
            {
                "kind": "unassign_no_dao_mortgage",
                "status": "ready_to_apply" if not missing_source_ids else "blocked",
                "baselane_ids": [str(action.get("baselane_id")) for action in mortgage_cleanup_actions],
                "row_count": len(mortgage_cleanup_actions),
                "issues": [f"mortgage_source_id_missing:{item}" for item in missing_source_ids],
                "payload_digest": stable_digest(payload),
            }
        )

    missing_pm_fee_source_ids = [
        str(action.get("baselane_id") or "").strip()
        for action in first_day_pm_fee_actions
        if str(action.get("baselane_id") or "").strip() not in rows_by_id
    ]
    if first_day_pm_fee_actions:
        payload = unassign_first_day_pm_fee_payload(first_day_pm_fee_actions)
        payloads.append({"kind": "unassign_first_day_pm_fee", "payload": payload})
        records.append(
            {
                "kind": "unassign_first_day_pm_fee",
                "status": "ready_to_apply" if not missing_pm_fee_source_ids else "blocked",
                "baselane_ids": [str(action.get("baselane_id")) for action in first_day_pm_fee_actions],
                "row_count": len(first_day_pm_fee_actions),
                "issues": [f"first_day_pm_fee_source_id_missing:{item}" for item in missing_pm_fee_source_ids],
                "payload_digest": stable_digest(payload),
            }
        )

    blocked_count = sum(1 for record in records if record.get("status") != "ready_to_apply")
    return {
        "generated_at": iso_z(),
        "status": "review" if queue_issues or blocked_count else "ready",
        "queue": str(queue_path),
        "source_index": str(source_path),
        "split_state": str(split_state),
        "queue_action_count": len(actions),
        "queue_duplicate_action_count": len(duplicate_actions),
        "queue_mortgage_action_count": len(mortgage_actions),
        "queue_native_mortgage_parent_guard_count": len(native_mortgage_parent_actions),
        "queue_first_day_pm_fee_action_count": len(first_day_pm_fee_actions),
        "ready_record_count": sum(1 for record in records if record.get("status") == "ready_to_apply"),
        "blocked_record_count": blocked_count,
        "issues": queue_issues,
        "records": records,
        "payloads": payloads,
    }


def execute_payload(helper: Path, payload: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle)
        payload_path = Path(handle.name)
    try:
        completed = subprocess.run(
            ["node", str(helper), str(payload_path)],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
        parsed = None
        try:
            parsed = json.loads(completed.stdout) if completed.stdout.strip() else None
        except Exception:
            parsed = None
        return {
            "return_code": completed.returncode,
            "stdout_json": parsed,
            "stdout_tail": (completed.stdout or "")[-2000:],
            "stderr_tail": (completed.stderr or "")[-4000:],
        }
    finally:
        try:
            payload_path.unlink()
        except FileNotFoundError:
            pass


def apply_plan(
    plan: dict[str, Any],
    helper: Path,
    apply_state_path: Path,
    apply: bool,
    timeout_seconds: int,
) -> dict[str, Any]:
    apply_allowed = apply and os.environ.get(APPLY_ENV) == "1"
    applied_state = load_applied_state(apply_state_path)
    executions: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    blocked_records = [record for record in plan.get("records") or [] if record.get("status") != "ready_to_apply"]
    guard_only_blocked = bool(blocked_records) and all(
        record.get("kind") == "native_split_no_dao_mortgage_parent_guard"
        for record in blocked_records
    )
    apply_plan_ready = plan.get("status") == "ready" or (
        plan.get("status") == "review"
        and guard_only_blocked
        and bool(plan.get("payloads"))
    )
    if apply and not apply_allowed:
        failures.append({"error": f"--apply requires {APPLY_ENV}=1"})
    elif apply and not apply_plan_ready:
        failures.append({"error": "plan_not_ready"})
    elif apply:
        for item in plan.get("payloads") or []:
            digest = stable_digest(item.get("payload"))
            if digest in applied_state.get("applied", {}):
                executions.append({"kind": item.get("kind"), "status": "already_applied", "payload_digest": digest})
                continue
            result = execute_payload(helper, item["payload"], timeout_seconds)
            execution = {"kind": item.get("kind"), "status": "applied" if result["return_code"] == 0 else "failed", "payload_digest": digest, "result": result}
            executions.append(execution)
            if result["return_code"] != 0:
                failures.append(execution)
                break
            applied_state.setdefault("applied", {})[digest] = {"applied_at": iso_z(), "kind": item.get("kind")}
            write_json(apply_state_path, applied_state)
    return {
        "mode": "apply" if apply else "dry_run",
        "apply_allowed": apply_allowed,
        "guard_only_blocked": guard_only_blocked,
        "apply_plan_ready": apply_plan_ready,
        "execution_count": len(executions),
        "applied_count": sum(1 for item in executions if item.get("status") == "applied"),
        "already_applied_count": sum(1 for item in executions if item.get("status") == "already_applied"),
        "failed_count": len(failures),
        "executions": executions,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply guarded Baselane source cleanup queue.")
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--source-index", type=Path)
    parser.add_argument("--split-state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--apply-state", type=Path, default=DEFAULT_APPLY_STATE)
    parser.add_argument("--graphql-helper", type=Path, default=DEFAULT_GRAPHQL_HELPER)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--payloads", type=Path, default=DEFAULT_PAYLOADS)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    plan = build_plan(args.queue, args.source_index, args.split_state)
    write_json(args.payloads, plan.get("payloads") or [])
    result = apply_plan(plan, args.graphql_helper, args.apply_state, args.apply, args.timeout_seconds)
    report = {
        "generated_at": iso_z(),
        "status": "ok" if plan.get("status") == "ready" and result["failed_count"] == 0 else "review",
        "policy": (
            "Dry-run by default. Live Baselane mutation requires --apply plus "
            f"{APPLY_ENV}=1. Duplicate split cleanup replaces each affected parent with the lowest-ID "
            "child per property/amount; no-DAO mortgage and 1st-day PM-fee cleanup clear property tags "
            "instead of deleting bank rows."
        ),
        "plan": plan,
        "apply_result": result,
    }
    write_json(args.report, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "mode": result["mode"],
                "plan_status": plan.get("status"),
                "ready_record_count": plan.get("ready_record_count"),
                "blocked_record_count": plan.get("blocked_record_count"),
                "payload_count": len(plan.get("payloads") or []),
                "applied_count": result.get("applied_count"),
                "failed_count": result.get("failed_count"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
