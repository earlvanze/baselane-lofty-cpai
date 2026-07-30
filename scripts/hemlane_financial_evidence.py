#!/usr/bin/env python3
"""Fetch sanitized Hemlane financial transaction evidence for reconciliation."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error, request

from baselane_hemlane_live_transactions import ENDPOINT, default_auth_file, load_headers


QUERY = """
query PagedFinancialTransactions(
  $page: Int!,
  $minTransactedAt: ISO8601DateTime,
  $maxTransactedAt: ISO8601DateTime,
  $view: FinancialTransactionsView!
) {
  financialTransactions(
    pagination: {page: $page},
    minTransactedAt: $minTransactedAt,
    maxTransactedAt: $maxTransactedAt,
    view: $view
  ) {
    pageInfo { page totalPages totalCount }
    data {
      id
      amountInCents
      transactedAt
      description
      status
      isHidden
      property { id nickname addressStreet }
      propertyUnit { id nicknameWithUnit }
      paymentCategory { label }
      paymentSubcategory { label }
    }
  }
}
"""


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def gql(headers: dict[str, str], variables: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "operationName": "PagedFinancialTransactions",
        "query": QUERY,
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
            data = json.loads(response.read().decode("utf-8", "replace"))
    except error.HTTPError as exc:
        return {"status": "http_error", "http_status": exc.code}
    except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"status": "request_error", "error": type(exc).__name__}
    if data.get("errors"):
        return {"status": "graphql_error", "errors": data["errors"]}
    return {"status": "ok", "data": data.get("data") or {}}


def sanitize(row: dict[str, Any]) -> dict[str, Any]:
    prop = row.get("property") if isinstance(row.get("property"), dict) else {}
    unit = row.get("propertyUnit") if isinstance(row.get("propertyUnit"), dict) else {}
    category = row.get("paymentCategory") if isinstance(row.get("paymentCategory"), dict) else {}
    subcategory = row.get("paymentSubcategory") if isinstance(row.get("paymentSubcategory"), dict) else {}
    cents = int(row.get("amountInCents") or 0)
    return {
        "id": str(row.get("id") or ""),
        "amount": f"{cents / 100:.2f}",
        "amount_in_cents": cents,
        "transacted_at": row.get("transactedAt"),
        "description": str(row.get("description") or ""),
        "status": row.get("status"),
        "is_hidden": bool(row.get("isHidden")),
        "property_id": str(prop.get("id") or ""),
        "property": prop.get("nickname") or prop.get("addressStreet") or "",
        "unit_id": str(unit.get("id") or ""),
        "unit": unit.get("nicknameWithUnit") or "",
        "payment_category": category.get("label") or "",
        "payment_subcategory": subcategory.get("label") or "",
    }


def fetch(auth_file: Path, days_back: int) -> dict[str, Any]:
    if not auth_file.is_file():
        return {"status": "auth_unavailable", "records": []}
    headers = load_headers(auth_file)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days_back)
    page = 1
    records: list[dict[str, Any]] = []
    pages: list[dict[str, int]] = []
    while True:
        result = gql(
            headers,
            {
                "page": page,
                "minTransactedAt": start.strftime("%Y-%m-%dT00:00:00.000Z"),
                "maxTransactedAt": end.strftime("%Y-%m-%dT23:59:59.999Z"),
                "view": "all",
            },
        )
        if result.get("status") != "ok":
            return {"status": "query_failed", "query_result": result, "records": records}
        data = ((result.get("data") or {}).get("financialTransactions") or {})
        rows = data.get("data") or []
        page_info = data.get("pageInfo") or {}
        records.extend(sanitize(row) for row in rows if isinstance(row, dict))
        total_pages = int(page_info.get("totalPages") or page)
        pages.append({"page": page, "record_count": len(rows)})
        if page >= total_pages:
            break
        page += 1

    records.sort(key=lambda row: (str(row["transacted_at"]), row["id"]), reverse=True)
    digest_payload = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    return {
        "status": "ok",
        "generated_at": iso_z(),
        "source": "hemlane_live_graphql_financial_transactions",
        "days_back": days_back,
        "record_count": len(records),
        "page_reports": pages,
        "evidence_digest": hashlib.sha256(digest_payload).hexdigest(),
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--auth-file", type=Path)
    parser.add_argument("--days-back", type=int, default=120)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    auth_file = args.auth_file or default_auth_file(args.root)
    report_path = args.report or args.root / "reports" / "hemlane_financial_evidence.json"
    report = fetch(auth_file, args.days_back)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ("status", "record_count", "evidence_digest")}, indent=2))
    return 0 if report.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
