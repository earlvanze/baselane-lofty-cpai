#!/usr/bin/env python3
import argparse
import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any, List
from urllib.parse import urlparse

try:
    import requests
except Exception:  # pragma: no cover - exercised through patched tests.
    requests = None

try:
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials
except Exception:  # pragma: no cover - exercised through patched tests.
    SigV4Auth = None
    AWSRequest = None
    Credentials = None

DEFAULT_ENDPOINT = "https://api.lofty.ai/prod/exchange/v2/getpropertyinfo"
DEFAULT_START_TIME = "2592000000"
DEFAULT_REGION = "us-east-1"
DEFAULT_SERVICE = "execute-api"
DEFAULT_APP_VERSION = "1.33.0-1772157022-prod"
CLASSIFICATION = "lofty-getpropertyinfo-batch-review"
ISSUE_CLASS = "lofty-getpropertyinfo-batch"


def extract_marketplace_ids(data) -> List[str]:
    ids = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                pid = item.get("propertyId") or item.get("id") or item.get("property_id")
                if pid:
                    ids.append(pid)
            elif isinstance(item, str):
                ids.append(item)
    elif isinstance(data, dict):
        for key in ("properties", "items", "data", "results"):
            if key in data and isinstance(data[key], list):
                for item in data[key]:
                    if isinstance(item, dict):
                        pid = item.get("propertyId") or item.get("id") or item.get("property_id")
                        if pid:
                            ids.append(pid)
                    elif isinstance(item, str):
                        ids.append(item)
        if not ids:
            for k, v in data.items():
                if isinstance(v, dict) and ("propertyId" in v or "id" in v):
                    pid = v.get("propertyId") or v.get("id")
                    if pid:
                        ids.append(pid)
                elif isinstance(k, str) and len(k) > 10:
                    ids.append(k)

    seen = set()
    out = []
    for pid in ids:
        if pid not in seen:
            seen.add(pid)
            out.append(pid)
    return out


def load_marketplace_ids(path: Path) -> List[str]:
    data = json.loads(path.read_text())
    out = extract_marketplace_ids(data)
    if not out:
        raise SystemExit("No property IDs found in marketplace.json")
    return out


def sign_headers(url: str, params: dict, region: str, service: str, app_version: str) -> dict:
    if SigV4Auth is None or AWSRequest is None or Credentials is None:
        raise SystemExit("Missing botocore dependency for AWS SigV4 signing")

    access_key = os.getenv("AWS_ACCESS_KEY_ID")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    session_token = os.getenv("AWS_SESSION_TOKEN")
    if not (access_key and secret_key and session_token):
        raise SystemExit("Missing AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN env vars")

    creds = Credentials(access_key, secret_key, session_token)
    headers = {
        "accept": "application/json, text/plain, */*",
        "origin": "https://www.lofty.ai",
        "referer": "https://www.lofty.ai/",
        "x-lofty-app-version": app_version,
    }

    req = AWSRequest(method="GET", url=url, params=params, headers=headers)
    SigV4Auth(creds, service, region).add_auth(req)
    # AWSRequest stores headers in a case-insensitive dict
    return dict(req.headers)


