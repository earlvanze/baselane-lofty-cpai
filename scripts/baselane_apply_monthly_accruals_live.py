#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
GRAPHQL_HELPER = ROOT / "scripts" / "baselane_graphql_via_cdp.js"
DEFAULT_GL = Path("/mnt/c/Users/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv")
DEFAULT_APPLY_LOCK = Path("/tmp/baselane-monthly-accruals-live-apply.lock")
DEFAULT_PIPELINE_LOCK = Path(os.environ.get("BASELANE_SOURCE_PIPELINE_LOCK", "/tmp/baselane-source-pipeline.lock"))
PIPELINE_LOCK_HELD_ENV = "BASELANE_SOURCE_PIPELINE_LOCK_HELD"
MARKER_RE = re.compile(
    r"(?P<prefix>AOPS-[A-Z]+-ACCRUAL)\|(?P<kind>[a-z_]+)\|"
    r"(?P<property>[^|]+)\|(?P<month>\d{4}-\d{2})\|(?P<amount>\d+(?:\.\d+)?)"
)
PM_FEE_MARKER_RE = re.compile(
    r"(?P<prefix>AOPS-PM-FEE)\|(?:(?P<kind>pm_(?:dao|eco))\|)?(?P<property>[^|]+)\|"
    r"(?P<month>\d{4}-\d{2})\|(?P<amount>\d+(?:\.\d+)?)"
)
PM_ACCRUAL_KINDS = {"pm", "pm_dao", "pm_eco"}
DAO_ECO_KIND = "dao_eco"
ECO_ACCRUAL_KINDS = {"pm_eco", DAO_ECO_KIND}
ECO_PROPERTY_ID = "37648"
# Positive ``pm_settlement`` entries were an obsolete workaround for a cash
# payment after a DAO-side PM accrual.  Paired DAO-expense/ECO-revenue accruals
# now carry the P&L treatment, while the actual bank transfer is mirrored in
# the transfer ledger.  Publishing the legacy reversal would understate PM.
DEPRECATED_ACCRUAL_KINDS = {"pm_settlement"}


class PipelineLockBusy(RuntimeError):
    pass


def iso_z() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def note_text(value: Any) -> str:
    return str(value.get("text") or "") if isinstance(value, dict) else str(value or "")


def parse_marker(notes: str) -> dict[str, str] | None:
    match = MARKER_RE.search(notes or "")
    if match:
        result = match.groupdict()
    else:
        pm_fee_match = PM_FEE_MARKER_RE.search(notes or "")
        if not pm_fee_match:
            return None
        result = pm_fee_match.groupdict()
        result["kind"] = result.get("kind") or "pm"
    result["property"] = result["property"].strip()
    if result["prefix"] == "AOPS-PNL-ACCRUAL" and result["kind"] == "legal":
        result["kind"] = "dao"
    if result["prefix"] == "AOPS-PM-FEE" and result["kind"] == "pm":
        result["key"] = "|".join(result[name] for name in ("prefix", "property", "month"))
    else:
        result["key"] = "|".join(result[name] for name in ("prefix", "kind", "property", "month"))
    return result


def marker_kind_in_scope(kind: str, kind_filters: set[str] | None) -> bool:
    """Let the existing ``--kind pm`` selector include both paired PM sides."""
    return (
        not kind_filters
        or kind in kind_filters
        or (kind in PM_ACCRUAL_KINDS and "pm" in kind_filters)
        or (kind == DAO_ECO_KIND and "dao" in kind_filters)
    )


