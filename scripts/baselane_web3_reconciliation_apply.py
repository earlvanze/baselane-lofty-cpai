#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any


ROOT = Path(__file__).absolute().parents[1]
GRAPHQL_HELPER = ROOT / "scripts" / "baselane_graphql_via_cdp.js"
DEFAULT_CONFIG = ROOT / "config" / "baselane_web3_reconciliation_events.json"
DEFAULT_REPORT = ROOT / "reports" / "baselane_web3_reconciliation_apply.json"
DEFAULT_LOCK = Path("/tmp/baselane-web3-reconciliation-apply.lock")
DEFAULT_CDP_VERSION_URL = "http://127.0.0.1:19222/json/version"


def iso_z() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def money(value: Any) -> float:
    return round(float(value), 2)


def note_text(value: Any) -> str:
    return str(value.get("text") or "") if isinstance(value, dict) else str(value or "")


def event_marker(event_id: str, leg: str) -> str:
    return f"WEB3-WEB2-RECON|{event_id}|{leg}"


def format_money(value: Any) -> str:
    amount = money(value)
    return ("-" if amount < 0 else "") + f"${abs(amount):,.2f}"


def build_note(event: dict[str, Any], row: dict[str, Any]) -> str:
    basis = event.get("reconciliation_basis") if isinstance(event.get("reconciliation_basis"), dict) else {}
    if event.get("basis_note"):
        components = f"basis: {event['basis_note']}"
    else:
        components = (
            f"components: security_deposit={format_money(basis.get('yhome_held_security_deposit', 0))}; "
            f"owner_distribution_minus_contribution={format_money(basis.get('apg_owner_distribution_net_due_from_yhome', 0))}; "
            f"less_dao_payable_to_yhome={format_money(basis.get('dao_payable_to_yhome_offset', 0))}; "
            f"net_due_from_yhome_to_dao={format_money(basis.get('net_due_from_yhome_to_dao', event.get('web2_settlement_amount')))}"
        )
    return (
        f"{event_marker(event['event_id'], row['leg'])} | {row['note_summary']} "
        f"| chain={event.get('chain')} | asset={event.get('asset')} | shares={event.get('shares')} "
        f"| reference_share_price={format_money(event.get('reference_share_price', 0))} "
        f"| web3_reference_value={format_money(event.get('web3_reference_value', 0))} "
        f"| web2_settlement_amount={format_money(event.get('web2_settlement_amount', 0))} "
        f"| {components}"
    )


def event_targets(config: dict[str, Any], event_filter: str | None = None) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for event in config.get("events") or []:
        if not isinstance(event, dict):
            continue
        if event_filter and event.get("event_id") != event_filter:
            continue
        if event.get("status") not in {"ready", "active", "applied"}:
            continue
        tag = event.get("baselane_tag") if isinstance(event.get("baselane_tag"), dict) else {}
        for row in event.get("ledger_rows") or []:
            if not isinstance(row, dict):
                continue
            targets.append(
                {
                    "event_id": event["event_id"],
                    "leg": row["leg"],
                    "marker": event_marker(event["event_id"], row["leg"]),
                    "date": row.get("date") or event["date"],
                    "amount": money(row["amount"]),
                    "merchantName": row["merchantName"],
                    "note": build_note(event, row),
                    "propertyId": str(row["property_id"]),
                    "property_name": row["property_name"],
                    "tagId": str(tag.get("id") or "25"),
                    "tag_name": str(tag.get("name") or "Owner Contributions/Distributions"),
                    "unitId": None,
                    "entityId": None,
                    "bankAccountId": None,
                    "isReviewedByUser": True,
                    "existing_transaction_id": (
                        str(row["existing_transaction_id"])
                        if row.get("existing_transaction_id")
                        else None
                    ),
                    "legacy_search": str(row.get("legacy_search") or ""),
                    "expected_existing": (
                        row.get("expected_existing")
                        if isinstance(row.get("expected_existing"), dict)
                        else None
                    ),
                }
            )
    markers = [target["marker"] for target in targets]
    duplicates = [marker for marker, count in Counter(markers).items() if count > 1]
    if duplicates:
        raise RuntimeError(f"duplicate target markers in config: {duplicates}")
    return targets


