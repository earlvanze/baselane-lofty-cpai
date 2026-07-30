#!/usr/bin/env python3
from __future__ import annotations

import argparse
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
BASE_DIR = Path(os.environ.get('DISCORD_PUBLIC_WORKSPACE_ROOT', str(Path.home() / '.openclaw' / 'workspace-discord-public')))
OUT_DIR = BASE_DIR / 'loftyassist_property_context'
META_PATH = BASE_DIR / 'loftyassist_properties_full_context.meta.json'
INDEX_MD = BASE_DIR / 'loftyassist_properties_full_context.md'
SCRIPT_PATH = Path(__file__).resolve()
ISSUE_CLASS = "loftyassist-properties-full-context"


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
                time.sleep(0.5 * attempt)
    raise RuntimeError(f"fetch failed for {url}: {last_err}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build full LoftyAssist per-property context artifacts.")
    parser.add_argument("--base-dir", default=str(BASE_DIR), help="Base directory for generated full-context artifacts")
    parser.add_argument("--json", action="store_true", help="Emit a read-only dashboard diagnostic and do not fetch, write, or prune")
    return parser.parse_args(argv)


def paths_for_base(base_dir: Path) -> dict[str, Path]:
    return {
        "base_dir": base_dir,
        "out_dir": base_dir / "loftyassist_property_context",
        "meta_path": base_dir / "loftyassist_properties_full_context.meta.json",
        "index_path": base_dir / "loftyassist_properties_full_context.md",
    }


