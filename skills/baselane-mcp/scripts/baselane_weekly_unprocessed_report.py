#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


def norm(value: str) -> str:
    return (value or "").strip().lower()


def contains_any(haystack: str, needles) -> bool:
    return any(n in haystack for n in needles)


def row_key(row: dict) -> str:
    parts = [
        row.get("Date", "").strip(),
        row.get("Amount", "").strip(),
        row.get("Merchant", "").strip(),
        row.get("Description", "").strip(),
        row.get("Account", "").strip(),
        row.get("Property", "").strip(),
    ]
    return "|".join(parts)


def is_unprocessed_hint(row: dict) -> bool:
    cat = norm(row.get("Category", ""))
    ttype = norm(row.get("Type", ""))
    notes = norm(row.get("Notes", ""))

    if "[processed-weekly-pass]" in notes:
        return False

    if not cat:
        return True
    if cat in {"uncategorized", "unknown", "other", "other expenses", "general"}:
        return True
    if not ttype:
        return True
    return False


def scopes_for_row(row: dict):
    merchant = norm(row.get("Merchant", ""))
    description = norm(row.get("Description", ""))
    notes = norm(row.get("Notes", ""))
    account = norm(row.get("Account", ""))
    prop = norm(row.get("Property", ""))
    text = " ".join([merchant, description, notes])

    scopes = []

    if contains_any(text, ["holly hill", "mortgage", "pmi", "escrow"]):
        scopes.append("mortgage_split")

    if "morgan linen" in text:
        scopes.append("morgan_linen_split")

    if contains_any(text, ["amazon", "walmart"]) and contains_any(
        " ".join([account, prop]), ["madison"]
    ):
        scopes.append("madison_consumables_split")

    if "stripe" in text and (is_unprocessed_hint(row) or not prop):
        scopes.append("stripe_unmatched_payout")

    if contains_any(text, ["county waste", "netflix", "hulu", "spectrum"]) and contains_any(
        " ".join([account, prop]), ["madison"]
    ):
        scopes.append("shared_service_4way")

    return scopes


def stable_hash_for_candidates(candidates):
    material = "\n".join(
        sorted(f"{c['scope']}|{c['key']}" for c in candidates)
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ledger",
        default="/home/umbrel/.openclaw/workspace/Dropbox/Projects/transaction_tracker/ECO Systems General Ledger.csv",
    )
    parser.add_argument(
        "--out-json",
        default="/home/umbrel/.openclaw/workspace/reports/baselane_weekly_unprocessed_report.json",
    )
    parser.add_argument(
        "--out-csv",
        default="/home/umbrel/.openclaw/workspace/reports/baselane_weekly_unprocessed_candidates.csv",
    )
    parser.add_argument(
        "--state-file",
        default="/home/umbrel/.openclaw/workspace/scripts/.baselane_weekly_unprocessed_state.json",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=50,
        help="max rows per scope in JSON sample",
    )
    args = parser.parse_args()

    ledger_path = Path(args.ledger)
    out_json = Path(args.out_json)
    out_csv = Path(args.out_csv)
    state_file = Path(args.state_file)

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    state_file.parent.mkdir(parents=True, exist_ok=True)

    if not ledger_path.exists():
        raise SystemExit(f"Ledger not found: {ledger_path}")

    with ledger_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    key_counter = Counter(row_key(r) for r in rows)
    duplicates = {k: c for k, c in key_counter.items() if c > 1}

    candidates = []
    per_scope = defaultdict(list)

    for r in rows:
        key = row_key(r)
        for scope in scopes_for_row(r):
            rec = {
                "scope": scope,
                "key": key,
                "Date": r.get("Date", ""),
                "Amount": r.get("Amount", ""),
                "Merchant": r.get("Merchant", ""),
                "Description": r.get("Description", ""),
                "Account": r.get("Account", ""),
                "Property": r.get("Property", ""),
                "Type": r.get("Type", ""),
                "Category": r.get("Category", ""),
                "Notes": r.get("Notes", ""),
                "unprocessed_hint": is_unprocessed_hint(r),
            }
            candidates.append(rec)
            if len(per_scope[scope]) < args.sample_limit:
                per_scope[scope].append(rec)

    signature = stable_hash_for_candidates(candidates)
    iso_week = datetime.now().strftime("%G-%V")

    previous = {}
    if state_file.exists():
        try:
            previous = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            previous = {}

    same_week = previous.get("last_week") == iso_week
    same_signature = previous.get("last_signature") == signature

    report = {
        "generated_at": datetime.now().isoformat(),
        "iso_week": iso_week,
        "ledger": str(ledger_path),
        "ledger_rows": len(rows),
        "duplicate_key_count": len(duplicates),
        "duplicate_rows_total": int(sum(duplicates.values()) - len(duplicates)),
        "candidate_count": len(candidates),
        "candidate_signature_sha256": signature,
        "idempotency": {
            "same_week_as_last_run": same_week,
            "same_signature_as_last_run": same_signature,
            "idempotent": bool(same_week and same_signature),
        },
        "scope_counts": {k: len(v) for k, v in defaultdict(list, {s: [None] * len([c for c in candidates if c['scope'] == s]) for s in sorted(set(c['scope'] for c in candidates))}).items()},
        "scope_samples": {k: v for k, v in per_scope.items()},
    }

    # simpler reliable scope counts
    scope_counts = Counter(c["scope"] for c in candidates)
    report["scope_counts"] = dict(sorted(scope_counts.items()))

    with out_json.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    csv_fields = [
        "scope",
        "key",
        "Date",
        "Amount",
        "Merchant",
        "Description",
        "Account",
        "Property",
        "Type",
        "Category",
        "Notes",
        "unprocessed_hint",
    ]
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=csv_fields)
        w.writeheader()
        for c in candidates:
            w.writerow(c)

    new_state = {
        "last_run_at": datetime.now().isoformat(),
        "last_week": iso_week,
        "last_signature": signature,
        "last_candidate_count": len(candidates),
        "last_duplicate_key_count": len(duplicates),
    }
    state_file.write_text(json.dumps(new_state, indent=2), encoding="utf-8")

    print(f"weekly_report={out_json}")
    print(f"candidates_csv={out_csv}")
    print(f"ledger_rows={len(rows)}")
    print(f"candidate_count={len(candidates)}")
    print(f"duplicate_key_count={len(duplicates)}")
    print(f"idempotent_same_week_and_signature={bool(same_week and same_signature)}")


if __name__ == "__main__":
    main()