def fetch_property(pid: str, endpoint: str, start_time: str, region: str, service: str, app_version: str, out_dir: Path, timeout: int = 30):
    if requests is None:
        raise SystemExit("Missing requests dependency for Lofty getpropertyinfo fetch")
    params = {"propertyId": pid, "startTime": start_time}
    headers = sign_headers(endpoint, params, region, service, app_version)
    resp = requests.get(endpoint, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    out_path = out_dir / f"{pid}.json"
    out_path.write_text(resp.text)


def _review_command(args) -> str:
    command = [sys.executable or "python3", str(Path(__file__)), "--json"]
    if args.marketplace:
        command.extend(["--marketplace", args.marketplace])
    if args.out:
        command.extend(["--out", args.out])
    if args.endpoint != DEFAULT_ENDPOINT:
        command.extend(["--endpoint", args.endpoint])
    if args.start_time != DEFAULT_START_TIME:
        command.extend(["--start-time", args.start_time])
    if args.region != DEFAULT_REGION:
        command.extend(["--region", args.region])
    if args.service != DEFAULT_SERVICE:
        command.extend(["--service", args.service])
    if args.app_version != DEFAULT_APP_VERSION:
        command.extend(["--app-version", args.app_version])
    if args.sleep != 0.2:
        command.extend(["--sleep", str(args.sleep)])
    return shlex.join(command)


def _valid_review_command(command: str) -> bool:
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    return len(parts) >= 3 and parts[1].endswith("lofty_getpropertyinfo_batch.py") and "--json" in parts


def _issue(code: str, message: str, route: str = "review-diagnostic-output") -> dict:
    return {
        "classification": CLASSIFICATION,
        "issue_class": ISSUE_CLASS,
        "code": code,
        "message": message,
        "remediation": route,
    }


def _check_endpoint(endpoint: str) -> bool:
    parsed = urlparse(endpoint or "")
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def build_report(args, env=None) -> dict:
    env = os.environ if env is None else env
    issues = []

    endpoint_valid = _check_endpoint(args.endpoint)
    start_time_valid = bool(args.start_time) and str(args.start_time).isdigit()
    region_present = bool(args.region)
    service_present = bool(args.service)
    app_version_present = bool(args.app_version)
    sleep_nonnegative = args.sleep is not None and args.sleep >= 0
    requests_available = requests is not None
    botocore_available = SigV4Auth is not None and AWSRequest is not None and Credentials is not None

    if not requests_available:
        issues.append(_issue("requests-unavailable", "Python requests dependency is not importable."))
    if not botocore_available:
        issues.append(_issue("botocore-unavailable", "Python botocore dependency is not importable for SigV4 signing."))
    if not endpoint_valid:
        issues.append(_issue("invalid-endpoint", "Lofty getpropertyinfo endpoint must be an http(s) URL with a host."))
    if not start_time_valid:
        issues.append(_issue("invalid-start-time", "Lofty start time must be present and numeric."))
    if not region_present:
        issues.append(_issue("missing-region", "AWS region is missing."))
    if not service_present:
        issues.append(_issue("missing-service", "AWS service is missing."))
    if not app_version_present:
        issues.append(_issue("missing-app-version", "Lofty app version header value is missing."))
    if not sleep_nonnegative:
        issues.append(_issue("invalid-sleep", "Sleep delay must be non-negative."))

    access_key_present = bool(env.get("AWS_ACCESS_KEY_ID"))
    secret_key_present = bool(env.get("AWS_SECRET_ACCESS_KEY"))
    session_token_present = bool(env.get("AWS_SESSION_TOKEN"))
    aws_auth_configured = access_key_present and secret_key_present and session_token_present
    if not aws_auth_configured:
        issues.append(_issue("aws-auth-missing", "Runtime AWS credential environment is incomplete."))

    marketplace = {
        "path_provided": bool(args.marketplace),
        "exists": False,
        "is_file": False,
        "readable": False,
        "json_valid": False,
        "container_type": None,
        "id_count": 0,
    }
    marketplace_ids = []
    if not args.marketplace:
        issues.append(_issue("marketplace-missing", "Lofty marketplace JSON path is missing."))
    else:
        mp_path = Path(args.marketplace)
        marketplace["exists"] = mp_path.exists()
        marketplace["is_file"] = mp_path.is_file()
        marketplace["readable"] = os.access(mp_path, os.R_OK) if marketplace["exists"] else False
        if not marketplace["exists"]:
            issues.append(_issue("marketplace-not-found", "Lofty marketplace JSON path does not exist."))
        elif not marketplace["is_file"]:
            issues.append(_issue("marketplace-not-file", "Lofty marketplace path is not a file."))
        elif not marketplace["readable"]:
            issues.append(_issue("marketplace-not-readable", "Lofty marketplace JSON path is not readable."))
        else:
            try:
                data = json.loads(mp_path.read_text())
                marketplace["json_valid"] = True
                marketplace["container_type"] = type(data).__name__
                marketplace_ids = extract_marketplace_ids(data)
                marketplace["id_count"] = len(marketplace_ids)
                if not marketplace_ids:
                    issues.append(_issue("marketplace-no-property-ids", "Lofty marketplace JSON contains no extractable property IDs."))
            except json.JSONDecodeError:
                issues.append(_issue("marketplace-invalid-json", "Lofty marketplace path does not contain valid JSON."))
            except OSError:
                issues.append(_issue("marketplace-read-failed", "Lofty marketplace JSON could not be read."))

    output = {
        "path_provided": bool(args.out),
        "exists": False,
        "is_dir": False,
        "parent_exists": False,
        "parent_writable": False,
        "writable": False,
        "would_create_directory": False,
    }
    if not args.out:
        issues.append(_issue("output-missing", "Output directory path is missing."))
    else:
        out_dir = Path(args.out)
        parent = out_dir.parent
        output["exists"] = out_dir.exists()
        output["is_dir"] = out_dir.is_dir()
        output["parent_exists"] = parent.exists()
        output["parent_writable"] = os.access(parent, os.W_OK) if output["parent_exists"] else False
        output["writable"] = os.access(out_dir, os.W_OK) if output["exists"] else False
        output["would_create_directory"] = not output["exists"] and output["parent_exists"] and output["parent_writable"]
        if output["exists"] and not output["is_dir"]:
            issues.append(_issue("output-not-directory", "Output path exists but is not a directory."))
        elif output["exists"] and not output["writable"]:
            issues.append(_issue("output-not-writable", "Output directory is not writable."))
        elif not output["exists"] and not output["parent_exists"]:
            issues.append(_issue("output-parent-missing", "Output directory parent does not exist."))
        elif not output["exists"] and not output["parent_writable"]:
            issues.append(_issue("output-parent-not-writable", "Output directory parent is not writable."))

    review_command = _review_command(args)
    review_commands = [review_command] if issues else []
    review_command_validations = [
        {"command": command, "valid": _valid_review_command(command)}
        for command in review_commands
    ]

    status = "NO_REPLY" if not issues else "LOFTY_GETPROPERTYINFO_BATCH_REVIEW"
    classification = "ok" if not issues else CLASSIFICATION
    issue_summary = {}
    for issue in issues:
        issue_summary[issue["code"]] = issue_summary.get(issue["code"], 0) + 1

    mutation_flags = {
        "directory_create_attempted": False,
        "sigv4_sign_attempted": False,
        "network_attempted": False,
        "fetch_attempted": False,
        "file_write_attempted": False,
        "sleep_attempted": False,
        "delete_attempted": False,
        "sync_attempted": False,
        "restart_attempted": False,
        "sudo_attempted": False,
        "oauth_attempted": False,
        "external_send_attempted": False,
        "property_id_content_included": False,
        "credential_value_included": False,
        "response_body_included": False,
    }
    planned_count = marketplace["id_count"]
    report = {
        "status": status,
        "classification": classification,
        "ok_state": not issues,
        "visible_ok": not issues,
        "issue_count": len(issues),
        "issue_classes": sorted({issue["issue_class"] for issue in issues}),
        "classified_issues": issues,
        "issue_records": issues,
        "structured_issues": issues,
        "classified_issue_summary": issue_summary,
        "remediation_class": "review-diagnostic-output" if issues else "no-remediation-needed",
        "review_commands": review_commands,
        "review_command_validations": review_command_validations,
        "invalid_review_command_count": sum(1 for item in review_command_validations if not item["valid"]),
        "requests_available": requests_available,
        "botocore_available": botocore_available,
        "endpoint_valid": endpoint_valid,
        "start_time_valid": start_time_valid,
        "region_present": region_present,
        "service_present": service_present,
        "app_version_present": app_version_present,
        "sleep_nonnegative": sleep_nonnegative,
        "access_key_present": access_key_present,
        "secret_key_present": secret_key_present,
        "session_token_present": session_token_present,
        "aws_auth_configured": aws_auth_configured,
        "marketplace": marketplace,
        "output": output,
        "planned_property_count": planned_count,
        "planned_fetch_count": planned_count,
        "planned_write_count": planned_count,
        **mutation_flags,
    }
    return report


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Fetch Lofty getpropertyinfo JSON for all properties in marketplace.json")
    ap.add_argument("--marketplace", help="Path to marketplace.json")
    ap.add_argument("--out", help="Output directory for per-property JSON")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--start-time", default=DEFAULT_START_TIME)
    ap.add_argument("--region", default=DEFAULT_REGION)
    ap.add_argument("--service", default=DEFAULT_SERVICE)
    ap.add_argument("--app-version", default=DEFAULT_APP_VERSION)
    ap.add_argument("--sleep", type=float, default=0.2, help="Delay between requests")
    ap.add_argument("--json", action="store_true", help="Emit a no-action dashboard readiness report")
    return ap


def main(argv=None, stdout=None):
    stdout = sys.stdout if stdout is None else stdout
    ap = _build_parser()
    args = ap.parse_args(argv)

    if args.json:
        report = build_report(args)
        json.dump(report, stdout, indent=2, sort_keys=True)
        stdout.write("\n")
        return 0 if report["ok_state"] else 1

    if not args.marketplace:
        ap.error("--marketplace is required unless --json is used")
    if not args.out:
        ap.error("--out is required unless --json is used")

    mp_path = Path(args.marketplace)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    ids = load_marketplace_ids(mp_path)
    print(f"Found {len(ids)} property IDs", file=stdout)

    for i, pid in enumerate(ids, 1):
        try:
            fetch_property(pid, args.endpoint, args.start_time, args.region, args.service, args.app_version, out_dir)
            print(f"[{i}/{len(ids)}] OK {pid}", file=stdout)
        except Exception as e:
            print(f"[{i}/{len(ids)}] FAIL {pid}: {e}", file=stdout)
        time.sleep(args.sleep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
