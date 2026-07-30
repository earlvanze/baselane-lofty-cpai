#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error, request


ENDPOINT = "https://api.hemlane.com/graphql"
TRANSACTIONS_QUERY = """
query TransactionsNextCursorQuery($pagination: PagedPaginationInput!, $status: String!, $propertyId: ID, $propertyUnitId: ID, $portfolioId: ID, $ownerUserId: ID, $dueDateBegin: DateTime2, $dueDateEnd: DateTime2, $paymentReferenceNumber: String, $recurringPaymentRequestId: ID, $paymentCategoryId: ID, $paymentSubcategoryId: ID, $destinationUserId: ID, $sourceUserId: ID, $sourceTenantGroupId: ID, $sortOrder: String) {
  transactionsNextCursor(pagination: $pagination, status: $status, propertyId: $propertyId, propertyUnitId: $propertyUnitId, portfolioId: $portfolioId, ownerUserId: $ownerUserId, dueDateBegin: $dueDateBegin, dueDateEnd: $dueDateEnd, paymentReferenceNumber: $paymentReferenceNumber, recurringPaymentRequestId: $recurringPaymentRequestId, paymentCategoryId: $paymentCategoryId, paymentSubcategoryId: $paymentSubcategoryId, destinationUserId: $destinationUserId, sourceUserId: $sourceUserId, sourceTenantGroupId: $sourceTenantGroupId, sortOrder: $sortOrder) {
    pageInfo { page hasNextPage hasPreviousPage __typename }
    data {
      id amount status successAmount pendingAmount initializedAmount uncollectedAmount dueDate
      property { id nickname addressStreet __typename }
      propertyUnit { id unitNumber nicknameWithUnit __typename }
      paymentCategory { id label __typename }
      paymentSubcategory { id label shortLabel __typename }
      sourceUser { id fullName __typename }
      sourceTenantGroup { id status __typename }
      destinationUser { id fullName __typename }
      __typename
    }
    __typename
  }
}
"""
def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_auth_file(root: Path) -> Path:
    return root / ".secrets" / "hemlane_auth.json"


def load_headers(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    headers = data.get("headers") if isinstance(data, dict) else {}
    if not isinstance(headers, dict):
        headers = data if isinstance(data, dict) else {}
    out = {
        str(key): str(value)
        for key, value in headers.items()
        if value is not None
    }
    out.setdefault("content-type", "application/json")
    out.setdefault("accept", "application/json")
    return out


def redacted_header_keys(headers: dict[str, str]) -> list[str]:
    return sorted(headers)


def capture_auth(root: Path, out_file: Path) -> dict[str, Any]:
    script = root / "skills" / "hemlane" / "scripts" / "capture_hemlane_auth_via_cdp.py"
    if not script.is_file():
        return {"status": "missing_capture_script", "script": str(script)}
    out_file.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["python3", str(script), "--endpoint-kind", "get-transactions", "--out-file", str(out_file)],
        cwd=str(root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=90,
    )
    return {
        "status": "ok" if proc.returncode == 0 and out_file.is_file() else "failed",
        "return_code": proc.returncode,
        "stderr_tail": proc.stderr.strip().splitlines()[-3:],
    }


def gql(headers: dict[str, str], variables: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "operationName": "TransactionsNextCursorQuery",
        "query": TRANSACTIONS_QUERY,
        "variables": variables,
    }
    req = request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=60) as response:
            body = response.read().decode("utf-8", "replace")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return {"status": "http_error", "http_status": exc.code, "body_prefix": body[:500]}
    data = json.loads(body)
    if data.get("errors"):
        return {"status": "graphql_error", "errors": data.get("errors")}
    return {"status": "ok", "data": data.get("data") or {}}


