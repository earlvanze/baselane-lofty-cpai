#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import shlex
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

LIST_URL = "https://www.loftyassist.com/api/properties"
DETAIL_URL = "https://www.loftyassist.com/api/properties/{slug}"
OUT_DIR = Path(os.environ.get("DISCORD_PUBLIC_WORKSPACE_ROOT", str(Path.home() / ".openclaw" / "workspace-discord-public")))
SCRIPT_PATH = Path(__file__).resolve()
ISSUE_CLASS = "loftyassist-all-properties-table"


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def iso_date(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        if v > 10_000_000_000:
            v = v / 1000.0
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return str(v)
    s = str(v).strip()
    if not s:
        return ""
    return s[:10] if len(s) >= 10 and s[4] == "-" and s[7] == "-" else s


def deep_find(obj: Any, keywords: list[str]) -> Any:
    kws = [k.lower() for k in keywords]

    def walk(x: Any) -> Any:
        if isinstance(x, dict):
            for k, v in x.items():
                lk = k.lower()
                if any(w in lk for w in kws):
                    if isinstance(v, (str, int, float, bool)) and str(v).strip() != "":
                        return v
                found = walk(v)
                if found is not None:
                    return found
        elif isinstance(x, list):
            for i in x:
                found = walk(i)
                if found is not None:
                    return found
        return None

    return walk(obj)


def fetch_json(url: str, timeout: int = 30, retries: int = 3) -> Any:
    import requests

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers={"accept": "application/json, text/plain, */*"}, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.6 * attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the LoftyAssist all-properties context table.")
    parser.add_argument("--out-dir", default=str(OUT_DIR), help="Directory for generated JSON, CSV, and Markdown artifacts")
    parser.add_argument("--json", action="store_true", help="Emit a read-only dashboard diagnostic and do not fetch or write")
    return parser.parse_args(argv)


def review_command(args: argparse.Namespace) -> str:
    parts = ["python3", str(SCRIPT_PATH), "--out-dir", str(args.out_dir), "--json"]
    return " ".join(shlex.quote(part) for part in parts)


def review_command_validation(command: object | None, args: argparse.Namespace) -> dict[str, Any]:
    command_text = "" if command is None else str(command)
    validation: dict[str, Any] = {
        "valid": False,
        "issues": [],
        "command": command_text,
        "parts": [],
        "script_path": str(SCRIPT_PATH),
        "script_exists": SCRIPT_PATH.exists(),
        "script_is_file": SCRIPT_PATH.is_file(),
        "path": str(SCRIPT_PATH),
        "path_exists": SCRIPT_PATH.exists(),
        "json_flag_present": False,
    }
    try:
        parts = shlex.split(command_text)
    except ValueError as exc:
        validation["issues"].append(f"parse-error:{exc}")
        return validation

    validation["parts"] = parts
    validation["json_flag_present"] = "--json" in parts
    if len(parts) != 5:
        validation["issues"].append("unexpected-argument-count")
    if not parts or parts[0] != "python3":
        validation["issues"].append("missing-python3")
    if len(parts) < 2 or Path(parts[1]).resolve() != SCRIPT_PATH:
        validation["issues"].append("unexpected-script-path")
    if "--out-dir" not in parts:
        validation["issues"].append("missing-out-dir-flag")
    else:
        idx = parts.index("--out-dir")
        if idx + 1 >= len(parts) or parts[idx + 1] != str(args.out_dir):
            validation["issues"].append("unexpected-out-dir")
    if "--json" not in parts:
        validation["issues"].append("missing-json-flag")
    for flag in ("--apply", "--write", "--force"):
        if flag in parts:
            validation["issues"].append(f"unexpected-{flag.lstrip('-')}-flag")
    if not SCRIPT_PATH.exists():
        validation["issues"].append("script-missing")
    elif not SCRIPT_PATH.is_file():
        validation["issues"].append("script-not-file")

    validation["valid"] = not validation["issues"]
    return validation


def classified_summary(classified_issues: list[dict[str, Any]]) -> dict[str, Any]:
    class_counts: dict[str, int] = {}
    route_counts: dict[str, int] = {}
    validation_issues: list[str] = []
    for issue in classified_issues:
        issue_class = issue.get("issue_class")
        route = issue.get("route")
        if issue_class:
            class_counts[issue_class] = class_counts.get(issue_class, 0) + 1
        if route:
            route_counts[route] = route_counts.get(route, 0) + 1
        for item in (issue.get("review_command_validation") or {}).get("issues") or []:
            validation_issues.append(str(item))
    safe_count = sum(1 for issue in classified_issues if issue.get("review_command_safe_to_run_automatically"))
    valid_count = sum(1 for issue in classified_issues if issue.get("review_command_valid"))
    return {
        "total": len(classified_issues),
        "total_count": len(classified_issues),
        "classified_record_count": len(classified_issues),
        "class_counts": class_counts,
        "issue_class_counts": class_counts,
        "route_classification_counts": route_counts,
        "review_required_count": len(classified_issues),
        "approval_required_count": sum(1 for issue in classified_issues if issue.get("requires_operator_approval")),
        "requires_operator_approval_count": sum(1 for issue in classified_issues if issue.get("requires_operator_approval")),
        "interactive_sudo_count": sum(1 for issue in classified_issues if issue.get("requires_interactive_sudo")),
        "interactive_oauth_count": sum(1 for issue in classified_issues if issue.get("requires_interactive_oauth")),
        "requires_interactive_sudo_count": sum(1 for issue in classified_issues if issue.get("requires_interactive_sudo")),
        "requires_interactive_oauth_count": sum(1 for issue in classified_issues if issue.get("requires_interactive_oauth")),
        "safe_review_command_count": safe_count,
        "valid_review_command_count": valid_count,
        "invalid_review_command_count": safe_count - valid_count,
        "review_command_validation_issues": validation_issues,
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    out_dir = Path(args.out_dir)
    output_paths = {
        "raw_listing_json": str(out_dir / "loftyassist_all_properties.json"),
        "table_json": str(out_dir / "loftyassist_all_properties_table.json"),
        "table_csv": str(out_dir / "loftyassist_all_properties_table.csv"),
        "table_markdown": str(out_dir / "loftyassist_all_properties_table.md"),
    }
    issues: list[str] = []
    requests_available = importlib.util.find_spec("requests") is not None
    if not requests_available:
        issues.append("Python dependency missing: requests")
    if out_dir.exists() and not out_dir.is_dir():
        issues.append(f"Output path exists but is not a directory: {out_dir}")
    elif not out_dir.exists() and not out_dir.parent.exists():
        issues.append(f"Output parent missing: {out_dir.parent}")
    elif not out_dir.exists() and not out_dir.parent.is_dir():
        issues.append(f"Output parent is not a directory: {out_dir.parent}")

    command = review_command(args)
    validation = review_command_validation(command, args)
    classified_issues: list[dict[str, Any]] = []
    for text in issues:
        classified_issues.append(
            {
                "issue_class": ISSUE_CLASS,
                "route": ISSUE_CLASS,
                "classification": "loftyassist-all-properties-table-review",
                "severity": "medium",
                "title": "LoftyAssist all-properties table preflight issue",
                "issue": text,
                "area": "loftyassist-public-context",
                "out_dir": str(out_dir),
                "out_dir_exists": out_dir.exists(),
                "out_parent_exists": out_dir.parent.exists(),
                "requests_available": requests_available,
                "fetch_attempted": False,
                "network_attempted": False,
                "write_attempted": False,
                "directory_create_attempted": False,
                "remediation_class": "operator-reviewed-loftyassist-all-properties-table",
                "requires_operator_approval": True,
                "requires_interactive_sudo": False,
                "requires_interactive_oauth": False,
                "safe_to_run_automatically": False,
                "review_command": command,
                "review_command_safe_to_run_automatically": True,
                "review_command_valid": validation["valid"],
                "review_command_validation": validation,
                "cleanup_command_after_review": None,
                "restart_command_after_review": None,
                "oauth_command_after_review": None,
                "helper_command_after_review": None,
                "remediation": {
                    "command": None,
                    "review_command": command,
                    "review_command_validation": validation,
                },
            }
        )

    ok_state = not classified_issues
    ok = ["LoftyAssist all-properties table diagnostic OK: local preflight passed"] if ok_state else []
    summary = classified_summary(classified_issues)
    report = {
        "status": "NO_REPLY" if ok_state else "LOFTYASSIST_ALL_PROPERTIES_TABLE_REVIEW",
        "classification": "ok" if ok_state else "loftyassist-all-properties-table-review",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_list_url": LIST_URL,
        "source_detail_url_template": DETAIL_URL,
        "out_dir": str(out_dir),
        "out_dir_exists": out_dir.exists(),
        "out_parent_exists": out_dir.parent.exists(),
        "out_parent_is_dir": out_dir.parent.is_dir(),
        "output_paths": output_paths,
        "requests_available": requests_available,
        "fetch_attempted": False,
        "network_attempted": False,
        "write_attempted": False,
        "directory_create_attempted": False,
        "sleep_attempted": False,
        "row_build_attempted": False,
        "ok": ok,
        "issues": issues,
        "ok_state": ok_state,
        "visible_ok": ok,
        "ok_count": len(ok),
        "issue_count": len(classified_issues),
        "advisory_count": 0,
        "review_required_count": len(classified_issues),
        "approval_required_count": len(classified_issues),
        "requires_operator_approval_count": len(classified_issues),
        "interactive_sudo_count": 0,
        "interactive_oauth_count": 0,
        "requires_interactive_sudo_count": 0,
        "requires_interactive_oauth_count": 0,
        "issue_classes": sorted({issue["issue_class"] for issue in classified_issues}),
        "classified_issues": classified_issues,
        "classified_issue_summary": summary,
        "remediation_class": "no-remediation-needed" if ok_state else "operator-reviewed-loftyassist-all-properties-table",
        "requires_operator_approval": not ok_state,
        "requires_interactive_sudo": False,
        "requires_interactive_oauth": False,
        "safe_to_run_automatically": ok_state,
        "review_command": None if ok_state else command,
        "review_command_safe_to_run_automatically": not ok_state,
        "review_command_valid": None if ok_state else validation["valid"],
        "review_command_validation": None if ok_state else validation,
        "safe_review_command_count": summary["safe_review_command_count"],
        "valid_review_command_count": summary["valid_review_command_count"],
        "invalid_review_command_count": summary["invalid_review_command_count"],
        "review_command_validation_issues": summary["review_command_validation_issues"],
        "cleanup_command_after_review": None,
        "restart_command_after_review": None,
        "oauth_command_after_review": None,
        "helper_command_after_review": None,
        "remediation": {
            "command": None,
            "review_command": None if ok_state else command,
            "review_command_validation": None if ok_state else validation,
        },
    }
    report["classified_issue_summary"].update(
        {
            "classification": report["classification"],
            "route_classification": report["classification"],
            "ok_count": report["ok_count"],
            "issue_count": report["issue_count"],
            "visible_ok_count": len(report["visible_ok"]),
            "requests_available": requests_available,
            "out_dir_exists": report["out_dir_exists"],
            "out_parent_exists": report["out_parent_exists"],
            "fetch_attempted": False,
            "network_attempted": False,
            "write_attempted": False,
            "directory_create_attempted": False,
        }
    )
    return report


def run(out_dir: Path, stdout: TextIO = sys.stdout) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)

    listing = fetch_json(LIST_URL, timeout=40)
    if not isinstance(listing, list):
        raise RuntimeError("Unexpected /api/properties payload type")

    generated = datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for i, item in enumerate(listing, 1):
        prop = item.get("property") or {}
        slug = prop.get("slug")
        if not slug:
            continue

        detail = {}
        try:
            detail = fetch_json(DETAIL_URL.format(slug=slug), timeout=30, retries=2)
        except Exception as e:
            errors.append({"slug": slug, "error": str(e)})

        dprop = detail.get("property") or {}

        occupancy = (
            dprop.get("custom_occupancy")
            or prop.get("custom_occupancy")
            or ("Occupied" if dprop.get("is_occupied") is True else "Vacant/Unknown" if dprop.get("is_occupied") is False else "Unknown")
        )

        dscr = deep_find(detail, ["dscr"]) if detail else None
        maturity = deep_find(detail, ["nextdebtmaturity", "debtmaturity", "maturity"]) if detail else None
        last_distribution = deep_find(detail, ["lastdistribution", "distributiondate", "lastpayout", "lastdividend", "distribution"]) if detail else None

        row = {
            "asset_unit": prop.get("assetUnit") or dprop.get("assetUnit") or "",
            "asset_id": prop.get("assetId") or dprop.get("assetId") or "",
            "slug": slug,
            "address": prop.get("address") or dprop.get("address") or "",
            "state": prop.get("state") or dprop.get("state") or "",
            "property_type": prop.get("property_type") or dprop.get("property_type") or "",
            "listing_status": prop.get("listingStatus") or dprop.get("listingStatus") or "",
            "status": item.get("status") or detail.get("status") or "",
            "occupancy": occupancy or "",
            "dscr": "" if dscr is None else str(dscr),
            "next_debt_maturity": iso_date(maturity),
            "last_distribution": iso_date(last_distribution),
            "coc": item.get("coc") if item.get("coc") is not None else prop.get("coc"),
            "cap_rate": item.get("capRate") if item.get("capRate") is not None else prop.get("cap_rate"),
            "market_price": item.get("marketPrice"),
            "oracle_price": item.get("oraclePrice"),
            "cash_flow_positive_days": item.get("cashFlowPositiveDays"),
            "cash_flow_pct_positive": item.get("cashFlowPercentagePositive"),
            "source_listing": LIST_URL,
            "source_detail": DETAIL_URL.format(slug=slug),
        }
        rows.append(row)

        if i % 25 == 0:
            time.sleep(0.15)

    rows.sort(key=lambda r: (str(r.get("asset_unit") or "ZZZ"), str(r.get("address") or "")))

    json_payload = {
        "generated_at": generated,
        "source": LIST_URL,
        "count": len(rows),
        "errors": errors,
        "rows": rows,
    }

    (out_dir / "loftyassist_all_properties.json").write_text(json.dumps(listing, indent=2) + "\n", encoding="utf-8")
    (out_dir / "loftyassist_all_properties_table.json").write_text(json.dumps(json_payload, indent=2) + "\n", encoding="utf-8")

    fieldnames = [
        "asset_unit",
        "asset_id",
        "slug",
        "address",
        "state",
        "property_type",
        "listing_status",
        "status",
        "occupancy",
        "dscr",
        "next_debt_maturity",
        "last_distribution",
        "coc",
        "cap_rate",
        "market_price",
        "oracle_price",
        "cash_flow_positive_days",
        "cash_flow_pct_positive",
        "source_detail",
    ]

    with (out_dir / "loftyassist_all_properties_table.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})

    lines = [
        "# LoftyAssist All Properties Table",
        "",
        f"Generated: {now_utc()}",
        f"Source: {LIST_URL}",
        f"Property count: {len(rows)}",
        f"Detail fetch errors: {len(errors)}",
        "",
        "This table is the default baseline for Discord investor Q&A across all Lofty properties.",
        "",
        "| Asset Unit | Address | Occupancy | DSCR | Next debt maturity | Last distribution | CoC | Cap rate |",
        "|---|---|---:|---:|---|---|---:|---:|",
    ]

    for r in rows:
        lines.append(
            f"| {r.get('asset_unit') or '—'} | {r.get('address') or r.get('slug') or '—'} | {r.get('occupancy') or '—'} | {r.get('dscr') or '—'} | {r.get('next_debt_maturity') or '—'} | {r.get('last_distribution') or '—'} | {r.get('coc') if r.get('coc') is not None else '—'} | {r.get('cap_rate') if r.get('cap_rate') is not None else '—'} |"
        )

    if errors:
        lines += ["", "## Detail fetch errors", ""]
        for e in errors[:50]:
            lines.append(f"- {e['slug']}: {e['error']}")
        if len(errors) > 50:
            lines.append(f"- ... and {len(errors) - 50} more")

    (out_dir / "loftyassist_all_properties_table.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    stdout.write(f"Wrote {len(rows)} rows to {out_dir}\n")
    return 0


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    if args.json:
        report = build_report(args)
        json.dump(report, stdout, indent=2)
        stdout.write("\n")
        return 0 if report["ok_state"] else 1
    return run(Path(args.out_dir), stdout=stdout)


if __name__ == "__main__":
    raise SystemExit(main())