def target_digest(targets: list[dict[str, Any]]) -> str:
    stable = [
        {key: target[key] for key in ("marker", "date", "amount", "merchantName", "note", "propertyId", "tagId")}
        for target in targets
    ]
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@contextmanager
def exclusive_apply_lock(enabled: bool, path: Path):
    if not enabled:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_graphql(payload: dict[str, Any]) -> dict[str, Any]:
    env = os.environ.copy()
    env.setdefault("BASELANE_CDP_VERSION_URL", DEFAULT_CDP_VERSION_URL)
    env.setdefault("BASELANE_GQL_CREATE_TARGET", "0")
    helper_timeout_ms = int(env.get("BASELANE_GQL_TIMEOUT_MS") or "90000")
    command_timeout_ms = int(env.get("BASELANE_GQL_COMMAND_TIMEOUT_MS") or "15000")
    timeout_seconds = max(30, (helper_timeout_ms + 2 * command_timeout_ms + 10000 + 999) // 1000)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle)
        payload_path = Path(handle.name)
    try:
        proc = subprocess.run(
            ["node", str(GRAPHQL_HELPER), str(payload_path)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=timeout_seconds,
        )
    finally:
        payload_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"GraphQL helper rc={proc.returncode}")
    result = json.loads(proc.stdout)
    if result.get("errors"):
        raise RuntimeError(json.dumps(result["errors"], indent=2))
    return result


def run_graphql_batch(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not payloads:
        return []
    result = run_graphql({"batchOperations": payloads})
    # The CDP helper intentionally unwraps a one-operation batch and returns
    # the GraphQL response directly. Treat that as a one-item batch so an
    # already-successful mutation is not reported as failed merely because
    # there was only one operation.
    responses = [result] if len(payloads) == 1 and "data" in result else result.get("batchResults")
    if not isinstance(responses, list) or len(responses) != len(payloads):
        raise RuntimeError("GraphQL helper returned an invalid batch result")
    for response in responses:
        if response.get("errors"):
            raise RuntimeError(json.dumps(response["errors"], indent=2))
    return responses


def query_transactions(search: str, page_limit: int = 500) -> list[dict[str, Any]]:
    payload = {
        "operationName": "Transactions",
        "variables": {
            "input": {
                "sort": {"direction": "DESC", "field": "date"},
                "filter": {"search": search, "isHidden": False, "isDeleted": False},
                "page": 1,
                "pageLimit": page_limit,
            }
        },
        "query": """
        query Transactions($input: SortsAndFilters) {
          transactions(input: $input) {
            total
            data { id amount date merchantName bankAccountId propertyId tagId note isManual hidden isDeleted }
          }
        }
        """,
    }
    return run_graphql(payload)["data"]["transactions"].get("data") or []


def query_properties() -> list[dict[str, Any]]:
    return run_graphql({
        "operationName": "PropertyList",
        "variables": {},
        "query": "query PropertyList { property { id name address } }",
    })["data"].get("property") or []


def query_tags() -> list[dict[str, Any]]:
    return run_graphql({
        "operationName": "TagList",
        "variables": {},
        "query": "query TagList { tag { type subType { id name subType { id name subType { id name } } } } }",
    })["data"].get("tag") or []


def query_metadata() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    data = run_graphql({
        "operationName": "ReconciliationMetadata",
        "variables": {},
        "query": """
          query ReconciliationMetadata {
            property { id name address }
            tag { type subType { id name subType { id name subType { id name } } } }
          }
        """,
    })["data"]
    return data.get("property") or [], data.get("tag") or []


def transaction_filter(search: str, page_limit: int = 500) -> dict[str, Any]:
    return {
        "sort": {"direction": "DESC", "field": "date"},
        "filter": {"search": search, "isHidden": False, "isDeleted": False},
        "page": 1,
        "pageLimit": page_limit,
    }


def query_reconciliation_snapshot(
    targets: list[dict[str, Any]], marker_search: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Capture every live preflight dependency in bounded authenticated reads."""
    searches: list[str] = []
    for target in targets:
        if not target.get("existing_transaction_id"):
            continue
        search = target.get("legacy_search") or target["merchantName"]
        if search not in searches:
            searches.append(search)

    chunks = [searches[:4]] + [searches[index:index + 5] for index in range(4, len(searches), 5)]
    if not chunks:
        chunks = [[]]
    property_rows: list[dict[str, Any]] = []
    tag_rows: list[dict[str, Any]] = []
    marker_rows: list[dict[str, Any]] = []
    live_by_id: dict[str, dict[str, Any]] = {}
    payloads: list[dict[str, Any]] = []
    aliases_by_chunk: list[list[str]] = []
    search_index = 0
    for chunk_index, chunk in enumerate(chunks):
        variables: dict[str, Any] = {}
        declarations: list[str] = []
        fields: list[str] = []
        if chunk_index == 0:
            variables["markerInput"] = transaction_filter(marker_search)
            declarations.append("$markerInput: SortsAndFilters")
            fields.extend(
                [
                    "property { id name address }",
                    "tag { type subType { id name subType { id name subType { id name } } } }",
                    (
                        "markerRows: transactions(input: $markerInput) { total data { "
                        "id amount date merchantName bankAccountId propertyId tagId note isManual hidden isDeleted } }"
                    ),
                ]
            )
        aliases: list[str] = []
        for search in chunk:
            variable = f"legacyInput{search_index}"
            alias = f"legacyRows{search_index}"
            variables[variable] = transaction_filter(search)
            declarations.append(f"${variable}: SortsAndFilters")
            fields.append(
                f"{alias}: transactions(input: ${variable}) {{ total data {{ "
                "id amount date merchantName bankAccountId propertyId tagId note isManual hidden isDeleted } }"
            )
            aliases.append(alias)
            search_index += 1
        query = (
            f"query ReconciliationSnapshot({', '.join(declarations)}) {{\n"
            + "\n".join(fields)
            + "\n}"
        )
        payloads.append(
            {
                "operationName": "ReconciliationSnapshot",
                "variables": variables,
                "query": query,
            }
        )
        aliases_by_chunk.append(aliases)
    responses = run_graphql_batch(payloads) if len(payloads) > 1 else [run_graphql(payloads[0])]
    for chunk_index, (aliases, response) in enumerate(
        zip(aliases_by_chunk, responses, strict=True)
    ):
        data = response["data"]
        if chunk_index == 0:
            property_rows = data.get("property") or []
            tag_rows = data.get("tag") or []
            marker_rows = (data.get("markerRows") or {}).get("data") or []
        for alias in aliases:
            for row in (data.get(alias) or {}).get("data") or []:
                if row.get("id"):
                    live_by_id[str(row["id"])] = row
    return property_rows, tag_rows, marker_rows, live_by_id


def flatten_tags(tag_tree: list[dict[str, Any]]) -> dict[str, str]:
    tags: dict[str, str] = {}

    def visit(rows: list[dict[str, Any]]) -> None:
        for row in rows or []:
            if row.get("id") and row.get("name"):
                tags[str(row["id"])] = str(row["name"])
            visit(row.get("subType") or [])

    for root in tag_tree:
        visit(root.get("subType") or [])
    return tags


def metadata_issues(
    targets: list[dict[str, Any]],
    property_rows: list[dict[str, Any]] | None = None,
    tag_rows: list[dict[str, Any]] | None = None,
) -> list[str]:
    if property_rows is None or tag_rows is None:
        property_rows, tag_rows = query_metadata()
    properties = {str(row.get("id")): row for row in property_rows}
    tags = flatten_tags(tag_rows)
    issues: list[str] = []
    for target in targets:
        prop = properties.get(target["propertyId"])
        if not prop:
            issues.append(f"missing_property:{target['marker']}:{target['propertyId']}")
        elif str(target["property_name"]).lower() not in str(prop.get("name") or "").lower() and target["property_name"] not in str(prop):
            issues.append(f"property_name_mismatch:{target['marker']}:{target['property_name']}:{prop}")
        tag_name = tags.get(target["tagId"])
        if tag_name != target["tag_name"]:
            issues.append(f"tag_mismatch:{target['marker']}:{target['tagId']}:{tag_name}!={target['tag_name']}")
    return issues


def mutation_input(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "merchantName": target["merchantName"],
        "note": target["note"],
        "tagId": target["tagId"],
        "propertyId": target["propertyId"],
        "unitId": None,
        "entityId": None,
        "date": target["date"],
        "bankAccountId": None,
        "amount": target["amount"],
        "isReviewedByUser": True,
    }


def live_matches(target: dict[str, Any], live: dict[str, Any]) -> bool:
    return (
        money(live.get("amount") or 0) == target["amount"]
        and str(live.get("date") or "")[:10] == target["date"]
        and str(live.get("merchantName") or "") == target["merchantName"]
        and str(live.get("propertyId") or "") == target["propertyId"]
        and str(live.get("tagId") or "") == target["tagId"]
        and note_text(live.get("note")) == target["note"]
    )


def expected_existing_matches(target: dict[str, Any], live: dict[str, Any]) -> bool:
    expected = target.get("expected_existing")
    if not isinstance(expected, dict):
        return True
    comparisons = {
        "amount": lambda value: money(live.get("amount") or 0) == money(value),
        "date": lambda value: str(live.get("date") or "")[:10] == str(value),
        "merchantName": lambda value: str(live.get("merchantName") or "") == str(value),
        "propertyId": lambda value: str(live.get("propertyId") or "") == str(value),
        "tagId": lambda value: str(live.get("tagId") or "") == str(value),
        "bankAccountId": lambda value: str(live.get("bankAccountId") or "") == str(value),
    }
    return all(comparisons[key](value) for key, value in expected.items() if key in comparisons)


def create_transaction(values: dict[str, Any]) -> dict[str, Any]:
    return run_graphql({"operationName": "createTransaction", "variables": values, "query": """
      mutation createTransaction($merchantName: String!, $note: String!, $tagId: ID, $propertyId: ID, $unitId: ID, $entityId: Int, $date: String!, $bankAccountId: ID, $amount: Float!, $isReviewedByUser: Boolean) {
        createTransaction(input: { merchantName: $merchantName note: $note tagId: $tagId propertyId: $propertyId unitId: $unitId entityId: $entityId date: $date bankAccountId: $bankAccountId amount: $amount isReviewedByUser: $isReviewedByUser }) { id amount date merchantName propertyId tagId note isManual }
      }
    """})["data"]["createTransaction"]


def create_transactions_batch_payload(values_rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not values_rows:
        raise ValueError("create batch must not be empty")
    if len(values_rows) > 5:
        raise ValueError("Baselane permits at most five GraphQL aliases per request")
    declarations: list[str] = []
    fields: list[str] = []
    variables: dict[str, Any] = {}
    types = {
        "merchantName": "String!",
        "note": "String!",
        "tagId": "ID",
        "propertyId": "ID",
        "unitId": "ID",
        "entityId": "Int",
        "date": "String!",
        "bankAccountId": "ID",
        "amount": "Float!",
        "isReviewedByUser": "Boolean",
    }
    for index, values in enumerate(values_rows):
        arguments: list[str] = []
        for key, graphql_type in types.items():
            variable = f"c{index}{key[0].upper()}{key[1:]}"
            declarations.append(f"${variable}: {graphql_type}")
            variables[variable] = values.get(key)
            arguments.append(f"{key}: ${variable}")
        fields.append(
            f"c{index}: createTransaction(input: {{ {' '.join(arguments)} }}) "
            "{ id amount date merchantName propertyId tagId note isManual }"
        )
    query = f"mutation BatchCreate({', '.join(declarations)}) {{ {' '.join(fields)} }}"
    return {"operationName": "BatchCreate", "variables": variables, "query": query}


def create_transactions_batch(values_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    data = run_graphql(create_transactions_batch_payload(values_rows))["data"]
    return [data[f"c{index}"] for index in range(len(values_rows))]


def update_transaction(transaction_id: str, values: dict[str, Any]) -> dict[str, Any]:
    update = {key: values[key] for key in ("amount", "merchantName", "note", "tagId", "propertyId", "unitId")}
    update["id"] = transaction_id
    return run_graphql({"operationName": "UpdateTransaction", "variables": {"input": [update]}, "query": """
      mutation UpdateTransaction($input: [UpdateTransaction!]) { updateTransactions(input: $input) { id amount date merchantName propertyId tagId note isManual } }
    """})["data"]["updateTransactions"][0]


def update_transactions_batch_payload(rows: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    if not rows:
        raise ValueError("update batch must not be empty")
    updates: list[dict[str, Any]] = []
    for transaction_id, values in rows:
        update = dict(values)
        update["id"] = transaction_id
        updates.append(update)
    return {
        "operationName": "UpdateTransaction",
        "variables": {"input": updates},
        "query": """
          mutation UpdateTransaction($input: [UpdateTransaction!]) {
            updateTransactions(input: $input) { id amount date merchantName propertyId tagId note isManual }
          }
        """,
    }


def update_transactions_batch(rows: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return run_graphql(update_transactions_batch_payload(rows))["data"]["updateTransactions"]


def changed_update_values(target: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    values = mutation_input(target)
    changed: dict[str, Any] = {}
    if money(live.get("amount") or 0) != money(values["amount"]):
        changed["amount"] = values["amount"]
    if str(live.get("merchantName") or "") != str(values["merchantName"]):
        changed["merchantName"] = values["merchantName"]
    if note_text(live.get("note")) != values["note"]:
        changed["note"] = values["note"]
    if str(live.get("tagId") or "") != str(values["tagId"]):
        changed["tagId"] = values["tagId"]
    if str(live.get("propertyId") or "") != str(values["propertyId"]):
        changed["propertyId"] = values["propertyId"]
    if (str(live.get("unitId")) if live.get("unitId") is not None else None) != values["unitId"]:
        changed["unitId"] = values["unitId"]
    return changed


def apply_planned_actions(actions: list[dict[str, Any]]) -> None:
    updates = [action for action in actions if action["action"] == "update"]
    operations: list[dict[str, Any]] = []
    operation_specs: list[tuple[str, list[dict[str, Any]]]] = []
    if updates:
        operations.append(
            update_transactions_batch_payload(
                [(str(action["id"]), action["values"]) for action in updates]
            )
        )
        operation_specs.append(("update", updates))
    creates = [action for action in actions if action["action"] == "create"]
    for start in range(0, len(creates), 5):
        chunk = creates[start:start + 5]
        operations.append(create_transactions_batch_payload([action["values"] for action in chunk]))
        operation_specs.append(("create", chunk))
    responses = run_graphql_batch(operations)
    for (kind, action_rows), response in zip(operation_specs, responses, strict=True):
        if kind == "update":
            results = response["data"]["updateTransactions"]
            results_by_id = {str(result["id"]): result for result in results}
            for action in action_rows:
                action["result"] = results_by_id[str(action["id"])]
        else:
            data = response["data"]
            for index, action in enumerate(action_rows):
                action["result"] = data[f"c{index}"]


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    config = read_json(args.config)
    targets = event_targets(config, args.event_id)
    digest = target_digest(targets)
    if args.apply and args.require_target_digest != digest:
        raise RuntimeError(f"target digest required for apply; current digest is {digest}")
    property_rows, tag_rows, live_rows, live_by_id = query_reconciliation_snapshot(
        targets, args.search or "WEB3-WEB2-RECON"
    )
    issues = metadata_issues(targets, property_rows, tag_rows)
    by_marker: dict[str, list[dict[str, Any]]] = {}
    for target in targets:
        by_marker[target["marker"]] = [row for row in live_rows if target["marker"] in note_text(row.get("note"))]
    actions: list[dict[str, Any]] = []
    if issues:
        return {
            "generated_at": iso_z(),
            "status": "blocked",
            "mode": "apply" if args.apply else "dry_run",
            "target_count": len(targets),
            "target_digest": digest,
            "issue_count": len(issues),
            "issues": issues,
            "actions": [],
        }
    for target in targets:
        matches = by_marker.get(target["marker"], [])
        existing_id = target.get("existing_transaction_id")
        if not matches and existing_id:
            existing = live_by_id.get(existing_id)
            if not existing:
                issues.append(f"missing_expected_existing_transaction:{target['marker']}:{existing_id}")
                continue
            if not expected_existing_matches(target, existing):
                issues.append(
                    f"expected_existing_transaction_changed:{target['marker']}:{existing_id}:"
                    f"{json.dumps(existing, sort_keys=True)}"
                )
                continue
            matches = [existing]
        values = mutation_input(target)
        if len(matches) > 1:
            issues.append(f"duplicate_live_marker:{target['marker']}:{[row.get('id') for row in matches]}")
            continue
        if not matches:
            action = {"action": "create", "marker": target["marker"], "values": values}
            actions.append(action)
            continue
        live = matches[0]
        if live_matches(target, live):
            actions.append({"action": "skip", "reason": "already_current", "marker": target["marker"], "id": live.get("id")})
            continue
        action = {
            "action": "update",
            "marker": target["marker"],
            "id": live.get("id"),
            "values": changed_update_values(target, live),
        }
        actions.append(action)
    if args.apply and not issues:
        apply_planned_actions(actions)
    return {
        "generated_at": iso_z(),
        "status": "ok" if not issues else "blocked",
        "mode": "apply" if args.apply else "dry_run",
        "config": str(args.config),
        "event_id": args.event_id,
        "target_count": len(targets),
        "target_digest": digest,
        "live_candidate_count": len(live_rows),
        "action_count": len(actions),
        "create_count": sum(action["action"] == "create" for action in actions),
        "update_count": sum(action["action"] == "update" for action in actions),
        "skip_count": sum(action["action"] == "skip" for action in actions),
        "issue_count": len(issues),
        "issues": issues,
        "actions": actions,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply deterministic Web3/Web2 reconciliation rows to Baselane.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--event-id")
    parser.add_argument("--search")
    parser.add_argument("--require-target-digest")
    parser.add_argument("--apply-lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    with exclusive_apply_lock(args.apply, args.apply_lock):
        report = build_report(args)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ("status", "mode", "target_count", "target_digest", "create_count", "update_count", "skip_count", "issue_count")}, indent=2))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