def normalize_transaction(tx: dict[str, Any]) -> dict[str, Any]:
    prop = tx.get("property") if isinstance(tx.get("property"), dict) else {}
    unit = tx.get("propertyUnit") if isinstance(tx.get("propertyUnit"), dict) else {}
    category = tx.get("paymentCategory") if isinstance(tx.get("paymentCategory"), dict) else {}
    subcategory = tx.get("paymentSubcategory") if isinstance(tx.get("paymentSubcategory"), dict) else {}
    source_user = tx.get("sourceUser") if isinstance(tx.get("sourceUser"), dict) else {}
    destination_user = tx.get("destinationUser") if isinstance(tx.get("destinationUser"), dict) else {}
    return {
        "id": tx.get("id"),
        "amount": tx.get("successAmount") or tx.get("amount"),
        "request_amount": tx.get("amount"),
        "success_amount": tx.get("successAmount"),
        "pending_amount": tx.get("pendingAmount"),
        "initialized_amount": tx.get("initializedAmount"),
        "uncollected_amount": tx.get("uncollectedAmount"),
        "status": tx.get("status"),
        "posted_at": None,
        "transaction_date": tx.get("dueDate"),
        "due_date": tx.get("dueDate"),
        "property_id": prop.get("id"),
        "property": prop.get("nickname") or prop.get("addressStreet") or "",
        "property_address": prop.get("addressStreet") or "",
        "unit_id": unit.get("id"),
        "unit": unit.get("nicknameWithUnit") or unit.get("unitNumber") or "",
        "payment_category": category.get("label") or "",
        "payment_category_id": category.get("id") or "",
        "payment_subcategory": subcategory.get("label") or "",
        "payment_subcategory_id": subcategory.get("id") or "",
        "source_user": source_user.get("fullName") or "",
        "destination_user": destination_user.get("fullName") or "",
    }


def fetch_transactions(root: Path, auth_file: Path, days_back: int, limit: int, all_pages: bool) -> dict[str, Any]:
    if not auth_file.is_file():
        capture = capture_auth(root, auth_file)
        if capture.get("status") != "ok":
            return {"status": "auth_unavailable", "capture": capture, "transactions": []}
    headers = load_headers(auth_file)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days_back)
    page = 1
    transactions: list[dict[str, Any]] = []
    page_reports: list[dict[str, Any]] = []
    while True:
        variables = {
            "pagination": {"page": page, "limit": limit},
            "dueDateBegin": start.strftime("%Y-%m-%dT00:00:00.000Z"),
            "dueDateEnd": end.strftime("%Y-%m-%dT23:59:59.999Z"),
            "status": "all-active",
            "sortOrder": "desc",
        }
        result = gql(headers, variables)
        if result.get("status") != "ok":
            return {
                "status": "query_failed",
                "query_result": result,
                "headers_present": redacted_header_keys(headers),
                "transactions": transactions,
            }
        cursor = ((result.get("data") or {}).get("transactionsNextCursor") or {})
        rows = cursor.get("data") or []
        page_info = cursor.get("pageInfo") or {}
        transactions.extend(normalize_transaction(row) for row in rows if isinstance(row, dict))
        page_reports.append({"page": page, "row_count": len(rows), "has_next_page": bool(page_info.get("hasNextPage"))})
        if not all_pages or not page_info.get("hasNextPage"):
            break
        page += 1
    return {
        "status": "ok",
        "generated_at": iso_z(),
        "source": "hemlane_live_graphql",
        "operation_name": "TransactionsNextCursorQuery",
        "query_field": "transactionsNextCursor",
        "days_back": days_back,
        "page_reports": page_reports,
        "transaction_count": len(transactions),
        "transactions": transactions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Pull live Hemlane transactions for deterministic Baselane category evidence.")
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--auth-file", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--days-back", type=int, default=75)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--all-pages", action="store_true", default=True)
    args = parser.parse_args()

    root = args.root
    auth_file = args.auth_file or default_auth_file(root)
    report_path = args.report or root / "reports" / "hemlane_live_transactions.json"
    report = fetch_transactions(root, auth_file, args.days_back, args.limit, args.all_pages)
    report.setdefault("generated_at", iso_z())
    report["auth_file"] = str(auth_file)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: report.get(k) for k in ["status", "transaction_count", "days_back"]}, indent=2, sort_keys=True))
    return 0 if report.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