def parse_date(value: str) -> dt.date:
    for pattern in ("%B %d, %Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value.strip(), pattern).date()
        except ValueError:
            continue
    raise ValueError(f"unsupported accrual date: {value}")


def read_targets(
    gl_path: Path,
    month: str,
    property_filters: list[str] | None = None,
    kind_filters: set[str] | None = None,
) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    with gl_path.open(newline="", encoding="utf-8-sig") as handle:
        for csv_row, row in enumerate(csv.DictReader(handle), start=2):
            marker = parse_marker(str(row.get("Notes") or ""))
            if not marker or marker["month"] != month:
                continue
            if marker["kind"] in DEPRECATED_ACCRUAL_KINDS:
                continue
            if not marker_kind_in_scope(marker["kind"], kind_filters):
                continue
            if property_filters:
                property_key = normalize_property(marker["property"])
                filter_keys = [normalize_property(value) for value in property_filters]
                if not any(key in property_key or property_key in key for key in filter_keys):
                    continue
            amount = round(float(str(row.get("Amount") or "0").replace(",", "")), 2)
            if amount == 0 or (amount > 0 and marker["kind"] not in {"pm_eco", DAO_ECO_KIND}):
                continue
            tag_hint = str(row.get("Category") or "").strip() if marker["kind"] in {"pm_eco", DAO_ECO_KIND} else str(row.get("Sub-category") or row.get("Category") or "").strip()
            targets.append(
                {
                    "csv_row": csv_row,
                    "marker": marker,
                    "marker_key": marker["key"],
                    "baselane_property": str(row.get("Property") or "").strip(),
                    "merchantName": row.get("Merchant") or row.get("Description") or "Monthly accrual",
                    "note": row.get("Notes") or "",
                    "tag_hint": tag_hint,
                    "date": parse_date(str(row.get("Date") or "")).isoformat(),
                    "amount": amount,
                }
            )
    targets.sort(key=lambda row: row["marker_key"])
    keys = [row["marker_key"] for row in targets]
    duplicates = sorted(key for key, count in Counter(keys).items() if count > 1)
    if duplicates:
        raise RuntimeError(f"duplicate local accrual markers: {duplicates[:5]}")
    return targets


def target_digest(targets: list[dict[str, Any]]) -> str:
    bounded = [
        {key: row[key] for key in ("marker_key", "baselane_property", "merchantName", "note", "tag_hint", "date", "amount")}
        for row in targets
    ]
    return hashlib.sha256(json.dumps(bounded, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@contextmanager
def exclusive_pipeline_lock(enabled: bool, path: Path = DEFAULT_PIPELINE_LOCK):
    """Acquire the source-pipeline lock without waiting when mutations are live."""
    if not enabled:
        yield True
        return
    if os.environ.get(PIPELINE_LOCK_HELD_ENV) == "1":
        yield True
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_apply_lock(enabled: bool, path: Path = DEFAULT_APPLY_LOCK):
    with exclusive_pipeline_lock(enabled) as pipeline_lock_acquired:
        if not pipeline_lock_acquired:
            raise PipelineLockBusy(str(DEFAULT_PIPELINE_LOCK))
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


def target_metadata_issues(issues: list[str], targets: list[dict[str, Any]]) -> list[str]:
    properties = {target["marker"]["property"] for target in targets}
    kinds = {target["marker"]["kind"] for target in targets}
    scoped: list[str] = []
    for issue in issues:
        if issue.startswith("ambiguous_property_id:"):
            if issue.split(":", 2)[1] in properties:
                scoped.append(issue)
            continue
        if issue.startswith("ambiguous_tag_id:"):
            if issue.split(":", 2)[1] in kinds:
                scoped.append(issue)
            continue
        scoped.append(issue)
    return scoped


def run_graphql(payload: dict[str, Any]) -> dict[str, Any]:
    helper_timeout_ms = int(os.environ.get("BASELANE_GQL_TIMEOUT_MS") or "90000")
    command_timeout_ms = int(os.environ.get("BASELANE_GQL_COMMAND_TIMEOUT_MS") or "15000")
    timeout_seconds = max(30, (helper_timeout_ms + 2 * command_timeout_ms + 10000 + 999) // 1000)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle)
        payload_path = Path(handle.name)
    try:
        try:
            proc = subprocess.run(
                ["node", str(GRAPHQL_HELPER), str(payload_path)],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=os.environ.copy(),
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"BASELANE_GRAPHQL_HELPER_TIMEOUT after {timeout_seconds}s"
            ) from exc
    finally:
        payload_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"GraphQL helper rc={proc.returncode}")
    result = json.loads(proc.stdout)
    if result.get("errors"):
        raise RuntimeError(json.dumps(result["errors"], indent=2))
    return result


def query_transactions(search: str, page_limit: int = 1000) -> list[dict[str, Any]]:
    page_limit = min(max(int(page_limit), 10), 1000)
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = {
            "operationName": "Transactions",
            "variables": {"input": {"sort": {"direction": "DESC", "field": "date"}, "filter": {"search": search, "isHidden": False, "isDeleted": False}, "page": page, "pageLimit": page_limit}},
            "query": """
            query Transactions($input: SortsAndFilters) {
              transactions(input: $input) {
                total
                data { id amount date merchantName propertyId tagId note isManual hidden isDeleted }
              }
            }
            """,
        }
        result = run_graphql(payload)["data"]["transactions"]
        batch = result.get("data") or []
        rows.extend(batch)
        total = int(result.get("total") or 0)
        if not batch or len(rows) >= total or len(batch) < page_limit:
            return rows
        page += 1


def query_properties() -> list[dict[str, Any]]:
    payload = {
        "operationName": "PropertyList",
        "variables": {},
        "query": "query PropertyList { property { id name address } }",
    }
    return run_graphql(payload)["data"].get("property") or []


def query_tags() -> list[dict[str, Any]]:
    payload = {
        "operationName": "TagList",
        "variables": {},
        "query": "query TagList { tag { type subType { id name subType { id name subType { id name } } } } }",
    }
    return run_graphql(payload)["data"].get("tag") or []


def flatten_tags(tag_tree: list[dict[str, Any]]) -> list[dict[str, str]]:
    flattened: list[dict[str, str]] = []

    def visit(rows: list[dict[str, Any]]) -> None:
        for row in rows or []:
            tag_id = str(row.get("id") or "")
            name = str(row.get("name") or "").strip()
            if tag_id and name:
                flattened.append({"id": tag_id, "name": name})
            visit(row.get("subType") or [])

    for root in tag_tree:
        visit(root.get("subType") or [])
    return flattened


def match_tag_id(tag_hint: str, live_tags: list[dict[str, str]]) -> str | None:
    target = normalize_property(tag_hint)
    matches = sorted({row["id"] for row in live_tags if normalize_property(row.get("name")) == target})
    return matches[0] if len(matches) == 1 else None


def normalize_property(value: Any) -> str:
    text = str(value or "").lower().replace("&", " and ")
    replacements = {"avenue": "ave", "street": "st", "road": "rd", "lane": "ln", "place": "pl", "drive": "dr", "north": "n", "south": "s", "east": "e", "west": "w"}
    text = re.sub(r"[^a-z0-9]+", " ", text)
    for source, target in replacements.items():
        text = re.sub(rf"\b{source}\b", target, text)
    return re.sub(r"\s+", " ", text).strip()


def match_property_id(property_name: str, live_properties: list[dict[str, Any]]) -> str | None:
    target = normalize_property(property_name)
    target_numbers = set(re.findall(r"\b\d+\b", target))
    target_tokens = set(target.split())
    matches: list[tuple[int, str]] = []
    for row in live_properties:
        candidate = normalize_property(f"{row.get('name') or ''} {row.get('address') or ''}")
        candidate_numbers = set(re.findall(r"\b\d+\b", candidate))
        if target_numbers and not target_numbers.intersection(candidate_numbers):
            continue
        shared = target_tokens.intersection(candidate.split())
        if len(shared) < 2:
            continue
        score = len(shared) * 10 + (100 if target in candidate or candidate in target else 0)
        matches.append((score, str(row.get("id") or "")))
    matches = [item for item in matches if item[1]]
    matches.sort(reverse=True)
    if not matches or (len(matches) > 1 and matches[0][0] == matches[1][0]):
        return None
    return matches[0][1]


def resolve_target_property_id(target: dict[str, Any], live_properties: list[dict[str, Any]]) -> str | None:
    """Post both ECO revenue sides to ECO, while retaining the DAO in the marker."""
    if target["marker"]["kind"] in ECO_ACCRUAL_KINDS:
        return ECO_PROPERTY_ID
    property_name = target.get("baselane_property") or target["marker"]["property"]
    return match_property_id(property_name, live_properties)


def live_metadata(rows: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, str], dict[str, list[dict[str, Any]]], list[str]]:
    property_candidates: dict[str, Counter[str]] = defaultdict(Counter)
    tag_candidates: dict[str, Counter[str]] = defaultdict(Counter)
    by_marker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        marker = parse_marker(note_text(row.get("note")))
        if not marker:
            continue
        property_id = str(row.get("propertyId") or "")
        tag_id = str(row.get("tagId") or "")
        if property_id:
            property_candidates[marker["property"]][property_id] += 1
        if tag_id:
            tag_candidates[marker["kind"]][tag_id] += 1
        by_marker[marker["key"]].append(row)
        # The paired PM model renamed the DAO expense side from ``pm`` to
        # ``pm_dao``. Treat an existing negative legacy row as the same live
        # identity so a migration updates it in place instead of creating a
        # duplicate expense. Positive PM rows are deliberately not aliased.
        if marker["kind"] == "pm" and round(float(row.get("amount") or 0), 2) < 0:
            alias_key = "|".join(
                (marker["prefix"], "pm_dao", marker["property"], marker["month"])
            )
            by_marker[alias_key].append(row)
    issues: list[str] = []
    properties: dict[str, str] = {}
    tags: dict[str, str] = {}
    for name, candidates in property_candidates.items():
        if len(candidates) == 1:
            properties[name] = next(iter(candidates))
        else:
            issues.append(f"ambiguous_property_id:{name}:{sorted(candidates)}")
    for kind, candidates in tag_candidates.items():
        if len(candidates) == 1:
            tags[kind] = next(iter(candidates))
        else:
            issues.append(f"ambiguous_tag_id:{kind}:{sorted(candidates)}")
    return properties, tags, by_marker, issues


def live_matches(target: dict[str, Any], live: dict[str, Any], property_id: str, tag_id: str) -> bool:
    return (
        round(float(live.get("amount") or 0), 2) == target["amount"]
        and str(live.get("merchantName") or "") == target["merchantName"]
        and str(live.get("propertyId") or "") == property_id
        and str(live.get("tagId") or "") == tag_id
        and note_text(live.get("note")) == target["note"]
    )


def mutation_input(target: dict[str, Any], property_id: str, tag_id: str) -> dict[str, Any]:
    return {"merchantName": target["merchantName"], "note": target["note"], "tagId": tag_id, "propertyId": property_id, "unitId": None, "entityId": None, "date": target["date"], "bankAccountId": None, "amount": target["amount"], "isReviewedByUser": True}


def create_transaction(values: dict[str, Any]) -> dict[str, Any]:
    return run_graphql({"operationName": "createTransaction", "variables": values, "query": """
      mutation createTransaction($merchantName: String!, $note: String!, $tagId: ID, $propertyId: ID, $unitId: ID, $entityId: Int, $date: String!, $bankAccountId: ID, $amount: Float!, $isReviewedByUser: Boolean) {
        createTransaction(input: { merchantName: $merchantName note: $note tagId: $tagId propertyId: $propertyId unitId: $unitId entityId: $entityId date: $date bankAccountId: $bankAccountId amount: $amount isReviewedByUser: $isReviewedByUser }) { id amount propertyId tagId note }
      }
    """})["data"]["createTransaction"]


MAX_CREATE_ALIASES = 5


def create_transaction_batch(values_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create no more than Baselane's supported number of aliased mutations."""
    if not values_list:
        return []
    if len(values_list) > MAX_CREATE_ALIASES:
        raise ValueError(f"create batch exceeds Baselane alias limit ({MAX_CREATE_ALIASES})")
    declarations: list[str] = []
    aliases: list[str] = []
    variables: dict[str, Any] = {}
    scalar_types = {
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
    for index, values in enumerate(values_list):
        assignments: list[str] = []
        for field, graphql_type in scalar_types.items():
            variable_name = f"{field}{index}"
            declarations.append(f"${variable_name}: {graphql_type}")
            variables[variable_name] = values.get(field)
            assignments.append(f"{field}: ${variable_name}")
        aliases.append(
            f"create{index}: createTransaction(input: {{ {' '.join(assignments)} }}) "
            "{ id amount propertyId tagId note }"
        )
    payload = {
        "operationName": "CreateTransactions",
        "variables": variables,
        "query": (
            f"mutation CreateTransactions({', '.join(declarations)}) "
            f"{{ {' '.join(aliases)} }}"
        ),
    }
    data = run_graphql(payload)["data"]
    return [data[f"create{index}"] for index in range(len(values_list))]


def create_transactions(values_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create accruals in Baselane-compatible aliased mutation batches."""
    created: list[dict[str, Any]] = []
    for start in range(0, len(values_list), MAX_CREATE_ALIASES):
        created.extend(create_transaction_batch(values_list[start : start + MAX_CREATE_ALIASES]))
    return created


def update_transaction(transaction_id: str, values: dict[str, Any]) -> dict[str, Any]:
    update = {key: values[key] for key in ("amount", "merchantName", "note", "tagId", "propertyId", "unitId")}
    update["id"] = transaction_id
    return run_graphql({"operationName": "UpdateTransaction", "variables": {"input": [update]}, "query": """
      mutation UpdateTransaction($input: [UpdateTransaction!]) { updateTransactions(input: $input) { id amount merchantName propertyId tagId note } }
    """})["data"]["updateTransactions"][0]


def run(args: argparse.Namespace) -> int:
    with exclusive_apply_lock(args.apply, args.apply_lock):
        kind_filters = {value.strip() for value in args.kind_filters or [] if value.strip()} or None
        targets = read_targets(args.gl_csv, args.month, args.property_filters, kind_filters)
        digest = target_digest(targets)
        if args.apply and args.require_target_digest != digest:
            raise RuntimeError(f"target digest required for apply; current digest is {digest}")
        live_query_page_limit = int(os.environ.get("BASELANE_LIVE_ACCRUAL_QUERY_PAGE_LIMIT") or "1000")
        # Scope the live read to the selected month. The global AOPS marker
        # population is large enough to make repeated idempotency reads time
        # out, while every target identity includes YYYY-MM and can therefore
        # be resolved from this bounded result set.
        live_rows = query_transactions(args.month, page_limit=live_query_page_limit)
        properties, tags, by_marker, metadata_issues = live_metadata(live_rows)
        if tags.get("pm"):
            tags["pm_dao"] = tags["pm"]
            tags["pm_settlement"] = tags["pm"]
        metadata_issues = target_metadata_issues(metadata_issues, targets)
        # Property IDs inferred from markers are unsafe for paired accruals:
        # the same marker property intentionally appears once under the DAO
        # and once under ECO. Resolve every non-ECO target from the canonical
        # live property catalog instead.
        need_property_catalog = any(
            target["marker"]["kind"] not in ECO_ACCRUAL_KINDS
            for target in targets
        )
        live_properties = query_properties() if need_property_catalog else []
        unresolved_tag_kinds = {
            target["marker"]["kind"]
            for target in targets
            if target["marker"]["kind"] not in tags
        }
        live_tags = flatten_tags(query_tags()) if unresolved_tag_kinds else []
        tag_resolution: dict[str, dict[str, str]] = {}
        for target in targets:
            property_name = target["marker"]["property"]
            if target["marker"]["kind"] in ECO_ACCRUAL_KINDS:
                property_id = ECO_PROPERTY_ID
            else:
                property_id = match_property_id(property_name, live_properties)
            target["resolved_property_id"] = property_id
            if property_id:
                properties[property_name] = property_id
                metadata_issues = [
                    issue
                    for issue in metadata_issues
                    if not issue.startswith(f"ambiguous_property_id:{property_name}:")
                ]
            kind = target["marker"]["kind"]
            tag_hint = target.get("tag_hint") or ""
            if kind not in tags and tag_hint:
                tag_id = match_tag_id(tag_hint, live_tags)
                if tag_id:
                    tags[kind] = tag_id
                    tag_resolution[kind] = {"source": "tag_hint_exact_name", "tag_hint": tag_hint, "tag_id": tag_id}
        actions: list[dict[str, Any]] = []
        issues = list(metadata_issues)
        for target in targets:
            marker = target["marker"]
            property_id = target.get("resolved_property_id") or properties.get(marker["property"])
            tag_id = tags.get(marker["kind"])
            matches = by_marker.get(target["marker_key"], [])
            if not property_id:
                issues.append(f"missing_property_id:{marker['property']}")
                continue
            if not tag_id:
                issues.append(f"missing_tag_id:{marker['kind']}")
                continue
            if len(matches) > 1:
                issues.append(f"duplicate_live_marker:{target['marker_key']}:{len(matches)}")
                continue
            values = mutation_input(target, property_id, tag_id)
            if matches and live_matches(target, matches[0], property_id, tag_id):
                actions.append({"action": "skip", "reason": "already_current", "marker_key": target["marker_key"], "id": matches[0].get("id")})
            elif matches:
                action = {"action": "update", "marker_key": target["marker_key"], "id": matches[0].get("id"), "values": values}
                actions.append(action)
            else:
                action = {"action": "create", "marker_key": target["marker_key"], "values": values}
                actions.append(action)
        if args.apply and not issues:
            for action in actions:
                if action["action"] == "update":
                    action["result"] = update_transaction(str(action["id"]), action["values"])
            create_actions = [action for action in actions if action["action"] == "create"]
            created = create_transactions([action["values"] for action in create_actions])
            for action, result in zip(create_actions, created, strict=True):
                action["result"] = result
    report = {
        "generated_at": iso_z(), "status": "ok" if not issues else "blocked", "mode": "apply" if args.apply else "dry_run",
        "month": args.month, "gl_csv": str(args.gl_csv), "property_filters": args.property_filters,
        "kind_filters": sorted(kind_filters or []), "target_count": len(targets), "target_digest": digest,
        "live_aops_row_count": len(live_rows), "action_count": len(actions), "create_count": sum(a["action"] == "create" for a in actions),
        "update_count": sum(a["action"] == "update" for a in actions), "skip_count": sum(a["action"] == "skip" for a in actions),
        "issue_count": len(issues), "issues": issues, "tag_resolution": tag_resolution, "actions": actions,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "mode", "month", "target_count", "target_digest", "create_count", "update_count", "skip_count", "issue_count")}, indent=2))
    return 0 if not issues else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Idempotently create/update one month of AOPS accruals in live Baselane.")
    parser.add_argument("--gl-csv", type=Path, default=DEFAULT_GL)
    parser.add_argument("--month", required=True)
    parser.add_argument("--report", type=Path, default=ROOT / "reports" / "baselane_monthly_accruals_live_apply.json")
    parser.add_argument("--property", dest="property_filters", action="append", default=None)
    parser.add_argument("--kind", dest="kind_filters", action="append", default=None)
    parser.add_argument("--require-target-digest")
    parser.add_argument("--apply-lock", type=Path, default=DEFAULT_APPLY_LOCK)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except PipelineLockBusy:
        print(json.dumps({
            "status": "deferred",
            "reason": "baselane_source_pipeline_lock_held",
            "lock": str(DEFAULT_PIPELINE_LOCK),
        }, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