def review_command(args: argparse.Namespace) -> str:
    parts = ["python3", str(SCRIPT_PATH), "--base-dir", str(args.base_dir), "--json"]
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
    if "--base-dir" not in parts:
        validation["issues"].append("missing-base-dir-flag")
    else:
        idx = parts.index("--base-dir")
        if idx + 1 >= len(parts) or parts[idx + 1] != str(args.base_dir):
            validation["issues"].append("unexpected-base-dir")
    if "--json" not in parts:
        validation["issues"].append("missing-json-flag")
    for flag in ("--apply", "--write", "--force", "--prune"):
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
    base_dir = Path(args.base_dir)
    paths = paths_for_base(base_dir)
    output_paths = {
        "out_dir": str(paths["out_dir"]),
        "meta_json": str(paths["meta_path"]),
        "index_markdown": str(paths["index_path"]),
        "property_json_template": str(paths["out_dir"] / "{slug}.json"),
    }
    issues: list[str] = []
    requests_available = importlib.util.find_spec("requests") is not None
    if not requests_available:
        issues.append("Python dependency missing: requests")
    if base_dir.exists() and not base_dir.is_dir():
        issues.append(f"Base path exists but is not a directory: {base_dir}")
    elif not base_dir.exists() and not base_dir.parent.exists():
        issues.append(f"Base parent missing: {base_dir.parent}")
    elif not base_dir.exists() and not base_dir.parent.is_dir():
        issues.append(f"Base parent is not a directory: {base_dir.parent}")

    command = review_command(args)
    validation = review_command_validation(command, args)
    classified_issues: list[dict[str, Any]] = []
    for text in issues:
        classified_issues.append(
            {
                "issue_class": ISSUE_CLASS,
                "route": ISSUE_CLASS,
                "classification": "loftyassist-properties-full-context-review",
                "severity": "medium",
                "title": "LoftyAssist full-context preflight issue",
                "issue": text,
                "area": "loftyassist-public-context",
                "base_dir": str(base_dir),
                "base_dir_exists": base_dir.exists(),
                "base_parent_exists": base_dir.parent.exists(),
                "requests_available": requests_available,
                "fetch_attempted": False,
                "network_attempted": False,
                "write_attempted": False,
                "directory_create_attempted": False,
                "property_json_write_attempted": False,
                "meta_write_attempted": False,
                "index_write_attempted": False,
                "stale_prune_attempted": False,
                "sleep_attempted": False,
                "row_build_attempted": False,
                "remediation_class": "operator-reviewed-loftyassist-properties-full-context",
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
    ok = ["LoftyAssist full-context diagnostic OK: local preflight passed"] if ok_state else []
    summary = classified_summary(classified_issues)
    report = {
        "status": "NO_REPLY" if ok_state else "LOFTYASSIST_PROPERTIES_FULL_CONTEXT_REVIEW",
        "classification": "ok" if ok_state else "loftyassist-properties-full-context-review",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_list_url": LIST_URL,
        "source_detail_url_template": DETAIL_URL,
        "base_dir": str(base_dir),
        "base_dir_exists": base_dir.exists(),
        "base_parent_exists": base_dir.parent.exists(),
        "base_parent_is_dir": base_dir.parent.is_dir(),
        "output_paths": output_paths,
        "requests_available": requests_available,
        "fetch_attempted": False,
        "network_attempted": False,
        "write_attempted": False,
        "directory_create_attempted": False,
        "property_json_write_attempted": False,
        "meta_write_attempted": False,
        "index_write_attempted": False,
        "stale_prune_attempted": False,
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
        "remediation_class": "no-remediation-needed" if ok_state else "operator-reviewed-loftyassist-properties-full-context",
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
            "base_dir_exists": report["base_dir_exists"],
            "base_parent_exists": report["base_parent_exists"],
            "fetch_attempted": False,
            "network_attempted": False,
            "write_attempted": False,
            "directory_create_attempted": False,
            "property_json_write_attempted": False,
            "meta_write_attempted": False,
            "index_write_attempted": False,
            "stale_prune_attempted": False,
            "sleep_attempted": False,
            "row_build_attempted": False,
        }
    )
    return report


def run(base_dir: Path, stdout: TextIO = sys.stdout) -> int:
    paths = paths_for_base(base_dir)
    out_dir = paths["out_dir"]
    meta_path = paths["meta_path"]
    index_md = paths["index_path"]

    out_dir.mkdir(parents=True, exist_ok=True)

    listing = fetch_json(LIST_URL, timeout=40, retries=3)
    if not isinstance(listing, list):
        raise RuntimeError("Unexpected payload from /api/properties")

    ts = datetime.now(timezone.utc).isoformat()

    seen_files: set[str] = set()
    rows = []
    errors = []

    for i, item in enumerate(listing, 1):
        prop = item.get("property") or {}
        slug = prop.get("slug")
        if not slug:
            errors.append({"index": i, "error": "missing slug"})
            continue

        detail_url = DETAIL_URL.format(slug=slug)
        payload = {
            "generated_at": ts,
            "source": {
                "listing_url": LIST_URL,
                "detail_url": detail_url,
            },
            "listing": item,
            "detail": None,
        }

        ok = True
        try:
            payload["detail"] = fetch_json(detail_url, timeout=30, retries=2)
        except Exception as e:
            ok = False
            errors.append({"slug": slug, "error": str(e)})

        out_name = f"{slug}.json"
        out_path = out_dir / out_name
        out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        seen_files.add(out_name)

        rows.append(
            {
                "slug": slug,
                "assetUnit": prop.get("assetUnit") or "",
                "address": prop.get("address") or "",
                "state": prop.get("state") or "",
                "file": str(out_path),
                "detail_ok": ok,
            }
        )

        if i % 20 == 0:
            time.sleep(0.15)

    # prune stale property files not in current listing
    removed = []
    for p in out_dir.glob("*.json"):
        if p.name not in seen_files:
            p.unlink(missing_ok=True)
            removed.append(p.name)

    meta = {
        "generated_at": ts,
        "source": LIST_URL,
        "property_count": len(rows),
        "detail_success_count": sum(1 for r in rows if r["detail_ok"]),
        "detail_error_count": len(errors),
        "errors": errors,
        "removed_stale_files": removed,
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    rows_sorted = sorted(rows, key=lambda r: (r["assetUnit"] or "ZZZ", r["address"] or r["slug"]))
    lines = [
        "# LoftyAssist Full Property Context",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"Source: {LIST_URL}",
        f"Property count: {len(rows)}",
        f"Detail success: {meta['detail_success_count']}",
        f"Detail errors: {meta['detail_error_count']}",
        "",
        "Each property JSON includes both listing snapshot and detail payload:",
        "- `loftyassist_property_context/{slug}.json`",
        "",
        "| Asset Unit | Address | Slug | Detail | File |",
        "|---|---|---|---:|---|",
    ]
    for r in rows_sorted:
        lines.append(
            f"| {r['assetUnit'] or '—'} | {r['address'] or '—'} | {r['slug']} | {'OK' if r['detail_ok'] else 'ERR'} | `{Path(r['file']).name}` |"
        )

    index_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    stdout.write(f"done properties={len(rows)} success={meta['detail_success_count']} errors={len(errors)} removed={len(removed)}\n")
    stdout.write(f"out_dir={out_dir}\n")
    stdout.write(f"meta={meta_path}\n")
    stdout.write(f"index={index_md}\n")
    return 0


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    if args.json:
        report = build_report(args)
        stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
        return 0 if report["ok_state"] else 1
    return run(Path(args.base_dir), stdout=stdout)


if __name__ == "__main__":
    raise SystemExit(main())
