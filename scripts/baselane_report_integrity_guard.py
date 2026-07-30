#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path



EXPECTED_LOCAL_MODEL = "ollama-cyber/qwen3.5:35b-a3b"
EXPECTED_LOCAL_PROVIDER = "ollama-cyber"
EXPECTED_LOCAL_MODEL_ID = "qwen3.5:35b-a3b"
REQUIRED_LOFTY_GUILD_ID = "847877825373012018"
OK_STATUSES = {"ok", "review", "failed"}
DAILY_SESSION_SEED_OK_STATUSES = {"ok", "failed_nonfatal", "timeout_nonfatal_180s"}
DAILY_ASSETRAIL_PUSH_OK_STATUSES = {
    "verified_current_clean",
    "committed_and_pushed",
    "clean_no_changes",
    "pushed_no_ledger_changes",
}
LEGACY_PUBLIC_UPDATES_PATH = "/Public/" + "Updates" + "/"
LEGACY_PUBLIC_FINANCIALS_PATH = "/Public/" + "Financials" + "/"
LFTY_PREFIX_RE = re.compile(r"^LFTY\d+\s+", re.IGNORECASE)
STALE_RECONCILIATION_MD_RE = re.compile(r"reconciliation[ _-]+report.*\.md$", re.IGNORECASE)
STALE_PNL_LEGACY_FILE_RE = re.compile(
    r"2026[-_]0[12].*(?:p&?l|pnl|profit[ _-]*(?:and|&)??[ _-]*loss)|"
    r"(?:p&?l|pnl|profit[ _-]*(?:and|&)??[ _-]*loss).*2026[-_]0[12]",
    re.IGNORECASE,
)
STALE_ACTION_TEXT_REPORTS = (
    "baselane_financials_operations_packet.json",
    "baselane_financials_operations_packet.md",
    "baselane_eod_telegram_report.json",
    "baselane_eod_telegram_preview_report.json",
    "baselane_financials_monthly_readiness.json",
    "baselane_financials_goal_audit.json",
    "baselane_financials_goal_audit.md",
    "baselane_monthly_owner_review_gate.json",
    "baselane_monthly_owner_review_gate.csv",
    "baselane_financials_monthly_guarded_apply.json",
    "baselane_financials_monthly_guarded_apply.md",
    "baselane_financials_monthly_lofty_pm_publish.json",
    "baselane_financials_monthly_lofty_pm_publish.md",
    "baselane_financials_monthly_live_update_capture.json",
    "baselane_financials_monthly_live_financial_capture.json",
    "baselane_financials_post_auth_resume_report.json",
    "lofty_cdp_preflight_report.json",
)
OPERATOR_TOPOLOGY_REPORTS = (
    *STALE_ACTION_TEXT_REPORTS,
    "baselane_daily_sync_report.json",
    "baselane_financials_post_auth_resume_report.json",
    "baselane_local_model_preflight_report.json",
    "lofty_financial_patch_readiness.json",
    "lofty_financial_patch_readiness.guard-reconcile.csv",
    "lofty_financial_patch_readiness.blocked-empty-patch.csv",
    "lofty_unreviewed_financial_approval_quarantine.json",
    "lofty_unreviewed_financial_approval_quarantine.requires-explicit-approval.sh",
)
OPERATOR_TOPOLOGY_FORBIDDEN_TOKENS = (
    ("/home/umbrel/.openclaw", "foreign_umbrel_openclaw_path"),
    ("/home/umbrel/.openclaw/workspace", "foreign_umbrel_workspace_path"),
    ("/mnt/f/.openclaw", "marlowe_vale_openclaw_path"),
    (
        "/mnt/c/Users/digit/Dropbox/Projects/cyber-gateway/config/openclaw/workspace",
        "stale_cyber_gateway_openclaw_workspace_path",
    ),
    (
        "/mnt/c/users/digit/Dropbox/Projects/cyber-gateway/config/openclaw/workspace",
        "stale_cyber_gateway_openclaw_workspace_path",
    ),
    ("GL Rows.csv", "discord_public_gl_rows_source"),
    (LEGACY_PUBLIC_FINANCIALS_PATH, "legacy_public_financials_source"),
)
STALE_ACTION_PHRASES = (
    "Download current Hemlane rent roll",
    "Refresh/reopen/auth Hemlane tab",
    "refresh/reopen/auth Hemlane tab",
    "refresh/reopen/auth the Hemlane tab",
    "refresh/reopen Hemlane tab",
    "hard-refresh/reopen Hemlane",
    "hard-refresh/reopen/auth Hemlane",
    "complete browser sign-in",
    "Open an authenticated Lofty PM tab",
    "Capture/register live Lofty PM UPDATES.md and FINANCIALS.md guards, then rerun guarded apply.",
    "Capture/register live Lofty UPDATES.md fetch with lofty-updates-guard before applying.",
    "Capture/register live Lofty FINANCIALS.md fetch with lofty-live-file-guard before applying.",
)
MONTHLY_CRON_COMMAND_TOKEN = "bash scripts/baselane_financials_monthly_cron.sh"
MONTHLY_SAFE_RERUN_TOKENS = (
    "DRY_RUN=1",
    "SEND_OWNER_EMAILS=0",
    "PUBLISH_LOFTY_PM_UPDATES=0",
    "APPLY_LOFTY_GUARDED_UPDATES=0",
)


def has_canonical_public_subpath(path_value, subpath: str) -> bool:
    text = str(path_value or "").replace("\\", "/")
    subpath = subpath.strip("/")
    return f"/Public/{subpath}" in text or f" Public/{subpath}" in text


def is_canonical_updates_path(path_value, *, file_name: str = "UPDATES.md") -> bool:
    suffix = "00 - README & Property Snapshot"
    if file_name:
        suffix = f"{suffix}/{file_name}"
    return has_canonical_public_subpath(path_value, suffix)


def is_canonical_financials_path(path_value, *, file_name: str = "FINANCIALS.md") -> bool:
    suffix = "00 - README & Property Snapshot"
    if file_name:
        suffix = f"{suffix}/{file_name}"
    return has_canonical_public_subpath(path_value, suffix)


def is_canonical_owner_statement_source_path(path_value) -> bool:
    text = str(path_value or "").replace("\\", "/")
    if not text.endswith(".csv"):
        return False
    if "ECO Systems General Ledger" not in Path(text).name:
        return False
    return has_canonical_public_subpath(text, "07 - P&L & Owner Statements")
LIVE_CAPTURE_APPLY_RE = re.compile(r"lofty_capture_live_(?:update|financial)_guards\.py\b.*--apply")
YHOME_OPERATING_CASH_TARGET_COLUMNS = ("Lofty Operating Cash", "ECO Net DAO Funds")
YHOME_OPERATING_CASH_REPORTS = (
    "yhome_operating_cash_apply_verify_report.json",
    "yhome_operating_cash_gsheet_update_report.json",
    "baselane_cf_balance_sheet_consistency_audit.json",
)
FUTURE_CF_VALUES_MAX_AGE_HOURS = 30.0
MONTHLY_SUMMARY_EXCLUSION_LABELS = {
    "excluded_total": "Excluded from update/email prep",
    "excluded_yhome": "Yhome sold/selling/closed/delisted excluded",
    "excluded_manual": "Manual exclusions",
    "excluded_local_closed": "Local closed/redeemed exclusions",
}


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str | None:
    try:
        if not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def default_root() -> Path:
    cwd = Path.cwd()
    if (cwd / "reports").is_dir() and (cwd / "scripts").is_dir():
        return cwd
    return Path(__file__).absolute().parents[1]


def read_json(path: Path) -> tuple[dict | None, str | None]:
    if not path.exists():
        return None, "missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"unreadable:{exc}"
    if not isinstance(payload, dict):
        return None, "not_json_object"
    return payload, None


def compact_count(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def compact_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def iso_age_hours(value: object) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() / 3600, 3)


def iso_timestamp(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def issue(code: str, detail: str) -> dict:
    return {"code": code, "detail": detail}


def guild_test_route_proof_ok(snapshot: dict) -> bool:
    target = str(snapshot.get("target") or "").strip()
    if not target.startswith("channel:"):
        return False
    selected = snapshot.get("selected") if isinstance(snapshot.get("selected"), dict) else {}
    route_report = snapshot.get("route_report") if isinstance(snapshot.get("route_report"), dict) else {}
    route_result = route_report.get("result") if isinstance(route_report.get("result"), dict) else {}
    property_name = str(selected.get("property_name") or snapshot.get("property_name") or "").strip()
    selected_ok = selected.get("route_matched") is True and str(selected.get("target") or "").strip() == target
    result_ok = route_result.get("route_matched") is True and str(route_result.get("target") or "").strip() == target
    return bool(property_name and (selected_ok or result_ok))


def guild_test_lofty_guild_ok(snapshot: dict) -> bool:
    selected = snapshot.get("selected") if isinstance(snapshot.get("selected"), dict) else {}
    route_report = snapshot.get("route_report") if isinstance(snapshot.get("route_report"), dict) else {}
    route_result = route_report.get("result") if isinstance(route_report.get("result"), dict) else {}
    envelope = snapshot.get("envelope") if isinstance(snapshot.get("envelope"), dict) else {}
    candidates = (
        snapshot.get("guild_id"),
        snapshot.get("guildId"),
        selected.get("guild_id"),
        selected.get("guildId"),
        route_result.get("guild_id"),
        route_result.get("guildId"),
        envelope.get("guild_id"),
        envelope.get("guildId"),
    )
    return any(str(candidate or "") == REQUIRED_LOFTY_GUILD_ID for candidate in candidates)


def real_estate_root_candidates(report_dir: Path) -> list[Path]:
    candidates = []
    env_root = os.environ.get("REAL_ESTATE_ROOT")
    if env_root:
        candidates.append(Path(env_root))
    env_roots = os.environ.get("BASELANE_STATEMENT_HYGIENE_ROOTS")
    if env_roots:
        candidates.extend(Path(root.strip()) for root in env_roots.split(os.pathsep) if root.strip())
    if (report_dir.parent / "scripts").is_dir():
        candidates.extend(
            [
                Path("/mnt/c/Users/digit/Dropbox/Real Estate"),
                Path("/home/digit/Dropbox/Real Estate"),
                Path("/data/Dropbox/Real Estate"),
            ]
        )
    candidates.append(report_dir.parent / "Dropbox" / "Real Estate")
    candidates.append(report_dir.parent / "pdf-extracts" / "real-estate")
    unique = []
    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def validate_statement_folder_hygiene(real_estate_root: Path) -> list[dict]:
    issues = []
    try:
        if not real_estate_root.exists():
            return issues
        stat = os.statvfs(real_estate_root)
        if stat.f_bavail * stat.f_frsize < 16 * 1024 * 1024:
            return [issue("statement_hygiene_real_estate_scan_skipped_low_disk_space", str(real_estate_root))]
        state_dirs = [Path(e.path) for e in os.scandir(real_estate_root) if e.is_dir(follow_symlinks=False)]
    except OSError as exc:
        return [issue("statement_hygiene_real_estate_scan_failed", f"{real_estate_root}:{exc}")]
    for state_dir in state_dirs:
        try:
            property_dirs = [Path(e.path) for e in os.scandir(state_dir) if e.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for property_dir in property_dirs:
            if not LFTY_PREFIX_RE.match(property_dir.name):
                continue
            try:
                if not any(os.scandir(property_dir)):
                    issues.append(issue("lfty_prefixed_empty_property_dir_present", str(property_dir)))
                nested_bank_roots = sorted(p for p in property_dir.rglob("Bank Statements") if p.is_dir())
            except OSError:
                issues.append(issue("lfty_prefixed_bank_statements_scan_failed", str(property_dir))); continue
            for nested_bank_root in nested_bank_roots:
                issues.append(issue("lfty_prefixed_bank_statements_dir_present", str(nested_bank_root)))
                try:
                    misplaced = sorted(nested_bank_root.rglob("BASELANE_*_STATEMENT*.pdf"))
                except OSError:
                    issues.append(issue("lfty_prefixed_bank_statements_scan_failed", str(nested_bank_root))); continue
                for path in misplaced[:20]:
                    issues.append(issue("lfty_prefixed_bank_statement_file_present", str(path)))
                if len(misplaced) > 20:
                    issues.append(issue("lfty_prefixed_bank_statement_file_present_truncated", f"{nested_bank_root}:{len(misplaced)}"))
    return issues


def validate_stale_financial_artifacts(real_estate_root: Path) -> list[dict]:
    """Reject legacy reconciliation markdown and Jan/Feb 2026 P&L exports."""
    issues = []
    try:
        if not real_estate_root.exists():
            return issues
        state_dirs = [Path(entry.path) for entry in os.scandir(real_estate_root) if entry.is_dir(follow_symlinks=False)]
    except OSError as exc:
        return [issue("stale_financial_artifact_scan_failed", f"{real_estate_root}:{exc}")]

    for state_dir in state_dirs:
        try:
            property_dirs = [Path(entry.path) for entry in os.scandir(state_dir) if entry.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for property_dir in property_dirs:
            try:
                seen_files = set()
                queue = [(property_dir, 0)]
                while queue:
                    current_path, depth = queue.pop(0)
                    try:
                        entries = list(os.scandir(current_path))
                    except OSError:
                        continue
                    for entry in entries:
                        path = Path(entry.path)
                        if entry.is_file(follow_symlinks=False):
                            if path in seen_files:
                                continue
                            seen_files.add(path)
                            if path.suffix.lower() == ".md" and STALE_RECONCILIATION_MD_RE.search(entry.name):
                                issues.append(issue("stale_financial_reconciliation_markdown_present", str(path)))
                            elif path.suffix.lower() in {".xlsx", ".csv"} and STALE_PNL_LEGACY_FILE_RE.search(entry.name):
                                issues.append(issue("stale_2026_01_02_pnl_statement_present", str(path)))
                            if len(issues) >= 100:
                                return issues + [issue("stale_financial_artifact_scan_truncated", str(real_estate_root))]
                            continue
                        if not entry.is_dir(follow_symlinks=False) or depth >= 3:
                            continue
                        name = entry.name.lower()
                        if name in {".dropbox.cache", "node_modules", "__pycache__", "bank statements", "tenant ledgers"}:
                            continue
                        if depth == 0 and name not in {"public", "financials", "07 - p&l & owner statements"}:
                            continue
                        if depth == 2 and name not in {"00 - readme & property snapshot", "07 - p&l & owner statements", "financials"}:
                            continue
                        queue.append((path, depth + 1))
            except OSError as exc:
                issues.append(issue("stale_financial_artifact_scan_failed", f"{property_dir}:{exc}"))
    return issues


def validate_stale_action_text(report_dir: Path) -> list[dict]:
    issues = []
    for name in STALE_ACTION_TEXT_REPORTS:
        path = report_dir / name
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            issues.append(issue("stale_action_text_scan_unreadable", name))
            continue
        except OSError as exc:
            issues.append(issue("stale_action_text_scan_failed", f"{name}:{exc}"))
            continue
        for phrase in STALE_ACTION_PHRASES:
            if phrase in text:
                issues.append(issue("stale_action_text", f"{name}:{phrase}"))
        for line_number, line in enumerate(text.splitlines(), start=1):
            lower_line = line.lower()
            if "hemlane visible tab" in lower_line and "baselane_financials_post_auth_resume.sh" not in line:
                issues.append(issue("stale_action_text", f"{name}:Hemlane visible tab without post-auth resume:{line_number}"))
            if LIVE_CAPTURE_APPLY_RE.search(line):
                issues.append(issue("unsafe_live_capture_apply_command", f"{name}:{line_number}"))
            if MONTHLY_CRON_COMMAND_TOKEN not in line:
                continue
            missing_tokens = [token for token in MONTHLY_SAFE_RERUN_TOKENS if token not in line]
            if missing_tokens:
                issues.append(
                    issue(
                        "unsafe_monthly_cron_rerun_command",
                        f"{name}:{line_number}:missing={','.join(missing_tokens)}",
                    )
                )
    return issues


def validate_operator_topology_scope(report_dir: Path) -> list[dict]:
    issues = []
    for name in OPERATOR_TOPOLOGY_REPORTS:
        path = report_dir / name
        if not path.exists():
            continue
        payload: dict | None = None
        if path.suffix == ".json":
            payload, _error = read_json(path)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            issues.append(issue("operator_topology_scan_unreadable", name))
            continue
        except OSError as exc:
            issues.append(issue("operator_topology_scan_failed", f"{name}:{exc}"))
            continue
        for token, code in OPERATOR_TOPOLOGY_FORBIDDEN_TOKENS:
            if token in text:
                if operator_topology_token_allowed(name, payload, token, code):
                    continue
                issues.append(issue(code, name))
    return issues


def operator_topology_token_allowed(name: str, payload: dict | None, token: str, code: str) -> bool:
    if code not in {"foreign_umbrel_openclaw_path", "foreign_umbrel_workspace_path"}:
        return False
    if not isinstance(payload, dict):
        return False
    candidates: list[dict] = []
    if name == "baselane_daily_sync_report.json":
        candidates.append(payload)
    if name in {"baselane_eod_telegram_preview_report.json", "baselane_eod_telegram_report.json"}:
        daily_summary = payload.get("daily_sync_summary")
        if isinstance(daily_summary, dict):
            candidates.append(daily_summary)
    for candidate in candidates:
        if (
            candidate.get("daily_run_workspace_root_aliases_current") is True
            and candidate.get("daily_run_foreign_workspace_root") is False
            and token
            in {
                str(candidate.get("daily_run_workspace_root") or ""),
                str(candidate.get("daily_run_openclaw_root") or ""),
            }
        ):
            return True
    return False


def csv_data_row_count(path: Path) -> tuple[int | None, str | None]:
    if not path.exists():
        return None, "missing"
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                return None, "missing_header"
            return sum(1 for _row in reader), None
    except Exception as exc:  # noqa: BLE001
        return None, f"unreadable:{exc}"


def csv_rows(path: Path) -> tuple[list[dict[str, str]], list[str], str | None]:
    if not path.exists():
        return [], [], "missing"
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                return [], [], "missing_header"
            return list(reader), list(reader.fieldnames), None
    except Exception as exc:  # noqa: BLE001
        return [], [], f"unreadable:{exc}"


def validate_yhome_operating_cash_targets(report_dir: Path) -> list[dict]:
    issues = []
    expected = list(YHOME_OPERATING_CASH_TARGET_COLUMNS)
    for name in YHOME_OPERATING_CASH_REPORTS:
        path = report_dir / name
        if not path.exists():
            continue
        payload, read_issue = read_json(path)
        if read_issue:
            issues.append(issue("yhome_target_report_read_error", f"{name}:{read_issue}"))
            continue
        payload = payload or {}
        observed = (
            payload.get("target_columns")
            or payload.get("yhome_target_columns")
            or payload.get("target_column_names")
        )
        if observed is None:
            issues.append(issue("yhome_target_columns_missing", name))
            continue
        if list(observed) != expected:
            issues.append(issue("yhome_target_columns_mismatch", f"{name}:{observed!r}!={expected!r}"))
    return issues


def parse_monthly_summary_exclusion_counts(summary_path: Path) -> dict[str, int | None]:
    try:
        text = summary_path.read_text(encoding="utf-8")
    except Exception:
        return {}
    counts: dict[str, int | None] = {}
    for key, label in MONTHLY_SUMMARY_EXCLUSION_LABELS.items():
        match = re.search(rf"^- {re.escape(label)}:\s+\*\*(\d+)\*\*\s*$", text, flags=re.MULTILINE)
        counts[key] = int(match.group(1)) if match else None
    return counts


def monthly_index_exclusion_counts(index_path: Path) -> dict[str, int] | None:
    try:
        with index_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except Exception:
        return None
    counts = {
        "excluded_total": 0,
        "excluded_yhome": 0,
        "excluded_manual": 0,
        "excluded_local_closed": 0,
    }
    for row in rows:
        status = str(row.get("status") or "").strip().lower()
        notes = str(row.get("notes") or "")
        is_excluded = status.startswith("skipped_") or status in {"sold", "closed", "delisted"}
        if not is_excluded:
            continue
        counts["excluded_total"] += 1
        if "source=yhome_transition_reconciliation" in notes:
            counts["excluded_yhome"] += 1
        elif "source=manual_exclusion" in notes:
            counts["excluded_manual"] += 1
        elif "property marked closed/redeemed" in notes:
            counts["excluded_local_closed"] += 1
    return counts


def validate_monthly_update_summary_exclusions(report_dir: Path, payloads: dict[str, dict]) -> list[dict]:
    monthly_run = payloads.get("baselane_financials_monthly_run_report.json") or {}
    run_month = str(monthly_run.get("run_month") or "").strip()
    if not run_month:
        return []
    comms_root = Path(os.environ.get("COMMS_WORKSPACE") or report_dir.parent.parent / "workspace-lofty-vp")
    if not comms_root.is_dir():
        comms_root = report_dir.parent.parent / "workspace-lofty-vp-comms"
    updates_dir = comms_root / "updates"
    index_path = updates_dir / f"{run_month}-portfolio-update-index.csv"
    summary_path = updates_dir / f"{run_month}-portfolio-update-summary.md"
    if not index_path.exists() and not summary_path.exists():
        return []
    issues: list[dict] = []
    if not index_path.exists():
        return [issue("monthly_update_index_missing_for_summary_guard", str(index_path))]
    if not summary_path.exists():
        return [issue("monthly_update_summary_missing_for_summary_guard", str(summary_path))]
    index_counts = monthly_index_exclusion_counts(index_path)
    if index_counts is None:
        return [issue("monthly_update_index_unreadable_for_summary_guard", str(index_path))]
    summary_counts = parse_monthly_summary_exclusion_counts(summary_path)
    for key, expected in index_counts.items():
        observed = summary_counts.get(key)
        if observed is None:
            issues.append(issue("monthly_update_summary_missing_exclusion_count", f"{key}:{MONTHLY_SUMMARY_EXCLUSION_LABELS[key]}"))
        elif observed != expected:
            issues.append(issue("monthly_update_summary_exclusion_count_mismatch", f"{key}:summary={observed},index={expected}"))
    return issues


def path_within_root(candidate: object, root: Path) -> bool:
    raw = str(candidate or "").strip()
    if not raw:
        return False
    try:
        path = Path(raw).expanduser()
        root_path = root.expanduser()
        if not path.is_absolute():
            path = root_path / path
        path = path.absolute()
        root_path = root_path.absolute()
        path.relative_to(root_path)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def eod_send_state_claims_success(payload: dict) -> bool:
    return (
        payload.get("status") == "ok"
        and payload.get("dry_run") is False
        and payload.get("telegram_send_ok") is True
        and payload.get("send_requested") is True
        and bool(payload.get("telegram_http_statuses") or [])
    )


def eod_send_state_transport_success(payload: dict) -> bool:
    return (
        payload.get("status") == "ok"
        and payload.get("dry_run") is False
        and payload.get("telegram_send_ok") is True
        and bool(payload.get("telegram_http_statuses") or [])
    )


def require_keys(payload: dict, keys: set[str]) -> list[dict]:
    missing = sorted(key for key in keys if key not in payload)
    if not missing:
        return []
    return [issue("missing_required_keys", ",".join(missing))]


def validate_status(payload: dict, *, allowed: set[str] = OK_STATUSES) -> list[dict]:
    status = str(payload.get("status") or "").strip()
    if status in allowed:
        return []
    return [issue("invalid_status", status or "missing")]


def valid_run_month(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}", value.strip()) is not None


def post_auth_step_status(step: dict) -> str:
    if step.get("ok") is not True:
        return "failed"
    if step.get("return_code") == 2:
        return "review"
    return "ok"


def validate_owner_review_gate(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload))
    issues.extend(
        require_keys(
            payload,
            {
                "blocker_count",
                "idempotency_key",
                "actionable_summary",
                "summary",
                "property_checklist",
                "primary_blocker",
                "next_action",
                "hold",
                "run_month",
            },
        )
    )
    actionable = payload.get("actionable_summary")
    summary = payload.get("summary")
    checklist = payload.get("property_checklist")
    if not isinstance(actionable, dict):
        issues.append(issue("invalid_actionable_summary", "not_object"))
    if not isinstance(summary, dict):
        issues.append(issue("invalid_summary", "not_object"))
        summary = {}
    if not valid_run_month(payload.get("run_month")):
        issues.append(issue("owner_gate_invalid_run_month", str(payload.get("run_month") or "missing")))
    summary_run_month = summary.get("run_month")
    if summary_run_month and payload.get("run_month") != summary_run_month:
        issues.append(issue("owner_gate_run_month_mismatch", f"top={payload.get('run_month')},summary={summary_run_month}"))
    if not isinstance(checklist, list):
        issues.append(issue("invalid_property_checklist", "not_list"))
        checklist = []
    property_count = compact_count(summary.get("property_count"))
    if property_count <= 0:
        issues.append(issue("invalid_property_count", str(property_count)))
    if property_count > 0 and len(checklist) != property_count:
        issues.append(issue("property_checklist_count_mismatch", f"{len(checklist)}!={property_count}"))
    primary = payload.get("primary_blocker")
    if isinstance(actionable, dict):
        actionable_primary = actionable.get("primary_blocker")
        if payload.get("status") == "review" and not isinstance(actionable_primary, dict):
            issues.append(issue("missing_primary_blocker", "review_requires_actionable_summary.primary_blocker"))
        if payload.get("status") == "review" and not isinstance(primary, dict):
            issues.append(issue("owner_gate_missing_primary_blocker_alias", "review_requires_top_level_primary_blocker"))
        if isinstance(actionable_primary, dict) and primary != actionable_primary:
            issues.append(issue("owner_gate_primary_blocker_alias_mismatch", "primary_blocker must match actionable_summary.primary_blocker"))
        if isinstance(primary, dict):
            missing_top_level_fields = [
                key
                for key in ("id", "class", "summary", "blocker", "artifact", "next_action", "hold")
                if key not in primary
            ]
            if missing_top_level_fields:
                issues.append(issue("owner_gate_primary_blocker_missing_fields", ",".join(missing_top_level_fields)))
        expected_primary = actionable_primary if isinstance(actionable_primary, dict) else primary
        if isinstance(expected_primary, dict):
            missing_blocker_fields = [
                key
                for key in ("id", "class", "summary", "blocker", "artifact", "next_action", "hold")
                if key not in expected_primary
            ]
            if missing_blocker_fields:
                issues.append(issue("owner_gate_primary_blocker_missing_fields", ",".join(missing_blocker_fields)))
            expected_next_action = expected_primary.get("next_action") or expected_primary.get("action")
            if payload.get("next_action") != expected_next_action:
                issues.append(issue("owner_gate_next_action_alias_mismatch", "next_action must match primary_blocker.next_action/action"))
            if payload.get("hold") != expected_primary.get("hold"):
                issues.append(issue("owner_gate_hold_alias_mismatch", "hold must match primary_blocker.hold"))
    return issues


def validate_goal_audit(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review"}))
    issues.extend(
        require_keys(
            payload,
            {
                "achieved",
                "actionable_summary",
                "requirements",
                "requirement_count",
                "ok_count",
                "review_count",
                "primary_blocker",
                "next_action",
                "hold",
                "run_month",
            },
        )
    )
    requirements = payload.get("requirements")
    actionable = payload.get("actionable_summary")
    if not isinstance(requirements, list):
        issues.append(issue("invalid_requirements", "not_list"))
        requirements = []
    if not valid_run_month(payload.get("run_month")):
        issues.append(issue("goal_audit_invalid_run_month", str(payload.get("run_month") or "missing")))
    if not requirements:
        issues.append(issue("empty_requirements", "goal audit must enumerate objective requirements"))
    if compact_count(payload.get("requirement_count")) != len(requirements):
        issues.append(issue("requirement_count_mismatch", f"{compact_count(payload.get('requirement_count'))}!={len(requirements)}"))
    primary = payload.get("primary_blocker")
    if not isinstance(actionable, dict):
        issues.append(issue("invalid_actionable_summary", "not_object"))
    else:
        actionable_primary = actionable.get("primary_blocker")
        if payload.get("status") == "review" and not isinstance(actionable_primary, dict):
            issues.append(issue("missing_primary_blocker", "review_requires_actionable_summary.primary_blocker"))
        if payload.get("status") == "review" and not isinstance(primary, dict):
            issues.append(issue("missing_primary_blocker_alias", "review_requires_top_level_primary_blocker"))
        if isinstance(actionable_primary, dict) and primary != actionable_primary:
            issues.append(issue("primary_blocker_alias_mismatch", "primary_blocker must match actionable_summary.primary_blocker"))
        expected_primary = actionable_primary if isinstance(actionable_primary, dict) else primary
        if isinstance(expected_primary, dict):
            if payload.get("next_action") != expected_primary.get("next_action"):
                issues.append(issue("next_action_alias_mismatch", "next_action must match primary_blocker.next_action"))
            if payload.get("hold") != expected_primary.get("hold"):
                issues.append(issue("hold_alias_mismatch", "hold must match primary_blocker.hold"))
        secondary = actionable.get("secondary_blockers")
        secondary_count = compact_count(actionable.get("secondary_blocker_count"))
        actionable_count = compact_count(actionable.get("actionable_blocker_count"))
        review_requirement_count = compact_count(actionable.get("review_requirement_count"))
        if payload.get("status") == "review":
            if not isinstance(secondary, list):
                issues.append(issue("goal_audit_missing_secondary_blockers", "review_requires_actionable_summary.secondary_blockers"))
                secondary = []
            if "secondary_blocker_count" not in actionable:
                issues.append(issue("goal_audit_missing_secondary_blocker_count", "review_requires_actionable_summary.secondary_blocker_count"))
            elif secondary_count != len(secondary):
                issues.append(issue("goal_audit_secondary_blocker_count_mismatch", f"{secondary_count}!={len(secondary)}"))
            if "actionable_blocker_count" not in actionable:
                issues.append(issue("goal_audit_missing_actionable_blocker_count", "review_requires_actionable_summary.actionable_blocker_count"))
            elif actionable_count != review_requirement_count:
                issues.append(issue("goal_audit_actionable_blocker_count_mismatch", f"{actionable_count}!={review_requirement_count}"))
            if actionable_count > 1 and not secondary:
                issues.append(issue("goal_audit_secondary_blockers_missing_for_concurrent_reviews", f"actionable_blocker_count={actionable_count}"))
        if isinstance(secondary, list):
            for index, item in enumerate(secondary):
                if not isinstance(item, dict):
                    issues.append(issue("goal_audit_invalid_secondary_blocker", f"index={index}"))
                    continue
                missing_secondary = [
                    key
                    for key in ("id", "requirement", "summary", "blocker", "artifact", "next_action", "hold")
                    if key not in item
                ]
                if missing_secondary:
                    issues.append(issue("goal_audit_secondary_blocker_missing_fields", f"{item.get('id') or index}:{','.join(missing_secondary)}"))
                if isinstance(primary, dict) and item.get("id") == primary.get("id"):
                    issues.append(issue("goal_audit_secondary_duplicates_primary", str(item.get("id"))))
        daily_disk_primary = (
            isinstance(primary, dict)
            and primary.get("id") == "daily_deterministic_sync"
            and (
                "daily_disk_space_preflight" in str(primary.get("artifact") or "")
                or "disk-space blocker" in str(primary.get("next_action") or "").lower()
                or "Free local Dropbox/Windows disk space" in str(primary.get("next_action") or "")
            )
        )
        if daily_disk_primary:
            disk_artifact = str(primary.get("artifact") or "reports/baselane_daily_disk_space_preflight_report.json")
            downstream_ids = {
                "monthly_bank_statement_capture",
                "monthly_review_and_guarded_apply",
                "lofty_pm_live_guard_workflow",
                "owner_email_idempotent_no_spam",
            }
            downstream_records = [
                record
                for record in list(requirements) + (secondary if isinstance(secondary, list) else [])
                if isinstance(record, dict) and record.get("id") in downstream_ids and record.get("status") != "ok"
            ]
            stale_action_markers = (
                "Rerun live UPDATES.md",
                "post message_file",
                "After readiness is clean",
                "Run BASELANE_MONTHLY_STATEMENTS_GATE_ONLY=1",
                "Authenticate Baselane in the visible browser tab",
                "Auth Lofty visible tab",
            )
            for record in downstream_records:
                record_id = str(record.get("id") or "unknown")
                action = str(record.get("next_action") or "")
                if str(record.get("artifact") or "") != disk_artifact:
                    issues.append(issue("goal_audit_daily_disk_downstream_artifact_mismatch", record_id))
                if "Resolve the daily Baselane disk-space blocker first" not in action:
                    issues.append(issue("goal_audit_daily_disk_downstream_next_action_missing", record_id))
                if any(marker in action for marker in stale_action_markers):
                    issues.append(issue("goal_audit_daily_disk_downstream_stale_action", record_id))
    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict):
            issues.append(issue("invalid_requirement", f"index={index}"))
            continue
        missing = [key for key in ("id", "status", "blocker", "artifact", "next_action") if key not in requirement]
        if missing:
            issues.append(issue("requirement_missing_action_fields", f"{requirement.get('id') or index}:{','.join(missing)}"))
        if requirement.get("id") == "eod_telegram_visibility" and requirement.get("status") == "ok":
            evidence = requirement.get("evidence") if isinstance(requirement.get("evidence"), dict) else {}
            direct_send_ok = (
                evidence.get("dry_run") is False
                and evidence.get("telegram_send_ok") is True
                and bool(evidence.get("telegram_http_statuses") or [])
            )
            prior_send_ok = (
                evidence.get("send_state_base_ok") is True
                and evidence.get("send_state_send_requested") is True
                and evidence.get("send_state_message_concise") is True
                and evidence.get("send_state_source_report_present") is True
                and evidence.get("send_state_source_report_scope_ok") is True
                and evidence.get("send_state_message_digest_ok") is True
                and evidence.get("send_state_source_report_generated_at_present") is True
            )
            if not direct_send_ok and not prior_send_ok:
                issues.append(issue("goal_eod_visibility_ok_without_send_proof", "eod_telegram_visibility"))
            if evidence.get("send_state_base_ok") is True and evidence.get("send_state_source_report_scope_ok") is False:
                issues.append(issue("goal_eod_visibility_ok_with_foreign_send_state_source", str(evidence.get("send_state_source_report") or "missing")))
            if evidence.get("send_state_base_ok") is True and evidence.get("send_state_message_digest_ok") is not True:
                issues.append(issue("goal_eod_visibility_ok_without_digest_bound_send_state", "eod_telegram_visibility"))
            if evidence.get("send_state_base_ok") is True and evidence.get("send_state_source_report_generated_at_present") is not True:
                issues.append(issue("goal_eod_visibility_ok_without_source_report_generated_at", "eod_telegram_visibility"))
    return issues


def validate_post_auth_resume(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed"}))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "job",
                "generated_at",
                "run_month",
                "root",
                "comms_root",
                "report",
                "safe_mode",
                "send_safety",
                "steps",
                "step_statuses",
                "failed_steps",
                "review_steps",
                "next_action",
            },
        )
    )
    if payload.get("job") != "baselane-financials-post-auth-resume":
        issues.append(issue("post_auth_invalid_job", str(payload.get("job") or "missing")))
    if not valid_run_month(payload.get("run_month")):
        issues.append(issue("post_auth_invalid_run_month", str(payload.get("run_month") or "missing")))
    if payload.get("safe_mode") is not True:
        issues.append(issue("post_auth_safe_mode_not_true", str(payload.get("safe_mode"))))
    send_safety = payload.get("send_safety")
    if not isinstance(send_safety, dict):
        issues.append(issue("post_auth_invalid_send_safety", "not_object"))
        send_safety = {}
    for key, expected in (
        ("dry_run", True),
        ("send_owner_emails", False),
        ("publish_lofty_pm_updates", False),
        ("apply_lofty_guarded_updates", False),
    ):
        if send_safety.get(key) is not expected:
            issues.append(issue("post_auth_unsafe_send_safety", f"{key}={send_safety.get(key)}"))
    steps = payload.get("steps")
    step_statuses = payload.get("step_statuses")
    failed_steps = payload.get("failed_steps")
    review_steps = payload.get("review_steps")
    if not isinstance(steps, list):
        issues.append(issue("post_auth_invalid_steps", "not_list"))
        steps = []
    if not isinstance(step_statuses, dict):
        issues.append(issue("post_auth_invalid_step_statuses", "not_object"))
        step_statuses = {}
    if not isinstance(failed_steps, list):
        issues.append(issue("post_auth_invalid_failed_steps", "not_list"))
        failed_steps = []
    if not isinstance(review_steps, list):
        issues.append(issue("post_auth_invalid_review_steps", "not_list"))
        review_steps = []
    step_names = [str(step.get("name") or "") for step in steps if isinstance(step, dict)]
    stopped_after_auth_review = (
        payload.get("post_auth_resume_stopped_after_auth_review") is True
        and "baselane_auth_preflight" in step_names
        and str(step_statuses.get("baselane_auth_preflight") or "") == "review"
    )
    if not stopped_after_auth_review:
        for required_step in ("goal_audit", "report_integrity_guard"):
            if required_step not in step_names:
                issues.append(issue("post_auth_missing_required_step", required_step))
    final_eod = payload.get("final_eod_no_send_refresh")
    if isinstance(final_eod, dict):
        if "final_eod_no_send_refresh" not in step_names:
            issues.append(issue("post_auth_final_eod_not_step", "final_eod_no_send_refresh"))
        elif "report_integrity_guard" in step_names and step_names.index("final_eod_no_send_refresh") > step_names.index("report_integrity_guard"):
            issues.append(issue("post_auth_final_eod_after_integrity", ",".join(step_names)))
    if "goal_audit" in step_names and "report_integrity_guard" in step_names:
        if step_names.index("goal_audit") > step_names.index("report_integrity_guard"):
            issues.append(issue("post_auth_integrity_before_goal_audit", ",".join(step_names)))
        if step_names[-1] != "report_integrity_guard":
            issues.append(issue("post_auth_integrity_not_final_step", ",".join(step_names)))
    computed_statuses: dict[str, str] = {}
    for step in steps:
        if not isinstance(step, dict):
            issues.append(issue("post_auth_invalid_step", "not_object"))
            continue
        name = str(step.get("name") or "")
        if not name:
            issues.append(issue("post_auth_step_missing_name", "name"))
            continue
        computed_statuses[name] = post_auth_step_status(step)
        if step.get("ok") is not True:
            issues.append(issue("post_auth_step_not_ok", name))
    if step_statuses and computed_statuses and step_statuses != computed_statuses:
        issues.append(issue("post_auth_step_status_mismatch", json.dumps({"reported": step_statuses, "computed": computed_statuses}, sort_keys=True)))
    computed_failed = sorted(name for name, status in computed_statuses.items() if status == "failed")
    computed_review = sorted(name for name, status in computed_statuses.items() if status == "review")
    if sorted(str(name) for name in failed_steps) != computed_failed:
        issues.append(issue("post_auth_failed_steps_mismatch", json.dumps({"reported": failed_steps, "computed": computed_failed}, sort_keys=True)))
    if sorted(str(name) for name in review_steps) != computed_review:
        issues.append(issue("post_auth_review_steps_mismatch", json.dumps({"reported": review_steps, "computed": computed_review}, sort_keys=True)))
    if payload.get("status") == "ok" and (computed_failed or computed_review):
        issues.append(issue("post_auth_ok_with_open_steps", json.dumps({"failed": computed_failed, "review": computed_review}, sort_keys=True)))
    if payload.get("status") == "review" and computed_failed:
        issues.append(issue("post_auth_review_with_failed_steps", ",".join(computed_failed)))
    if payload.get("status") == "failed" and not computed_failed:
        issues.append(issue("post_auth_failed_without_failed_steps", "failed_steps"))
    return issues


def validate_eod_send_state(payload: dict, *, report_dir: Path) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed"}))
    issues.extend(require_keys(payload, {"status", "dry_run", "telegram_send_ok", "telegram_http_statuses"}))
    if not eod_send_state_transport_success(payload):
        return issues
    if payload.get("send_requested") is not True:
        issues.append(issue("eod_send_state_success_missing_send_request", "send_requested"))
    source_report = str(payload.get("source_report") or "").strip()
    if not source_report:
        issues.append(issue("eod_send_state_success_missing_source_report", "source_report"))
        return issues
    if not path_within_root(source_report, report_dir):
        issues.append(issue("eod_send_state_success_foreign_source_report", source_report))
        return issues
    source_path = Path(source_report).expanduser()
    source_report_allowed = (
        source_path.name == "baselane_eod_telegram_report.json"
        or (
            source_path.parent.name == "eod_telegram_send_proofs"
            and source_path.name.startswith("baselane_eod_telegram_report.")
            and source_path.suffix == ".json"
        )
    )
    if not source_report_allowed:
        issues.append(issue("eod_send_state_success_unexpected_source_report", source_path.name or "missing"))
    source_payload = None
    if not source_path.exists():
        issues.append(issue("eod_send_state_success_source_report_missing", source_report))
    else:
        try:
            source_payload = json.loads(source_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            issues.append(issue("eod_send_state_success_source_report_unreadable", str(exc)))
    message = str(payload.get("message") or "")
    if not message.strip():
        issues.append(issue("eod_send_state_success_missing_message", "message"))
    message_digest = str(payload.get("source_report_message_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", message_digest):
        issues.append(issue("eod_send_state_success_missing_message_digest", message_digest or "missing"))
    elif message_digest != sha256_text(message):
        issues.append(issue("eod_send_state_success_message_digest_mismatch", message_digest))
    sent_digest = str(payload.get("telegram_sent_message_sha256") or "")
    if sent_digest and sent_digest != message_digest:
        issues.append(issue("eod_send_state_success_sent_digest_mismatch", f"sent={sent_digest},source={message_digest}"))
    source_report_generated_at = str(payload.get("source_report_generated_at") or "").strip()
    if not source_report_generated_at:
        issues.append(issue("eod_send_state_success_missing_source_report_generated_at", "source_report_generated_at"))
    if isinstance(source_payload, dict):
        source_message = str(source_payload.get("message") or "")
        source_generated_at = str(source_payload.get("generated_at") or "").strip()
        if source_payload.get("dry_run") is not False:
            issues.append(issue("eod_send_state_success_source_report_not_non_dry_run", str(source_payload.get("dry_run"))))
        if source_payload.get("send_requested") is not True:
            issues.append(issue("eod_send_state_success_source_report_missing_send_request", str(source_payload.get("send_requested"))))
        if source_payload.get("telegram_send_ok") is not True:
            issues.append(issue("eod_send_state_success_source_report_not_sent", str(source_payload.get("telegram_send_ok"))))
        if not source_message.strip():
            issues.append(issue("eod_send_state_success_source_report_missing_message", source_report))
        elif message and source_message != message:
            issues.append(issue("eod_send_state_success_source_report_message_mismatch", source_report))
        if source_message and re.fullmatch(r"[0-9a-f]{64}", message_digest) and message_digest != sha256_text(source_message):
            issues.append(issue("eod_send_state_success_source_report_digest_mismatch", message_digest))
        if not source_generated_at:
            issues.append(issue("eod_send_state_success_source_report_missing_generated_at", source_report))
        elif source_report_generated_at and source_report_generated_at != source_generated_at:
            issues.append(
                issue(
                    "eod_send_state_success_source_report_generated_at_mismatch",
                    f"state={source_report_generated_at},source={source_generated_at}",
                )
            )
    return issues


def validate_eod_telegram_report(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed"}))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "dry_run",
                "message",
                "message_quality",
                "message_chunk_count",
                "message_chunks",
                "message_character_count",
                "daily_sync_summary",
                "owner_exclusion_summary",
                "daily_sync_report_refresh",
                "goal_audit_refresh",
                "lofty_empty_updates_backfill_queue_refresh",
                "lofty_empty_updates_backfill_queue_summary",
                "telegram_send_ok",
                "telegram_http_statuses",
            },
        )
    )
    message = str(payload.get("message") or "")
    daily_sync_auth_blocked = (
        payload.get("daily_sync_auth_blocked") is True
        and str(payload.get("daily_sync_auth_blocker_reason") or "").strip()
        in {"baselane_login_auth_401", "baselane_login_recaptcha_required", "baselane_login_required", "baselane_manual_auth_required"}
    )
    daily_sync_disk_space_blocked = (
        payload.get("daily_sync_disk_space_blocked") is True
        and str(payload.get("daily_sync_disk_space_blocker_reason") or "").strip()
        == "low_local_disk_space"
    )

    def refresh_skipped_by_daily_sync_blocker(refresh: dict) -> bool:
        if not isinstance(refresh, dict) or refresh.get("attempted") is not False:
            return False
        if daily_sync_disk_space_blocked:
            return (
                refresh.get("reason") == "daily_sync_disk_space_blocker"
                and refresh.get("blocker") == payload.get("daily_sync_disk_space_blocker_reason")
            )
        return (
            daily_sync_auth_blocked
            and refresh.get("reason") == "daily_sync_auth_blocker"
            and refresh.get("blocker") == payload.get("daily_sync_auth_blocker_reason")
        )

    if not message.strip():
        issues.append(issue("eod_report_empty_message", "message"))
    if "Sync:" not in message and "Daily sync:" not in message:
        issues.append(issue("eod_report_missing_daily_sync_line", "message"))
    quality = payload.get("message_quality")
    if not isinstance(quality, dict):
        issues.append(issue("eod_report_invalid_message_quality", "not_object"))
        quality = {}
    if quality.get("ok") is not True:
        issues.append(issue("eod_report_message_quality_not_ok", str(quality.get("issues") or quality.get("noise_markers") or "false")))
    line_count = compact_count(quality.get("line_count"))
    max_lines = compact_count(quality.get("max_lines")) or 8
    character_count = compact_count(quality.get("character_count"))
    max_chars = compact_count(quality.get("max_chars")) or 520
    if line_count <= 0 or line_count > max_lines:
        issues.append(issue("eod_report_line_count_out_of_bounds", f"{line_count}>{max_lines}"))
    if character_count <= 0 or character_count > max_chars:
        issues.append(issue("eod_report_character_count_out_of_bounds", f"{character_count}>{max_chars}"))
    if quality.get("issues"):
        issues.append(issue("eod_report_message_quality_has_issues", str(quality.get("issues"))))
    if quality.get("noise_markers"):
        issues.append(issue("eod_report_message_quality_has_noise", str(quality.get("noise_markers"))))
    chunks = payload.get("message_chunks")
    if not isinstance(chunks, list) or not chunks:
        issues.append(issue("eod_report_invalid_message_chunks", "missing_or_not_list"))
        chunks = []
    if compact_count(payload.get("message_chunk_count")) != len(chunks):
        issues.append(issue("eod_report_message_chunk_count_mismatch", f"{payload.get('message_chunk_count')}!={len(chunks)}"))
    if compact_count(payload.get("message_character_count")) > max_chars * max(1, len(chunks)):
        issues.append(issue("eod_report_total_character_count_out_of_bounds", str(payload.get("message_character_count"))))
    owner_exclusion = payload.get("owner_exclusion_summary")
    if not isinstance(owner_exclusion, dict):
        issues.append(issue("eod_report_invalid_owner_exclusion_summary", "not_object"))
        owner_exclusion = {}
    else:
        active_total_counts = owner_exclusion.get("active_total_source_counts")
        component_counts = owner_exclusion.get("component_counts")
        if not isinstance(active_total_counts, dict) or not active_total_counts:
            issues.append(issue("eod_report_invalid_owner_exclusion_active_totals", "missing_or_not_object"))
            active_total_counts = {}
        if not isinstance(component_counts, dict):
            issues.append(issue("eod_report_invalid_owner_exclusion_components", "not_object"))
            component_counts = {}
        active_count = compact_count(owner_exclusion.get("active_excluded_count"))
        message_skip_count = compact_count(owner_exclusion.get("message_skip_count"))
        expected_active_count = max([compact_count(value) for value in active_total_counts.values()] or [0])
        if active_count != expected_active_count:
            issues.append(issue("eod_report_owner_exclusion_active_count_mismatch", f"active={active_count},expected={expected_active_count}"))
        if message_skip_count != active_count:
            issues.append(issue("eod_report_owner_exclusion_message_skip_mismatch", f"message={message_skip_count},active={active_count}"))
        nonzero_totals = [compact_count(value) for value in active_total_counts.values() if compact_count(value)]
        if nonzero_totals and len(set(nonzero_totals)) > 1:
            issues.append(issue("eod_report_owner_exclusion_active_totals_mismatch", json.dumps(active_total_counts, sort_keys=True)))
        if owner_exclusion.get("active_total_source_counts_match") is False:
            issues.append(issue("eod_report_owner_exclusion_active_totals_match_false", "active_total_source_counts_match=false"))
        manual_names = owner_exclusion.get("manual_excluded_property_names")
        if isinstance(manual_names, list) and compact_count(owner_exclusion.get("manual_excluded_count")) != len(manual_names):
            issues.append(issue("eod_report_owner_exclusion_manual_count_mismatch", f"{owner_exclusion.get('manual_excluded_count')}!={len(manual_names)}"))
        skip_match = re.search(
            r"^SKIP: (\d+)(?: excluded| sold/delisted/closed/manual excluded)$",
            message,
            flags=re.MULTILINE,
        )
        if skip_match and int(skip_match.group(1)) != message_skip_count:
            issues.append(issue("eod_report_owner_exclusion_skip_line_mismatch", f"line={skip_match.group(1)},summary={message_skip_count}"))

    daily_summary = payload.get("daily_sync_summary")
    if not isinstance(daily_summary, dict):
        issues.append(issue("eod_report_invalid_daily_sync_summary", "not_object"))
        daily_summary = {}
    for key in (
        "daily_run_report",
        "daily_run_status",
        "daily_run_return_code",
        "daily_run_failed_step",
        "daily_run_generated_at",
        "daily_run_started_at",
        "daily_run_ended_at",
        "daily_run_duration_seconds",
        "daily_run_age_hours",
        "daily_run_max_age_hours",
        "daily_run_fresh",
        "daily_sync_report",
        "status",
        "effective_status",
        "sync_report_status",
    ):
        if key not in daily_summary:
            issues.append(issue("eod_report_daily_sync_summary_missing_key", key))
    for key, expected in (
        ("status", "ok"),
        ("effective_status", "ok"),
        ("sync_report_status", "ok"),
    ):
        if key == "status" and daily_summary.get("effective_status") == "ok" and daily_summary.get("sync_report_status") == "ok":
            continue
        if daily_summary.get(key) != expected:
            issues.append(issue("eod_report_daily_sync_summary_not_ok", f"{key}={daily_summary.get(key)}"))
    daily_effectively_ok = daily_summary.get("effective_status") == "ok" and daily_summary.get("sync_report_status") == "ok"
    if daily_summary.get("local_model_ready") is not True and not daily_effectively_ok:
        issues.append(issue("eod_report_daily_sync_local_model_not_ready", str(daily_summary.get("local_model_ready"))))
    if daily_summary.get("daily_run_fresh") is not True:
        issues.append(issue("eod_report_daily_run_not_fresh", str(daily_summary.get("daily_run_age_hours"))))
    if compact_count(daily_summary.get("issue_count")) and not daily_effectively_ok:
        issues.append(issue("eod_report_daily_sync_issue_count_nonzero", str(daily_summary.get("issue_count"))))
    if "assetrail_live_status" in daily_summary and daily_summary.get("assetrail_live_status") != "ok":
        issues.append(issue("eod_report_daily_sync_assetrail_not_ok", str(daily_summary.get("assetrail_live_status"))))
    if daily_summary.get("source_cash_balance_status") == "ok":
        if "cash=" not in message:
            issues.append(issue("eod_report_message_missing_source_cash_status", "cash="))
        elif daily_summary.get("source_cash_balance_report_fresh") is False and "cash=stale" not in message:
            issues.append(issue("eod_report_message_source_cash_stale_mismatch", "cash=stale"))
        elif compact_count(daily_summary.get("source_cash_balance_violation_count")) and "cash=violations(" not in message:
            issues.append(issue("eod_report_message_source_cash_violation_mismatch", "cash=violations"))

    daily_refresh = payload.get("daily_sync_report_refresh")
    if not isinstance(daily_refresh, dict):
        issues.append(issue("eod_report_invalid_daily_sync_refresh", "not_object"))
    else:
        if daily_refresh.get("attempted") is not True:
            issues.append(issue("eod_report_daily_sync_refresh_not_attempted", str(daily_refresh.get("attempted"))))
        if daily_refresh.get("ok") is not True:
            issues.append(issue("eod_report_daily_sync_refresh_not_ok", str(daily_refresh.get("ok"))))
    goal_refresh = payload.get("goal_audit_refresh")
    if not isinstance(goal_refresh, dict):
        issues.append(issue("eod_report_invalid_goal_audit_refresh", "not_object"))
    elif refresh_skipped_by_daily_sync_blocker(goal_refresh):
        pass
    else:
        if goal_refresh.get("attempted") is not True:
            issues.append(issue("eod_report_goal_audit_refresh_not_attempted", str(goal_refresh.get("attempted"))))
        if goal_refresh.get("ok") is not True:
            issues.append(issue("eod_report_goal_audit_refresh_not_ok", str(goal_refresh.get("ok"))))

    if payload.get("dry_run") is False and payload.get("send_requested") is True and payload.get("status") == "ok":
        if payload.get("telegram_send_ok") is not True:
            issues.append(issue("eod_report_non_dry_run_without_send_ok", str(payload.get("telegram_send_ok"))))
        if not payload.get("telegram_http_statuses"):
            issues.append(issue("eod_report_non_dry_run_without_http_statuses", "telegram_http_statuses"))
    if payload.get("dry_run") is True and payload.get("telegram_send_ok") is True:
        issues.append(issue("eod_report_dry_run_claims_send_ok", "telegram_send_ok"))
    empty_updates_refresh = payload.get("lofty_empty_updates_backfill_queue_refresh")
    if not isinstance(empty_updates_refresh, dict):
        issues.append(issue("eod_report_invalid_empty_updates_queue_refresh", "not_object"))
    elif refresh_skipped_by_daily_sync_blocker(empty_updates_refresh):
        pass
    else:
        if empty_updates_refresh.get("attempted") is not True:
            issues.append(issue("eod_report_empty_updates_queue_refresh_not_attempted", str(empty_updates_refresh.get("attempted"))))
        if empty_updates_refresh.get("ok") is not True:
            issues.append(issue("eod_report_empty_updates_queue_refresh_not_ok", str(empty_updates_refresh.get("ok"))))
    empty_updates_summary = payload.get("lofty_empty_updates_backfill_queue_summary")
    if not isinstance(empty_updates_summary, dict):
        issues.append(issue("eod_report_invalid_empty_updates_queue_summary", "not_object"))
    else:
        if empty_updates_summary.get("mutates_dropbox_files") is True:
            issues.append(issue("eod_report_empty_updates_queue_mutates_dropbox", "true"))
        if empty_updates_summary.get("mutates_lofty_listing") is True:
            issues.append(issue("eod_report_empty_updates_queue_mutates_lofty", "true"))
        if empty_updates_summary.get("sends_owner_email") is True:
            issues.append(issue("eod_report_empty_updates_queue_sends_email", "true"))
        if empty_updates_summary.get("commands_require_explicit_approval") is not True:
            issues.append(issue("eod_report_empty_updates_queue_commands_not_approval_gated", str(empty_updates_summary.get("commands_require_explicit_approval"))))
        if empty_updates_summary.get("approval_copy_requires_current_rent_roll") is not True:
            issues.append(issue("eod_report_empty_updates_queue_approval_not_rent_roll_gated", str(empty_updates_summary.get("approval_copy_requires_current_rent_roll"))))
    return issues


def validate_daily_run(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed"}))
    issues.extend(
        require_keys(
            payload,
            {
                "job",
                "generated_at",
                "status",
                "reported_status",
                "return_code",
                "reported_return_code",
                "started_at",
                "ended_at",
                "finished_at",
                "duration_seconds",
                "failed_step",
                "steps",
                "sync_report_status",
                "wrapper_consistency_issues",
            },
        )
    )
    if payload.get("job") != "baselane-daily-sync":
        issues.append(issue("daily_run_unexpected_job", str(payload.get("job") or "missing")))
    if not str(payload.get("generated_at") or "").strip():
        issues.append(issue("daily_run_missing_generated_at", "generated_at"))
    elif payload.get("ended_at") and payload.get("generated_at") != payload.get("ended_at"):
        issues.append(issue("daily_run_generated_at_ended_at_mismatch", f"{payload.get('generated_at')}!={payload.get('ended_at')}"))
    if not str(payload.get("finished_at") or "").strip():
        issues.append(issue("daily_run_missing_finished_at", "finished_at"))
    elif payload.get("ended_at") and payload.get("finished_at") != payload.get("ended_at"):
        issues.append(issue("daily_run_finished_at_ended_at_mismatch", f"{payload.get('finished_at')}!={payload.get('ended_at')}"))
    if compact_count(payload.get("duration_seconds")) <= 0:
        issues.append(issue("daily_run_invalid_duration_seconds", str(payload.get("duration_seconds"))))
    if compact_count(payload.get("return_code")) != compact_count(payload.get("reported_return_code")):
        issues.append(issue("daily_run_return_code_reported_mismatch", f"{payload.get('return_code')}!={payload.get('reported_return_code')}"))
    if payload.get("status") != payload.get("reported_status"):
        issues.append(issue("daily_run_status_reported_mismatch", f"{payload.get('status')}!={payload.get('reported_status')}"))
    steps = payload.get("steps")
    if not isinstance(steps, dict):
        issues.append(issue("daily_run_invalid_steps", "not_object"))
        steps = {}
    for step in ("session_seed", "local_model_preflight", "deterministic_sync"):
        if step not in steps:
            issues.append(issue("daily_run_missing_step", step))
    if payload.get("wrapper_consistency_issues") not in ([], None):
        issues.append(issue("daily_run_wrapper_consistency_issues_nonempty", str(payload.get("wrapper_consistency_issues"))))
    if payload.get("status") == "ok":
        if compact_count(payload.get("return_code")) != 0:
            issues.append(issue("daily_run_ok_return_code_nonzero", str(payload.get("return_code"))))
        if payload.get("failed_step") not in (None, ""):
            issues.append(issue("daily_run_ok_failed_step_present", str(payload.get("failed_step"))))
        if steps.get("session_seed") not in DAILY_SESSION_SEED_OK_STATUSES:
            issues.append(issue("daily_run_ok_session_seed_not_ok", str(steps.get("session_seed") or "missing")))
        if steps.get("local_model_preflight") != "ok":
            issues.append(issue("daily_run_ok_local_model_preflight_not_ok", str(steps.get("local_model_preflight") or "missing")))
        if steps.get("deterministic_sync") != "ok":
            issues.append(issue("daily_run_ok_deterministic_sync_not_ok", str(steps.get("deterministic_sync") or "missing")))
    if payload.get("status") == "failed":
        if compact_count(payload.get("return_code")) == 0:
            issues.append(issue("daily_run_failed_return_code_zero", str(payload.get("return_code"))))
        if not str(payload.get("failed_step") or "").strip():
            issues.append(issue("daily_run_failed_missing_failed_step", "failed_step"))
    return issues


def validate_monthly_run(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "run_month",
                "effective_status",
                "return_code",
                "effective_return_code",
                "effective_failed_step",
                "effective_send_owner_emails",
                "send_owner_emails",
                "require_yhome_sold_guard",
                "yhome_sold_guard_status",
                "yhome_transition_reconciliation",
                "lofty_update_drafts_status",
                "monthly_readiness_owner_email_allowed",
                "monthly_readiness_blocker_count",
                "monthly_readiness_actionable_blocker_count",
                "owner_email_send_guard_status",
                "owner_email_send_guard_send_allowed",
                "owner_email_send_guard_issue_count",
                "owner_email_send_blocked_reason",
                "owner_email_packet_status",
                "owner_email_packet_issue_count",
                "owner_email_packet_safe_to_send_now",
                "owner_email_packet_full_history_leak_count",
                "owner_email_packet_body_guard_issue_count",
                "steps",
                "review_step_names",
            },
        )
    )
    if str(payload.get("effective_status") or "").strip() not in OK_STATUSES:
        issues.append(issue("invalid_effective_status", str(payload.get("effective_status") or "missing")))
    if not valid_run_month(payload.get("run_month")):
        issues.append(issue("monthly_run_invalid_run_month", str(payload.get("run_month") or "missing")))
    if not isinstance(payload.get("steps"), dict):
        issues.append(issue("invalid_steps", "not_object"))
    if not isinstance(payload.get("review_step_names"), list):
        issues.append(issue("invalid_review_step_names", "not_list"))
    if not isinstance(payload.get("effective_send_owner_emails"), bool):
        issues.append(issue("invalid_effective_send_owner_emails", "not_bool"))
    if not isinstance(payload.get("send_owner_emails"), bool):
        issues.append(issue("invalid_send_owner_emails", "not_bool"))
    disk_preflight_hold = (
        payload.get("disk_space_preflight_status") == "review"
        and payload.get("effective_failed_step") == "baselane_disk_space_preflight"
    )
    if payload.get("require_yhome_sold_guard") is True and not disk_preflight_hold:
        if str(payload.get("yhome_sold_guard_status") or "").strip() != "ok":
            issues.append(issue("monthly_run_yhome_sold_guard_not_ok", str(payload.get("yhome_sold_guard_status") or "missing")))
        if not str(payload.get("yhome_transition_reconciliation") or "").strip():
            issues.append(issue("monthly_run_missing_yhome_transition_reconciliation", "yhome_transition_reconciliation"))
    steps = payload.get("steps") if isinstance(payload.get("steps"), dict) else {}
    if payload.get("yhome_sold_guard_status") and steps.get("yhome_sold_guard") and payload.get("yhome_sold_guard_status") != steps.get("yhome_sold_guard"):
        issues.append(issue("monthly_run_yhome_sold_guard_step_mismatch", f"{payload.get('yhome_sold_guard_status')}!={steps.get('yhome_sold_guard')}"))
    if not str(payload.get("lofty_update_drafts_status") or "").strip():
        issues.append(issue("monthly_run_missing_lofty_update_drafts_status", "lofty_update_drafts_status"))
    if payload.get("effective_send_owner_emails") is True:
        if payload.get("monthly_readiness_owner_email_allowed") is not True:
            issues.append(issue("monthly_run_effective_email_without_readiness", str(payload.get("monthly_readiness_owner_email_allowed"))))
        if payload.get("owner_email_send_guard_send_allowed") is not True:
            issues.append(issue("monthly_run_effective_email_without_guard_send_allowed", str(payload.get("owner_email_send_guard_send_allowed"))))
        if compact_count(payload.get("owner_email_send_guard_issue_count")):
            issues.append(issue("monthly_run_effective_email_with_guard_issues", str(payload.get("owner_email_send_guard_issue_count"))))
        if payload.get("owner_email_packet_status") != "ok":
            issues.append(issue("monthly_run_effective_email_without_packet_ok", str(payload.get("owner_email_packet_status") or "missing")))
        if compact_count(payload.get("owner_email_packet_issue_count")):
            issues.append(issue("monthly_run_effective_email_with_packet_issues", str(payload.get("owner_email_packet_issue_count"))))
        if payload.get("owner_email_packet_safe_to_send_now") is not True:
            issues.append(issue("monthly_run_effective_email_packet_not_safe", str(payload.get("owner_email_packet_safe_to_send_now"))))
        if compact_count(payload.get("owner_email_packet_full_history_leak_count")):
            issues.append(issue("monthly_run_effective_email_packet_full_history_leak", str(payload.get("owner_email_packet_full_history_leak_count"))))
        if compact_count(payload.get("owner_email_packet_body_guard_issue_count")):
            issues.append(issue("monthly_run_effective_email_packet_body_guard_issue", str(payload.get("owner_email_packet_body_guard_issue_count"))))
    if payload.get("send_owner_emails") is True and payload.get("monthly_readiness_owner_email_allowed") is False:
        if payload.get("effective_send_owner_emails") is not False:
            issues.append(issue("monthly_run_email_request_not_safely_blocked", str(payload.get("effective_send_owner_emails"))))
        if not str(payload.get("owner_email_send_blocked_reason") or "").strip():
            issues.append(issue("monthly_run_missing_email_blocked_reason", "owner_email_send_blocked_reason"))
    monthly_gap_count = compact_count(payload.get("monthly_completion_gap_count"))
    blocker_index = payload.get("monthly_blocker_command_index")
    if monthly_gap_count:
        if not isinstance(blocker_index, list) or not blocker_index:
            issues.append(issue("monthly_run_blocker_command_index_missing", f"monthly_completion_gap_count={monthly_gap_count}"))
        else:
            for index, blocker in enumerate(blocker_index):
                if not isinstance(blocker, dict):
                    issues.append(issue("monthly_run_blocker_command_index_invalid_item", f"index={index}"))
                    continue
                name = str(blocker.get("name") or f"index={index}").strip()
                missing_fields = [
                    field
                    for field in ("name", "action", "preflight_status")
                    if not str(blocker.get(field) or "").strip()
                ]
                if not isinstance(blocker.get("ready_to_run"), bool):
                    missing_fields.append("ready_to_run")
                if not isinstance(blocker.get("safe_to_run_automatically"), bool):
                    missing_fields.append("safe_to_run_automatically")
                if missing_fields:
                    issues.append(issue("monthly_run_blocker_command_missing_fields", f"{name}:{','.join(missing_fields)}"))
                if not str(blocker.get("command") or "").strip():
                    issues.append(issue("monthly_run_blocker_command_missing", name))
                artifacts = blocker.get("artifacts")
                if artifacts is not None and not isinstance(artifacts, dict):
                    issues.append(issue("monthly_run_blocker_command_artifacts_invalid", name))
    return issues


def validate_monthly_close_status(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "run_month",
                "generated_at",
                "close_status_generated_at",
                "source_report_generated_at",
                "close_status_write_status",
            },
        )
    )
    if payload.get("close_status_write_status") != "written":
        issues.append(issue("monthly_close_status_not_written", str(payload.get("close_status_write_status"))))
    generated_at = str(payload.get("generated_at") or "").strip()
    close_generated_at = str(payload.get("close_status_generated_at") or "").strip()
    if generated_at and close_generated_at and generated_at != close_generated_at:
        issues.append(
            issue(
                "monthly_close_status_generated_at_mismatch",
                f"generated_at={generated_at},close_status_generated_at={close_generated_at}",
            )
        )
    if not valid_run_month(payload.get("run_month")):
        issues.append(issue("invalid_run_month", str(payload.get("run_month") or "missing")))
    return issues


def validate_monthly_readiness(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed"}))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "run_month",
                "owner_email_allowed",
                "blocker_count",
                "actionable_summary",
                "primary_blocker",
                "next_action",
                "hold",
                "monthly_skip_policy",
                "monthly_comms_gates",
                "monthly_apply_publish_gates",
                "owner_email_send_guard_ok",
                "owner_email_send_guard_active_property_proof_ok",
                "owner_email_send_guard_max_once_monthly_ok",
                "owner_email_send_guard_no_spam_guard_ok",
                "owner_email_send_guard_send_allowed",
            },
        )
    )
    if not isinstance(payload.get("owner_email_allowed"), bool):
        issues.append(issue("monthly_readiness_invalid_owner_email_allowed", "not_bool"))
    actionable = payload.get("actionable_summary")
    if not isinstance(actionable, dict):
        issues.append(issue("monthly_readiness_invalid_actionable_summary", "not_object"))
        actionable = {}
    primary = payload.get("primary_blocker")
    if payload.get("status") == "review" and not isinstance(primary, dict):
        issues.append(issue("monthly_readiness_missing_primary_blocker", "review_requires_primary_blocker"))
    actionable_primary = actionable.get("primary_blocker")
    if payload.get("status") == "review" and not isinstance(actionable_primary, dict):
        issues.append(issue("monthly_readiness_missing_actionable_primary_blocker", "review_requires_actionable_summary.primary_blocker"))
    if isinstance(actionable_primary, dict) and primary != actionable_primary:
        issues.append(issue("monthly_readiness_primary_blocker_alias_mismatch", "primary_blocker must match actionable_summary.primary_blocker"))
    if isinstance(primary, dict):
        missing_top_level_fields = [
            key
            for key in ("id", "class", "summary", "blocker", "artifact", "next_action", "hold")
            if key not in primary
        ]
        if missing_top_level_fields:
            issues.append(issue("monthly_readiness_primary_blocker_missing_fields", ",".join(missing_top_level_fields)))
    expected_primary = actionable_primary if isinstance(actionable_primary, dict) else primary
    if isinstance(expected_primary, dict):
        missing_blocker_fields = [
            key
            for key in ("id", "class", "summary", "blocker", "artifact", "next_action", "hold")
            if key not in expected_primary
        ]
        if missing_blocker_fields:
            issues.append(issue("monthly_readiness_primary_blocker_missing_fields", ",".join(missing_blocker_fields)))
        if payload.get("next_action") != expected_primary.get("next_action"):
            issues.append(issue("monthly_readiness_next_action_alias_mismatch", "next_action must match primary_blocker.next_action"))
        if payload.get("hold") != expected_primary.get("hold"):
            issues.append(issue("monthly_readiness_hold_alias_mismatch", "hold must match primary_blocker.hold"))
    skip_policy = payload.get("monthly_skip_policy")
    if not isinstance(skip_policy, dict):
        issues.append(issue("monthly_readiness_invalid_skip_policy", "not_object"))
        skip_policy = {}
    comms_gates = payload.get("monthly_comms_gates")
    if not isinstance(comms_gates, dict):
        issues.append(issue("monthly_readiness_invalid_comms_gates", "not_object"))
        comms_gates = {}
    apply_publish_gates = payload.get("monthly_apply_publish_gates")
    if not isinstance(apply_publish_gates, dict):
        issues.append(issue("monthly_readiness_invalid_apply_publish_gates", "not_object"))
        apply_publish_gates = {}
    for key in ("total_exclusion_counts_match", "lofty_pm_publish_exclusion_counts_match"):
        if key in skip_policy and skip_policy.get(key) is not True:
            issues.append(issue("monthly_readiness_skip_policy_not_ok", key))
    if compact_count(skip_policy.get("lofty_pm_publish_excluded_payload_file_count")):
        issues.append(issue("monthly_readiness_excluded_payload_file_count_nonzero", str(skip_policy.get("lofty_pm_publish_excluded_payload_file_count"))))
    if compact_count(skip_policy.get("lofty_pm_publish_excluded_owner_email_candidate_count")):
        issues.append(
            issue(
                "monthly_readiness_excluded_owner_email_candidate_count_nonzero",
                str(skip_policy.get("lofty_pm_publish_excluded_owner_email_candidate_count")),
            )
        )
    if payload.get("owner_email_allowed") is True:
        if payload.get("status") != "ok":
            issues.append(issue("monthly_readiness_owner_email_allowed_status_not_ok", str(payload.get("status") or "missing")))
        if compact_count(payload.get("blocker_count")):
            issues.append(issue("monthly_readiness_owner_email_allowed_with_blockers", str(payload.get("blocker_count"))))
        if compact_count(actionable.get("actionable_blocker_count")):
            issues.append(issue("monthly_readiness_owner_email_allowed_with_actionable_blockers", str(actionable.get("actionable_blocker_count"))))
        for key, code in (
            ("owner_email_send_guard_ok", "monthly_readiness_owner_email_guard_not_ok"),
            ("owner_email_send_guard_active_property_proof_ok", "monthly_readiness_owner_email_active_property_proof_not_ok"),
            ("owner_email_send_guard_max_once_monthly_ok", "monthly_readiness_owner_email_max_once_not_ok"),
            ("owner_email_send_guard_no_spam_guard_ok", "monthly_readiness_owner_email_no_spam_not_ok"),
        ):
            if payload.get(key) is not True:
                issues.append(issue(code, str(payload.get(key))))
        for key, code in (
            ("owner_email_send_guard_ok", "monthly_readiness_comms_owner_email_guard_not_ok"),
            ("owner_email_send_guard_active_property_proof_ok", "monthly_readiness_comms_active_property_proof_not_ok"),
            ("owner_email_send_guard_max_once_monthly_ok", "monthly_readiness_comms_max_once_not_ok"),
            ("owner_email_send_guard_no_spam_guard_ok", "monthly_readiness_comms_no_spam_not_ok"),
        ):
            if key in comms_gates and comms_gates.get(key) is not True:
                issues.append(issue(code, str(comms_gates.get(key))))
        if payload.get("owner_email_send_guard_send_allowed") is True:
            for key, code in (
                ("owner_email_send_guard_send_allowed", "monthly_readiness_owner_email_send_not_allowed_by_guard"),
            ):
                if payload.get(key) is not True:
                    issues.append(issue(code, str(payload.get(key))))
            for key, code in (
                ("owner_email_send_guard_send_allowed", "monthly_readiness_comms_send_not_allowed"),
            ):
                if key in comms_gates and comms_gates.get(key) is not True:
                    issues.append(issue(code, str(comms_gates.get(key))))
        hemlane_source_current = bool(
            comms_gates.get("rent_roll_source_freshness_status") == "current"
            and comms_gates.get("rent_roll_source_owner_email_allowed") is True
            and comms_gates.get("hemlane_cdp_capture_status") == "ok"
        )
        for key in ("lofty_cdp_preflight_status", "hemlane_cdp_preflight_status"):
            if key not in comms_gates or comms_gates.get(key) == "ok":
                continue
            if key == "hemlane_cdp_preflight_status" and hemlane_source_current:
                continue
            issues.append(issue("monthly_readiness_cdp_gate_not_ok", f"{key}={comms_gates.get(key)}"))
        for key in ("guarded_apply_status", "lofty_pm_publish_status", "lofty_pm_publish_guarded_apply_status"):
            if key in apply_publish_gates and apply_publish_gates.get(key) != "ok":
                issues.append(issue("monthly_readiness_apply_publish_gate_not_ok", f"{key}={apply_publish_gates.get(key)}"))
        for key in ("guarded_apply_fresh", "lofty_pm_publish_fresh"):
            if key in apply_publish_gates and apply_publish_gates.get(key) is not True:
                issues.append(issue("monthly_readiness_apply_publish_not_fresh", key))
    return issues


def validate_no_mortgage_financials(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed"}))
    issues.extend(require_keys(payload, {"status", "states", "file_count", "remaining_nonzero_count", "records"}))
    states = payload.get("states")
    if not isinstance(states, list):
        issues.append(issue("invalid_states", "not_list"))
        states = []
    missing_states = sorted({"IL", "OH", "TN"} - {str(state).upper() for state in states})
    if missing_states:
        issues.append(issue("missing_no_mortgage_states", ",".join(missing_states)))
    if compact_count(payload.get("file_count")) <= 0:
        issues.append(issue("invalid_file_count", str(payload.get("file_count") or 0)))
    if not isinstance(payload.get("records"), list):
        issues.append(issue("invalid_records", "not_list"))
    remaining_nonzero_count = compact_count(payload.get("remaining_nonzero_count"))
    if remaining_nonzero_count:
        issues.append(issue("remaining_mortgage_rows_nonzero", str(remaining_nonzero_count)))
    read_error_count = compact_count(payload.get("read_error_count"))
    if read_error_count:
        issues.append(issue("no_mortgage_financials_read_error", str(read_error_count)))
    return issues


def validate_lofty_cdp_preflight(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed"}))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "pm_tab_count",
                "login_tab_count",
                "login_recovery_performed",
                "login_recovery_try_count",
                "manual_auth_required",
            },
        )
    )
    pm_tab_count = compact_count(payload.get("pm_tab_count"))
    login_tab_count = compact_count(payload.get("login_tab_count"))
    recovery_try_count = compact_count(payload.get("login_recovery_try_count"))
    recovery_performed = payload.get("login_recovery_performed")
    if payload.get("status") == "ok" and pm_tab_count <= 0:
        issues.append(issue("lofty_cdp_preflight_ok_without_pm_tab", str(pm_tab_count)))
    if login_tab_count > 0 and pm_tab_count == 0:
        if recovery_performed is not True:
            issues.append(issue("lofty_cdp_preflight_login_without_recovery", f"login_tabs={login_tab_count},pm_tabs={pm_tab_count}"))
        if recovery_try_count <= 0:
            issues.append(issue("lofty_cdp_preflight_recovery_try_count_missing", str(payload.get("login_recovery_try_count"))))
        if payload.get("manual_auth_required") is not True:
            issues.append(issue("lofty_cdp_preflight_login_without_manual_auth_flag", str(payload.get("manual_auth_required"))))
        if "login_recovery_hard_refresh_attempted" in payload and payload.get("login_recovery_hard_refresh_attempted") is not True:
            issues.append(issue("lofty_cdp_preflight_recovery_without_hard_refresh", str(payload.get("login_recovery_hard_refresh_attempted"))))
        if "login_recovery_exhausted" in payload and payload.get("login_recovery_exhausted") is not True:
            issues.append(issue("lofty_cdp_preflight_recovery_not_exhausted", str(payload.get("login_recovery_exhausted"))))
    if recovery_performed is True and recovery_try_count <= 0:
        issues.append(issue("lofty_cdp_preflight_recovery_performed_without_try_count", str(payload.get("login_recovery_try_count"))))
    return issues


def validate_weekly_file_updates(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed", "skipped_not_friday", "already_done_for_week"}))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "return_code",
                "deterministic_verification_idempotent",
                "weekly_unprocessed_idempotent",
                "weekly_unprocessed_state_idempotent",
                "review_safe_idempotency",
                "cf_statement_sync_status",
                "cf_statement_sync_return_code",
                "cf_statement_sync_source_cash_balance_violation_count",
                "cf_statement_sync_no_mortgage_debt_violation_count",
                "cf_statement_sync_conflict_count",
                "cf_statement_sync_missing_canonical_cf_count",
                "cf_review_gate_status",
                "cf_review_gate_action_queue_count",
                "cf_review_gate_blocker_count",
                "future_cf_values_status",
                "future_cf_values_changed_cell_count",
                "future_cf_values_unreadable_count",
            },
        )
    )
    review_safe = payload.get("review_safe_idempotency")
    if not isinstance(review_safe, dict):
        issues.append(issue("invalid_review_safe_idempotency", "not_object"))
        review_safe = {}
    if payload.get("status") in {"ok", "skipped_not_friday", "already_done_for_week"} and payload.get("deterministic_verification_idempotent") is not True:
        issues.append(issue("weekly_deterministic_verification_not_idempotent", str(payload.get("deterministic_verification_idempotent"))))
    if "deterministic_verification_idempotent" in review_safe and review_safe.get("deterministic_verification_idempotent") != payload.get("deterministic_verification_idempotent"):
        issues.append(issue("weekly_deterministic_verification_mismatch", "review_safe_idempotency"))
    deterministic_skip_or_done = (
        payload.get("status") in {"skipped_not_friday", "already_done_for_week"}
        and payload.get("deterministic_verification_idempotent") is True
        and int(payload.get("return_code") or 0) == 0
    )
    for key in ("weekly_unprocessed_idempotent", "weekly_unprocessed_state_idempotent"):
        if payload.get(key) is not True and not deterministic_skip_or_done:
            issues.append(issue("weekly_idempotency_not_true", key))
    if compact_count(payload.get("cf_statement_sync_source_cash_balance_violation_count")):
        issues.append(issue("weekly_source_cash_balance_violation_count_nonzero", str(payload.get("cf_statement_sync_source_cash_balance_violation_count"))))
    if compact_count(payload.get("cf_statement_sync_no_mortgage_debt_violation_count")):
        issues.append(issue("weekly_no_mortgage_debt_violation_count_nonzero", str(payload.get("cf_statement_sync_no_mortgage_debt_violation_count"))))
    if compact_count(payload.get("cf_statement_sync_conflict_count")):
        issues.append(issue("weekly_cf_statement_sync_conflict_count_nonzero", str(payload.get("cf_statement_sync_conflict_count"))))
    if compact_count(payload.get("cf_statement_sync_missing_canonical_cf_count")):
        issues.append(issue("weekly_cf_statement_sync_missing_canonical_count_nonzero", str(payload.get("cf_statement_sync_missing_canonical_cf_count"))))
    if compact_count(payload.get("cf_statement_sync_return_code")):
        issues.append(issue("weekly_cf_statement_sync_return_code_nonzero", str(payload.get("cf_statement_sync_return_code"))))
    if compact_count(payload.get("cf_review_gate_action_queue_count")):
        issues.append(issue("weekly_cf_review_gate_action_queue_nonzero", str(payload.get("cf_review_gate_action_queue_count"))))
    if compact_count(payload.get("cf_review_gate_blocker_count")):
        issues.append(issue("weekly_cf_review_gate_blocker_count_nonzero", str(payload.get("cf_review_gate_blocker_count"))))
    if payload.get("future_cf_values_status") != "ok":
        issues.append(issue("weekly_future_cf_values_status_not_ok", str(payload.get("future_cf_values_status") or "missing")))
    if compact_count(payload.get("future_cf_values_changed_cell_count")):
        issues.append(issue("weekly_future_cf_values_changed_cell_count_nonzero", str(payload.get("future_cf_values_changed_cell_count"))))
    if compact_count(payload.get("future_cf_values_unreadable_count")):
        issues.append(issue("weekly_future_cf_values_unreadable_count_nonzero", str(payload.get("future_cf_values_unreadable_count"))))
    if "future_cf_values_apply_status" in payload and payload.get("future_cf_values_apply_status") != "ok":
        issues.append(issue("weekly_future_cf_values_apply_status_not_ok", str(payload.get("future_cf_values_apply_status") or "missing")))
    if compact_count(payload.get("future_cf_values_apply_changed_cell_count")):
        issues.append(issue("weekly_future_cf_values_apply_changed_cell_count_nonzero", str(payload.get("future_cf_values_apply_changed_cell_count"))))
    if compact_count(payload.get("future_cf_values_apply_unreadable_count")):
        issues.append(issue("weekly_future_cf_values_apply_unreadable_count_nonzero", str(payload.get("future_cf_values_apply_unreadable_count"))))
    if payload.get("status") == "review" and review_safe.get("retry_required") is True and review_safe.get("state_file_unmarked") is not True:
        issues.append(issue("weekly_review_state_file_not_unmarked", str(review_safe.get("state_file_unmarked"))))
    if review_safe.get("ecogl_source_fix_effectively_clear") is False:
        issues.append(issue("weekly_source_fix_not_clear", "review_safe_idempotency.ecogl_source_fix_effectively_clear=false"))
    return issues


def validate_future_cf_statement_values(payload: dict, *, expected_mode: str = "audit") -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review"}))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "generated_at",
                "mode",
                "policy",
                "real_estate_root",
                "as_of_date",
                "year",
                "start_month",
                "include_archive",
                "include_conflicts",
                "cf_file_count",
                "changed_workbook_count",
                "changed_cell_count",
                "unreadable_count",
                "issue_count",
                "issues",
            },
        )
    )
    age_hours = iso_age_hours(payload.get("generated_at"))
    if age_hours is None or age_hours < -1 or age_hours > FUTURE_CF_VALUES_MAX_AGE_HOURS:
        issues.append(issue("future_cf_values_report_stale", str(age_hours)))
    try:
        as_of_date = date.fromisoformat(str(payload.get("as_of_date") or ""))
    except ValueError:
        as_of_date = None
        issues.append(issue("future_cf_values_invalid_as_of_date", str(payload.get("as_of_date") or "missing")))
    if as_of_date is not None:
        day_delta = (date.today() - as_of_date).days
        if day_delta < -1 or day_delta > 1:
            issues.append(issue("future_cf_values_as_of_date_stale", str(day_delta)))
    if payload.get("year") != 2026:
        issues.append(issue("future_cf_values_wrong_year", str(payload.get("year") or "missing")))
    if payload.get("include_archive") is not True:
        issues.append(issue("future_cf_values_archive_scope_missing", str(payload.get("include_archive"))))
    if payload.get("include_conflicts") is not True:
        issues.append(issue("future_cf_values_conflict_scope_missing", str(payload.get("include_conflicts"))))
    current_month = date.today().month
    if compact_count(payload.get("start_month")) <= 0 or compact_count(payload.get("start_month")) > current_month:
        issues.append(issue("future_cf_values_start_month_too_narrow", str(payload.get("start_month") or "missing")))
    policy_text = str(payload.get("policy") or "")
    if "Revenue" not in policy_text or "Operating Expenses" not in policy_text:
        issues.append(issue("future_cf_values_policy_missing_scope", policy_text[:120] or "missing"))
    root_text = str(payload.get("real_estate_root") or "")
    if "Dropbox" not in root_text or "Real Estate" not in root_text:
        issues.append(issue("future_cf_values_non_dropbox_real_estate_root", root_text or "missing"))
    if compact_count(payload.get("issue_count")):
        issues.append(issue("future_cf_values_issue_count_nonzero", str(payload.get("issue_count"))))
    if payload.get("issues") not in ([], None):
        issues.append(issue("future_cf_values_issues_not_empty", str(payload.get("issues"))[:200]))
    if payload.get("mode") != expected_mode:
        issues.append(issue("future_cf_values_report_wrong_mode", str(payload.get("mode") or "missing")))
    apply_changes_are_successful = (
        expected_mode == "apply"
        and payload.get("status") == "ok"
        and compact_count(payload.get("issue_count")) == 0
        and payload.get("issues") in ([], None)
    )
    if compact_count(payload.get("changed_cell_count")) and not apply_changes_are_successful:
        issues.append(issue("future_cf_values_changed_cell_count_nonzero", str(payload.get("changed_cell_count"))))
    if compact_count(payload.get("changed_workbook_count")) and not apply_changes_are_successful:
        issues.append(issue("future_cf_values_changed_workbook_count_nonzero", str(payload.get("changed_workbook_count"))))
    if payload.get("changed_workbooks_bounded") not in ([], None) and not apply_changes_are_successful:
        issues.append(issue("future_cf_values_changed_workbooks_not_empty", str(len(payload.get("changed_workbooks_bounded") or []))))
    if compact_count(payload.get("unreadable_count")):
        issues.append(issue("future_cf_values_unreadable_count_nonzero", str(payload.get("unreadable_count"))))
    if compact_count(payload.get("cf_file_count")) <= 0:
        issues.append(issue("future_cf_values_invalid_cf_file_count", str(payload.get("cf_file_count") or 0)))
    return issues


def validate_future_cf_statement_values_apply(payload: dict) -> list[dict]:
    return validate_future_cf_statement_values(payload, expected_mode="apply")


def validate_weekly_cf_sync(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed"}))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "source_cash_balance_policy",
                "source_cash_balance_violation_count",
                "no_mortgage_debt_policy",
                "no_mortgage_debt_violation_count",
                "conflict_count",
                "untagged_review_required_count",
                "canonical_cf_property_count",
                "missing_canonical_cf_count",
            },
        )
    )
    if compact_count(payload.get("source_cash_balance_violation_count")):
        issues.append(issue("weekly_cf_source_cash_balance_violation_count_nonzero", str(payload.get("source_cash_balance_violation_count"))))
    if compact_count(payload.get("no_mortgage_debt_violation_count")):
        issues.append(issue("weekly_cf_no_mortgage_debt_violation_count_nonzero", str(payload.get("no_mortgage_debt_violation_count"))))
    if compact_count(payload.get("conflict_count")):
        issues.append(issue("weekly_cf_conflict_count_nonzero", str(payload.get("conflict_count"))))
    if compact_count(payload.get("missing_canonical_cf_count")):
        issues.append(issue("weekly_cf_missing_canonical_count_nonzero", str(payload.get("missing_canonical_cf_count"))))
    if compact_count(payload.get("canonical_cf_property_count")) <= 0:
        issues.append(issue("weekly_cf_invalid_canonical_property_count", str(payload.get("canonical_cf_property_count") or 0)))
    return issues


def validate_monthly_statements_idempotent(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed", "error"}))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "job",
                "generated_at",
                "stamp",
                "target_year",
                "target_month",
                "state_file",
                "action",
                "reason",
                "monthly_script_return_code",
                "operator_report",
                "operator_status",
                "operator_ok_state",
                "operator_issue_count",
                "captured_unique_count",
                "min_captured_required",
                "download_report",
                "download_ok",
                "download_total_buttons",
                "download_clicked_buttons",
                "download_new_files_count",
            },
        )
    )
    if payload.get("job") != "baselane-monthly-statements-idempotent":
        issues.append(issue("invalid_monthly_statements_job", str(payload.get("job") or "missing")))
    target_year = compact_count(payload.get("target_year"))
    target_month = compact_count(payload.get("target_month"))
    if target_year < 2020 or target_month < 1 or target_month > 12:
        issues.append(issue("invalid_monthly_statement_target", f"{target_year}-{target_month}"))
    stamp = str(payload.get("stamp") or "")
    if target_year and target_month and f"{target_year}-{target_month}" not in {stamp, stamp.replace("-0", "-")}:
        issues.append(issue("monthly_statement_stamp_target_mismatch", f"{stamp}!={target_year}-{target_month}"))
    if not str(payload.get("state_file") or "").strip():
        issues.append(issue("monthly_statement_state_file_missing", "state_file"))
    allowed_actions = {
        "refresh",
        "skip",
        "stamp",
        "mortgage-review",
        "run-statements",
        "retry-next-run",
        "auth-baselane",
        "wait-for-statements",
        "free-disk",
        "review-aligned-owner-import",
        "aligned-owner-import",
        "external-verified",
    }
    action = str(payload.get("action") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    if action not in allowed_actions:
        issues.append(issue("monthly_statement_invalid_action", action or "missing"))
    if payload.get("status") == "ok":
        expected_reason_by_action = {
            "refresh": "existing-capture-verified-gate-refresh",
            "skip": "already-captured-and-verified",
            "stamp": "captured-and-verified",
            "mortgage-review": "statements-verified-mortgage-review",
            "aligned-owner-import": "statements-verified-aligned-owner-import",
            "external-verified": "assumed-statements-verified",
        }
        if action not in expected_reason_by_action:
            issues.append(issue("monthly_statement_ok_unexpected_action", action or "missing"))
        elif reason != expected_reason_by_action[action]:
            issues.append(issue("monthly_statement_action_reason_mismatch", f"{action}:{reason}"))
        captured = compact_count(payload.get("captured_unique_count"))
        minimum = compact_count(payload.get("min_captured_required"))
        if minimum <= 0:
            issues.append(issue("monthly_statement_invalid_min_captured_required", str(minimum)))
        if captured < minimum:
            issues.append(issue("monthly_statement_captured_below_minimum", f"{captured}<{minimum}"))
        total_buttons = compact_count(payload.get("download_total_buttons"))
        clicked_buttons = compact_count(payload.get("download_clicked_buttons"))
        new_files = compact_count(payload.get("download_new_files_count"))
        if total_buttons <= 0:
            issues.append(issue("monthly_statement_download_total_buttons_not_positive", str(payload.get("download_total_buttons"))))
        if clicked_buttons <= 0:
            issues.append(issue("monthly_statement_download_clicked_buttons_not_positive", str(payload.get("download_clicked_buttons"))))
        if clicked_buttons > total_buttons:
            issues.append(issue("monthly_statement_download_clicked_exceeds_total", f"{clicked_buttons}>{total_buttons}"))
        if new_files < 0:
            issues.append(issue("monthly_statement_download_new_files_negative", str(payload.get("download_new_files_count"))))
        if payload.get("operator_ok_state") is not True:
            issues.append(issue("monthly_statement_operator_not_ok", str(payload.get("operator_ok_state"))))
        if compact_count(payload.get("operator_issue_count")):
            issues.append(issue("monthly_statement_operator_issue_count_nonzero", str(payload.get("operator_issue_count"))))
        if payload.get("download_ok") is not True:
            issues.append(issue("monthly_statement_download_not_ok", str(payload.get("download_ok"))))
        if compact_count(payload.get("monthly_script_return_code")):
            issues.append(issue("monthly_statement_script_return_code_nonzero", str(payload.get("monthly_script_return_code"))))
        download_error_class = str(payload.get("download_error_class") or "").strip()
        if download_error_class:
            issues.append(issue("monthly_statement_download_error_class", download_error_class))
    return issues


def validate_owner_email_guard(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed"}))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "guard_ok",
                "send_allowed",
                "safe_block",
                "no_spam_guard_ok",
                "issue_count",
                "run_month",
                "max_once_monthly_ok",
                "idempotency_configured",
                "effective_send_owner_emails",
                "active_property_guard_proof",
                "manual_exclusions_ok",
                "yhome_transition_guard_ok",
                "yhome_transition_guard_column_b_rule_ok",
                "active_property_policy_mentions_yhome",
                "active_property_policy_mentions_manual_exclusions",
                "excluded_payload_file_count",
                "excluded_owner_email_candidate_count",
                "send_decision_digest",
                "send_decision_inputs_present",
                "send_decision_inputs_digest",
                "send_decision_digest_matches_inputs",
                "guild_test_post_required_before_email",
                "guild_test_post_required_lofty_guild_id",
                "guild_test_post_lofty_guild_ok",
                "guild_test_post_valid",
                "guild_test_post_route_proof_ok",
                "guild_test_post_posted",
                "guild_test_post_posted_at_month_matches",
                "send_decision_digest_ok",
                "send_evidence_matches_intent",
                "send_lock_file_resolved",
                "send_lock_status",
                "send_lock_safe",
                "send_lock_ok_for_guard",
                "existing_send_lock_digest_safe",
                "sent_state_file_resolved",
                "already_sent_state_ok",
                "send_interval_days",
                "send_interval_ok",
                "owner_email_will_send_count",
                "owner_email_send_evidence_count",
                "owner_email_send_evidence_issue_count",
                "owner_email_packet_required",
                "owner_email_packet_loaded",
                "owner_email_packet_status",
                "owner_email_packet_issue_count",
                "owner_email_packet_run_month_matches",
                "owner_email_packet_full_history_leak_count",
                "owner_email_packet_body_guard_issue_count",
                "owner_email_packet_unsafe_preview_packet_count",
                "owner_email_packet_safe_to_send_now",
                "owner_email_packet_ok_for_send",
            },
        )
    )
    for key in (
        "guard_ok",
        "send_allowed",
        "safe_block",
        "no_spam_guard_ok",
        "max_once_monthly_ok",
        "idempotency_configured",
        "effective_send_owner_emails",
        "manual_exclusions_ok",
        "yhome_transition_guard_ok",
        "yhome_transition_guard_column_b_rule_ok",
        "active_property_policy_mentions_yhome",
        "active_property_policy_mentions_manual_exclusions",
        "guild_test_post_required_before_email",
        "guild_test_post_lofty_guild_ok",
        "guild_test_post_valid",
        "guild_test_post_route_proof_ok",
        "guild_test_post_posted",
        "guild_test_post_posted_at_month_matches",
        "send_decision_digest_ok",
        "send_evidence_matches_intent",
        "send_lock_safe",
        "send_lock_ok_for_guard",
        "existing_send_lock_digest_safe",
        "already_sent_state_ok",
        "send_interval_ok",
        "send_decision_inputs_present",
        "send_decision_digest_matches_inputs",
        "owner_email_packet_required",
        "owner_email_packet_loaded",
        "owner_email_packet_run_month_matches",
        "owner_email_packet_safe_to_send_now",
        "owner_email_packet_ok_for_send",
    ):
        if key in payload and not isinstance(payload.get(key), bool):
            issues.append(issue("invalid_boolean", key))
    send_digest = str(payload.get("send_decision_digest") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", send_digest):
        issues.append(issue("owner_email_send_decision_digest_invalid", send_digest or "missing"))
    send_inputs_digest = str(payload.get("send_decision_inputs_digest") or "").strip()
    if payload.get("send_decision_inputs_present") is True:
        if not re.fullmatch(r"[0-9a-f]{64}", send_inputs_digest):
            issues.append(issue("owner_email_send_decision_inputs_digest_invalid", send_inputs_digest or "missing"))
        if payload.get("send_decision_digest_matches_inputs") is not True:
            issues.append(issue("owner_email_send_decision_digest_mismatch_inputs", str(payload.get("send_decision_digest_matches_inputs"))))
    elif send_inputs_digest:
        issues.append(issue("owner_email_send_decision_inputs_digest_without_inputs", send_inputs_digest))
    send_lock_file = str(payload.get("send_lock_file_resolved") or "")
    if send_lock_file and Path(send_lock_file).name != "owner_email_sent_month.in-progress.json":
        issues.append(issue("owner_email_send_lock_file_unexpected", send_lock_file))
    sent_state_file = str(payload.get("sent_state_file_resolved") or "")
    if sent_state_file and Path(sent_state_file).name != "owner_email_sent_month":
        issues.append(issue("owner_email_sent_state_file_unexpected", sent_state_file))
    if compact_count(payload.get("issue_count")) < 0:
        issues.append(issue("invalid_issue_count", str(payload.get("issue_count"))))
    if payload.get("status") == "ok":
        if payload.get("guard_ok") is not True:
            issues.append(issue("owner_email_guard_ok_not_true", str(payload.get("guard_ok"))))
        if payload.get("no_spam_guard_ok") is not True:
            issues.append(issue("owner_email_no_spam_guard_not_true", str(payload.get("no_spam_guard_ok"))))
        if payload.get("max_once_monthly_ok") is not True:
            issues.append(issue("owner_email_max_once_monthly_not_true", str(payload.get("max_once_monthly_ok"))))
        if payload.get("idempotency_configured") is not True:
            issues.append(issue("owner_email_idempotency_not_configured", str(payload.get("idempotency_configured"))))
        if payload.get("send_decision_digest_ok") is not True:
            issues.append(issue("owner_email_send_decision_digest_not_ok", str(payload.get("send_decision_digest_ok"))))
        if payload.get("send_decision_inputs_present") is True and payload.get("send_decision_digest_matches_inputs") is not True:
            issues.append(issue("owner_email_send_decision_digest_not_recomputed", str(payload.get("send_decision_digest_matches_inputs"))))
        if payload.get("send_lock_safe") is not True:
            issues.append(issue("owner_email_send_lock_not_safe", str(payload.get("send_lock_safe"))))
        if payload.get("send_lock_ok_for_guard") is not True:
            issues.append(issue("owner_email_send_lock_not_ok_for_guard", str(payload.get("send_lock_ok_for_guard"))))
        if payload.get("existing_send_lock_digest_safe") is not True:
            issues.append(issue("owner_email_existing_send_lock_digest_not_safe", str(payload.get("existing_send_lock_digest_safe"))))
        if payload.get("already_sent_state_ok") is not True:
            issues.append(issue("owner_email_already_sent_state_not_ok", str(payload.get("already_sent_state_ok"))))
        owner_email_packet_cooldown_days = compact_count(payload.get("owner_email_packet_property_cooldown_days"))
        owner_email_packet_cooldown_issue_count = compact_count(payload.get("owner_email_packet_property_cooldown_issue_count"))
        interval_is_safe_blocked_property_cooldown = (
            payload.get("send_allowed") is False
            and payload.get("effective_send_owner_emails") is False
            and payload.get("max_once_monthly_ok") is True
            and payload.get("no_spam_guard_ok") is True
            and payload.get("owner_email_packet_property_cooldown_ok") is True
            and owner_email_packet_cooldown_issue_count == 0
            and owner_email_packet_cooldown_days >= 7
        )
        if payload.get("send_interval_ok") is not True and not interval_is_safe_blocked_property_cooldown:
            issues.append(issue("owner_email_send_interval_not_ok", str(payload.get("send_interval_ok"))))
    active_proof = payload.get("active_property_guard_proof")
    if not isinstance(active_proof, dict):
        issues.append(issue("invalid_owner_email_active_property_guard_proof", "not_object"))
        active_proof = {}
    for key, code in (
        ("manual_exclusions_ok", "owner_email_manual_exclusions_not_ok"),
        ("yhome_transition_guard_ok", "owner_email_yhome_transition_guard_not_ok"),
        ("yhome_transition_guard_column_b_rule_ok", "owner_email_yhome_column_b_rule_not_ok"),
        ("active_property_policy_mentions_yhome", "owner_email_policy_missing_yhome"),
        ("active_property_policy_mentions_manual_exclusions", "owner_email_policy_missing_manual_exclusions"),
    ):
        if payload.get("status") == "ok" and payload.get(key) is not True:
            issues.append(issue(code, str(payload.get(key))))
    manual_missing = active_proof.get("manual_missing_property_names")
    if isinstance(manual_missing, list) and manual_missing:
        issues.append(issue("owner_email_manual_exclusion_missing", ",".join(str(item) for item in manual_missing)))
    if compact_count(payload.get("excluded_payload_file_count")):
        issues.append(issue("owner_email_excluded_payload_file_count_nonzero", str(payload.get("excluded_payload_file_count"))))
    if compact_count(payload.get("excluded_owner_email_candidate_count")):
        issues.append(issue("owner_email_excluded_candidate_count_nonzero", str(payload.get("excluded_owner_email_candidate_count"))))
    if compact_count(payload.get("owner_email_send_evidence_issue_count")):
        issues.append(issue("owner_email_send_evidence_issue_count_nonzero", str(payload.get("owner_email_send_evidence_issue_count"))))
    guild_snapshot = payload.get("guild_test_post_snapshot") if isinstance(payload.get("guild_test_post_snapshot"), dict) else {}
    guild_snapshot_status = str(guild_snapshot.get("status") or "").strip()
    has_configured_guild_snapshot = bool(guild_snapshot) and guild_snapshot_status not in {"missing", "not_configured"}
    if str(payload.get("guild_test_post_required_lofty_guild_id") or "") != REQUIRED_LOFTY_GUILD_ID:
        issues.append(
            issue(
                "owner_email_guild_test_required_lofty_guild_mismatch",
                str(payload.get("guild_test_post_required_lofty_guild_id") or "missing"),
            )
        )
    if payload.get("guild_test_post_required_before_email") is True:
        if not has_configured_guild_snapshot:
            issues.append(issue("owner_email_guild_test_snapshot_missing", guild_snapshot_status or "missing"))
    if payload.get("guild_test_post_required_before_email") is True and has_configured_guild_snapshot:
        if payload.get("guild_test_post_route_proof_ok") is not True:
            issues.append(issue("owner_email_guild_test_route_proof_not_ok", str(payload.get("guild_test_post_route_proof_ok"))))
        if not guild_test_route_proof_ok(guild_snapshot):
            issues.append(issue("owner_email_guild_test_route_proof_not_valid", str(guild_snapshot.get("target") or "missing")))
        if payload.get("guild_test_post_lofty_guild_ok") is not True:
            issues.append(issue("owner_email_guild_test_lofty_guild_not_ok", str(payload.get("guild_test_post_lofty_guild_ok"))))
        if not guild_test_lofty_guild_ok(guild_snapshot):
            issues.append(issue("owner_email_guild_test_lofty_guild_not_valid", str(guild_snapshot.get("target") or "missing")))
    if payload.get("guild_test_post_valid") is True:
        if payload.get("guild_test_post_required_before_email") is not True:
            issues.append(issue("owner_email_guild_test_valid_without_required_gate", str(payload.get("guild_test_post_required_before_email"))))
        if not has_configured_guild_snapshot or guild_snapshot.get("valid") is not True:
            issues.append(issue("owner_email_guild_test_valid_without_snapshot", str(guild_snapshot.get("valid") if guild_snapshot else "missing")))
        if payload.get("guild_test_post_posted") is not True:
            issues.append(issue("owner_email_guild_test_valid_without_posted_proof", str(payload.get("guild_test_post_posted"))))
        if payload.get("guild_test_post_posted_at_month_matches") is not True:
            issues.append(
                issue(
                    "owner_email_guild_test_valid_without_month_proof",
                    str(payload.get("guild_test_post_posted_at_month_matches")),
                )
            )
        if payload.get("guild_test_post_route_proof_ok") is not True:
            issues.append(issue("owner_email_guild_test_valid_without_route_proof", str(payload.get("guild_test_post_route_proof_ok"))))
        if payload.get("guild_test_post_lofty_guild_ok") is not True:
            issues.append(issue("owner_email_guild_test_valid_without_lofty_guild", str(payload.get("guild_test_post_lofty_guild_ok"))))
    if payload.get("send_allowed") is True or payload.get("effective_send_owner_emails") is True:
        if payload.get("guild_test_post_required_before_email") is not True:
            issues.append(issue("owner_email_guild_test_post_not_required", str(payload.get("guild_test_post_required_before_email"))))
        if payload.get("guild_test_post_valid") is not True:
            issues.append(issue("owner_email_guild_test_post_not_valid", str(payload.get("guild_test_post_valid"))))
        if payload.get("guild_test_post_route_proof_ok") is not True:
            issues.append(issue("owner_email_guild_test_route_proof_not_ok", str(payload.get("guild_test_post_route_proof_ok"))))
        if payload.get("guild_test_post_lofty_guild_ok") is not True:
            issues.append(issue("owner_email_guild_test_lofty_guild_not_ok", str(payload.get("guild_test_post_lofty_guild_ok"))))
        if payload.get("send_decision_digest_ok") is not True:
            issues.append(issue("owner_email_send_decision_digest_not_ok", str(payload.get("send_decision_digest_ok"))))
        if payload.get("send_evidence_matches_intent") is not True:
            issues.append(issue("owner_email_send_evidence_does_not_match_intent", str(payload.get("send_evidence_matches_intent"))))
        if payload.get("owner_email_packet_ok_for_send") is not True:
            issues.append(issue("owner_email_send_allowed_packet_not_ok", str(payload.get("owner_email_packet_ok_for_send"))))
        if payload.get("owner_email_packet_status") != "ok":
            issues.append(issue("owner_email_send_allowed_packet_status_not_ok", str(payload.get("owner_email_packet_status") or "missing")))
        if compact_count(payload.get("owner_email_packet_issue_count")):
            issues.append(issue("owner_email_send_allowed_packet_issue_count_nonzero", str(payload.get("owner_email_packet_issue_count"))))
        if payload.get("owner_email_packet_safe_to_send_now") is not True:
            issues.append(issue("owner_email_send_allowed_packet_not_safe", str(payload.get("owner_email_packet_safe_to_send_now"))))
        if payload.get("owner_email_packet_run_month_matches") is not True:
            issues.append(issue("owner_email_send_allowed_packet_month_mismatch", str(payload.get("owner_email_packet_run_month_matches"))))
        if compact_count(payload.get("owner_email_packet_full_history_leak_count")):
            issues.append(issue("owner_email_send_allowed_packet_full_history_leak", str(payload.get("owner_email_packet_full_history_leak_count"))))
        if compact_count(payload.get("owner_email_packet_body_guard_issue_count")):
            issues.append(issue("owner_email_send_allowed_packet_body_guard_issue", str(payload.get("owner_email_packet_body_guard_issue_count"))))
        if compact_count(payload.get("owner_email_packet_unsafe_preview_packet_count")):
            issues.append(issue("owner_email_send_allowed_packet_unsafe_preview", str(payload.get("owner_email_packet_unsafe_preview_packet_count"))))
        if compact_count(payload.get("owner_email_will_send_count")) != compact_count(payload.get("owner_email_send_evidence_count")):
            issues.append(
                issue(
                    "owner_email_send_evidence_count_mismatch",
                    f"{compact_count(payload.get('owner_email_send_evidence_count'))}!={compact_count(payload.get('owner_email_will_send_count'))}",
                )
            )
    return issues


def validate_lofty_pm_publish(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed", "ok_not_published", "ok_dry_run", "ok_not_applied"}))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "run_month",
                "active_property_only_policy",
                "excluded_property_count",
                "excluded_payload_file_count",
                "excluded_owner_email_candidate_count",
                "manual_excluded_property_names",
                "send_owner_emails",
                "effective_send_owner_emails",
                "owner_email_idempotency",
                "owner_email_send_decision",
                "listing_update_policy",
                "listing_update_guard_issue_count",
                "listing_update_full_history_count",
                "listing_update_non_history_count",
                "description_check_report",
                "description_check_status",
                "description_check_blocking_count",
                "description_check_policy",
                "records",
            },
        )
    )
    policy = str(payload.get("active_property_only_policy") or "")
    for required_phrase in ("Yhome", "manual", "owner-email"):
        if required_phrase not in policy:
            issues.append(issue("active_property_policy_missing_scope", required_phrase))
    manual_excluded = payload.get("manual_excluded_property_names")
    if not isinstance(manual_excluded, list):
        issues.append(issue("invalid_manual_excluded_property_names", "not_list"))
        manual_excluded = []
    manual_text = "\n".join(str(name) for name in manual_excluded)
    for property_name in ("3560 Saint Albans Rd", "1935 S Glen Rd"):
        if property_name not in manual_text:
            issues.append(issue("manual_exclusion_missing", property_name))
    excluded_payload_file_count = compact_count(payload.get("excluded_payload_file_count"))
    excluded_owner_email_candidate_count = compact_count(payload.get("excluded_owner_email_candidate_count"))
    if excluded_payload_file_count:
        issues.append(issue("excluded_payload_file_count_nonzero", str(excluded_payload_file_count)))
    if excluded_owner_email_candidate_count:
        issues.append(issue("excluded_owner_email_candidate_count_nonzero", str(excluded_owner_email_candidate_count)))
    listing_policy = str(payload.get("listing_update_policy") or "")
    for required_phrase in ("full UPDATES.md history", "owner emails"):
        if required_phrase not in listing_policy:
            issues.append(issue("listing_update_policy_missing_scope", required_phrase))
    if compact_count(payload.get("listing_update_guard_issue_count")):
        issues.append(issue("listing_update_guard_issue_count_nonzero", str(payload.get("listing_update_guard_issue_count"))))
    full_history_count = compact_count(payload.get("listing_update_full_history_count"))
    current_only_count = compact_count(payload.get("listing_update_non_history_count"))
    if current_only_count:
        issues.append(issue("listing_update_non_history_count_nonzero", f"current_only={current_only_count},full_history={full_history_count}"))
    description_policy = str(payload.get("description_check_policy") or "")
    for required_phrase in ("rent-roll", "Lofty-live", "owner email"):
        if required_phrase not in description_policy:
            issues.append(issue("description_check_policy_missing_scope", required_phrase))
    if payload.get("description_check_status") != "ok":
        issues.append(issue("description_check_not_ok", str(payload.get("description_check_status"))))
    if compact_count(payload.get("description_check_blocking_count")):
        issues.append(issue("description_check_blocking_count_nonzero", str(payload.get("description_check_blocking_count"))))
    if not isinstance(payload.get("send_owner_emails"), bool):
        issues.append(issue("invalid_send_owner_emails", "not_bool"))
    if not isinstance(payload.get("effective_send_owner_emails"), bool):
        issues.append(issue("invalid_effective_send_owner_emails", "not_bool"))
    idempotency = payload.get("owner_email_idempotency")
    send_decision = payload.get("owner_email_send_decision")
    if not isinstance(idempotency, dict):
        issues.append(issue("invalid_owner_email_idempotency", "not_object"))
        idempotency = {}
    if not isinstance(send_decision, dict):
        issues.append(issue("invalid_owner_email_send_decision", "not_object"))
        send_decision = {}
    if idempotency.get("configured") is not True:
        issues.append(issue("owner_email_idempotency_not_configured", str(idempotency.get("configured"))))
    if idempotency.get("max_send_per_month") is not True:
        issues.append(issue("owner_email_monthly_max_send_not_configured", str(idempotency.get("max_send_per_month"))))
    records = payload.get("records")
    if not isinstance(records, list):
        issues.append(issue("invalid_publish_records", "not_list"))
        records = []
    mapped_count = 0
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            issues.append(issue("invalid_publish_record", f"index={index}"))
            continue
        record_status = str(record.get("status") or "")
        if record_status == "mapped":
            mapped_count += 1
            updates_md = str(record.get("updates_md") or "")
            financials_md = str(record.get("financials_md") or "")
            if not is_canonical_updates_path(updates_md):
                issues.append(issue("publish_record_noncanonical_updates_path", str(record.get("property_name") or index)))
            if not is_canonical_financials_path(financials_md):
                issues.append(issue("publish_record_noncanonical_financials_path", str(record.get("property_name") or index)))
            if LEGACY_PUBLIC_FINANCIALS_PATH in financials_md or LEGACY_PUBLIC_UPDATES_PATH in updates_md:
                issues.append(issue("publish_record_legacy_public_path", str(record.get("property_name") or index)))
        if record_status == "excluded_no_live_update_or_email":
            forbidden_payload_keys = sorted(key for key in record if key.endswith("_payload_file"))
            if forbidden_payload_keys:
                issues.append(issue("excluded_record_has_payload_files", f"{record.get('property_name') or index}:{','.join(forbidden_payload_keys)}"))
    if records and compact_count(payload.get("property_count")) != mapped_count:
        issues.append(issue("publish_property_count_mismatch", f"{compact_count(payload.get('property_count'))}!={mapped_count}"))
    publish_results = payload.get("publish_results")
    if isinstance(publish_results, list):
        for index, result in enumerate(publish_results):
            if not isinstance(result, dict):
                issues.append(issue("invalid_publish_result", f"index={index}"))
                continue
            if result.get("listing_update_guard_ok") is not True:
                issues.append(issue("publish_result_listing_update_guard_not_ok", str(result.get("property_name") or index)))
            if result.get("listing_update_scope") != "full_history":
                issues.append(issue("publish_result_listing_update_scope_not_supported", str(result.get("property_name") or index)))
            if result.get("listing_update_scope") != "full_history" and compact_count(result.get("listing_update_char_count")) > 3500:
                issues.append(issue("publish_result_listing_update_oversized_chars", str(result.get("property_name") or index)))
            if result.get("listing_update_scope") != "full_history" and compact_count(result.get("listing_update_line_count")) > 80:
                issues.append(issue("publish_result_listing_update_oversized_lines", str(result.get("property_name") or index)))
    guild_required = payload.get("guild_test_post_required_before_email") is True
    guild_snapshot = payload.get("guild_test_post_snapshot") if isinstance(payload.get("guild_test_post_snapshot"), dict) else {}
    guild_snapshot_prepared = guild_snapshot.get("prepared") is True or guild_snapshot.get("valid") is True
    if guild_required and guild_snapshot_prepared and not guild_test_route_proof_ok(guild_snapshot):
        issues.append(issue("publish_guild_test_route_proof_not_valid", str(guild_snapshot.get("target") or "missing")))
    if guild_required and guild_snapshot_prepared and not guild_test_lofty_guild_ok(guild_snapshot):
        issues.append(issue("publish_guild_test_lofty_guild_not_valid", str(guild_snapshot.get("target") or "missing")))
    if payload.get("effective_send_owner_emails") is True:
        if not guild_required:
            issues.append(issue("owner_email_guild_test_post_not_required", str(payload.get("guild_test_post_required_before_email"))))
        if guild_snapshot.get("valid") is not True:
            issues.append(issue("owner_email_guild_test_post_not_valid", str(guild_snapshot.get("valid"))))
        if not guild_test_route_proof_ok(guild_snapshot):
            issues.append(issue("owner_email_guild_test_route_proof_not_valid", str(guild_snapshot.get("target") or "missing")))
        if not guild_test_lofty_guild_ok(guild_snapshot):
            issues.append(issue("owner_email_guild_test_lofty_guild_not_valid", str(guild_snapshot.get("target") or "missing")))
        digest = str(payload.get("send_decision_digest") or idempotency.get("send_decision_digest") or send_decision.get("send_decision_digest") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            issues.append(issue("invalid_owner_email_send_decision_digest", digest or "missing"))
        if compact_count(payload.get("owner_email_send_evidence_issue_count")):
            issues.append(issue("owner_email_send_evidence_issue_count_nonzero", str(payload.get("owner_email_send_evidence_issue_count"))))
        if compact_count(payload.get("owner_email_will_send_count")) != compact_count(payload.get("owner_email_send_evidence_count")):
            issues.append(
                issue(
                    "owner_email_send_evidence_count_mismatch",
                    f"{compact_count(payload.get('owner_email_send_evidence_count'))}!={compact_count(payload.get('owner_email_will_send_count'))}",
                )
            )
    return issues


def validate_owner_email_packet(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed"}))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "run_month",
                "runtime_map",
                "runtime_map_exists",
                "runtime_map_canonical_name_ok",
                "runtime_map_expected_name",
                "issue_count",
                "actionable_summary",
                "primary_blocker",
                "next_action",
                "hold",
                "property_count",
                "available_property_count",
                "recipient_count",
                "packet_count",
                "property_unavailable_count",
                "property_unavailable_reason_counts",
                "property_unavailable_bounded",
                "full_history_leak_count",
                "full_history_guard_issue_count",
                "body_guard_issue_count",
                "financial_summary_enriched_property_count",
                "monthly_financial_summary_present_property_count",
                "property_unavailable_financial_summary_enriched_count",
                "property_unavailable_monthly_financial_summary_present_count",
                "monthly_financial_summary_present_total_property_count",
                "monthly_financial_summary_missing_property_count",
                "already_sent_for_run_month",
                "max_once_monthly_ok",
                "safe_to_send_now",
                "send_requested",
                "send_result_count",
                "send_failed_count",
                "send_results_bounded",
                "sent_state_written",
                "packets_bounded",
                "preview_file_write_allowed",
                "preview_write_blocked_reason",
                "unsafe_preview_packet_count",
                "stale_preview_file_removed_count",
                "stale_preview_cleanup_error_count",
                "previews",
                "requires_live_update_guard",
                "live_update_capture_report",
                "listing_cleanup_queue_report",
                "review_candidate_packet_report",
            },
        )
    )
    actionable = payload.get("actionable_summary")
    primary = payload.get("primary_blocker")
    if not isinstance(actionable, dict):
        issues.append(issue("owner_email_packet_invalid_actionable_summary", type(actionable).__name__))
        actionable = {}
    actionable_primary = actionable.get("primary_blocker")
    if payload.get("status") == "review" and not isinstance(primary, dict):
        issues.append(issue("owner_email_packet_missing_primary_blocker", "review_requires_primary_blocker"))
    if payload.get("status") == "review" and not isinstance(actionable_primary, dict):
        issues.append(issue("owner_email_packet_missing_actionable_primary_blocker", "review_requires_actionable_summary.primary_blocker"))
    if isinstance(actionable_primary, dict) and primary != actionable_primary:
        issues.append(issue("owner_email_packet_primary_blocker_alias_mismatch", "primary_blocker must match actionable_summary.primary_blocker"))
    expected_primary = actionable_primary if isinstance(actionable_primary, dict) else primary
    if isinstance(expected_primary, dict):
        missing_blocker_fields = [
            key
            for key in ("id", "class", "summary", "blocker", "artifact", "next_action", "hold")
            if key not in expected_primary
        ]
        if missing_blocker_fields:
            issues.append(issue("owner_email_packet_primary_blocker_missing_fields", ",".join(missing_blocker_fields)))
        if payload.get("next_action") != expected_primary.get("next_action"):
            issues.append(issue("owner_email_packet_next_action_alias_mismatch", "next_action must match primary_blocker.next_action"))
        if payload.get("hold") != expected_primary.get("hold"):
            issues.append(issue("owner_email_packet_hold_alias_mismatch", "hold must match primary_blocker.hold"))
    secondary_blockers = actionable.get("secondary_blockers")
    if secondary_blockers is not None:
        if not isinstance(secondary_blockers, list):
            issues.append(issue("owner_email_packet_secondary_blockers_invalid", type(secondary_blockers).__name__))
        else:
            for index, blocker in enumerate(secondary_blockers):
                if not isinstance(blocker, dict):
                    issues.append(issue("owner_email_packet_secondary_blocker_invalid", f"{index}:{type(blocker).__name__}"))
                    continue
                missing_secondary_fields = [
                    key
                    for key in ("id", "class", "summary", "blocker", "artifact", "next_action", "hold")
                    if key not in blocker
                ]
                if missing_secondary_fields:
                    issues.append(issue("owner_email_packet_secondary_blocker_missing_fields", f"{index}:{','.join(missing_secondary_fields)}"))
    if compact_count(payload.get("full_history_leak_count")):
        issues.append(issue("owner_email_packet_full_history_leak", str(payload.get("full_history_leak_count"))))
    if compact_count(payload.get("body_guard_issue_count")):
        issues.append(issue("owner_email_packet_body_guard_issue", str(payload.get("body_guard_issue_count"))))
    runtime_map = str(payload.get("runtime_map") or "")
    expected_runtime_map_name = str(payload.get("runtime_map_expected_name") or "baselane_financials_monthly_lofty_pm_runtime_map.json")
    if payload.get("runtime_map_exists") is not True:
        issues.append(issue("owner_email_packet_runtime_map_missing", runtime_map or "missing"))
    if payload.get("runtime_map_canonical_name_ok") is not True:
        issues.append(issue("owner_email_packet_runtime_map_noncanonical", Path(runtime_map).name or "missing"))
    if Path(runtime_map).name != expected_runtime_map_name:
        issues.append(issue("owner_email_packet_runtime_map_name_mismatch", f"{Path(runtime_map).name or 'missing'}!={expected_runtime_map_name}"))
    if payload.get("requires_live_update_guard") is not True:
        issues.append(issue("owner_email_packet_live_guard_not_required", str(payload.get("requires_live_update_guard"))))
    live_report = str(payload.get("live_update_capture_report") or "")
    if Path(live_report).name != "baselane_financials_monthly_live_update_capture.json":
        issues.append(issue("owner_email_packet_live_guard_report_unexpected", live_report or "missing"))
    listing_cleanup_report = str(payload.get("listing_cleanup_queue_report") or "")
    if Path(listing_cleanup_report).name != "lofty_listing_update_cleanup_queue.json":
        issues.append(issue("owner_email_packet_listing_cleanup_report_unexpected", listing_cleanup_report or "missing"))
    review_candidate_report = str(payload.get("review_candidate_packet_report") or "")
    if Path(review_candidate_report).name != "baselane_financials_monthly_review_candidate_packet.json":
        issues.append(issue("owner_email_packet_review_candidate_report_unexpected", review_candidate_report or "missing"))
    reason_counts = payload.get("property_unavailable_reason_counts")
    if not isinstance(reason_counts, dict):
        issues.append(issue("owner_email_packet_reason_counts_invalid", type(reason_counts).__name__))
    elif sum(compact_count(value) for value in reason_counts.values()) != compact_count(payload.get("property_unavailable_count")):
        issues.append(
            issue(
                "owner_email_packet_reason_counts_mismatch",
                f"reasons={sum(compact_count(value) for value in reason_counts.values())},unavailable={compact_count(payload.get('property_unavailable_count'))}",
            )
        )
    property_unavailable_bounded = payload.get("property_unavailable_bounded")
    if not isinstance(property_unavailable_bounded, list):
        issues.append(issue("owner_email_packet_property_unavailable_bounded_invalid", type(property_unavailable_bounded).__name__))
        property_unavailable_bounded = []
    elif len(property_unavailable_bounded) == compact_count(payload.get("property_unavailable_count")):
        unavailable_present = sum(1 for record in property_unavailable_bounded if isinstance(record, dict) and record.get("monthly_financial_summary_present") is True)
        if unavailable_present != compact_count(payload.get("property_unavailable_monthly_financial_summary_present_count")):
            issues.append(
                issue(
                    "owner_email_packet_unavailable_financial_summary_present_count_mismatch",
                    f"{compact_count(payload.get('property_unavailable_monthly_financial_summary_present_count'))}!={unavailable_present}",
                )
            )
    safe_to_send = payload.get("safe_to_send_now") is True
    send_requested = payload.get("send_requested") is True
    sent_state_written = payload.get("sent_state_written") is True
    send_result_count = compact_count(payload.get("send_result_count"))
    send_failed_count = compact_count(payload.get("send_failed_count"))
    send_results = payload.get("send_results_bounded")
    packets_bounded = payload.get("packets_bounded")
    previews = payload.get("previews")
    already_sent = payload.get("already_sent_for_run_month") is True
    max_once_monthly_ok = payload.get("max_once_monthly_ok") is True
    preview_file_write_allowed = payload.get("preview_file_write_allowed") is True
    preview_write_blocked_reason = str(payload.get("preview_write_blocked_reason") or "").strip()
    unsafe_preview_packet_count = compact_count(payload.get("unsafe_preview_packet_count"))
    stale_preview_cleanup_error_count = compact_count(payload.get("stale_preview_cleanup_error_count"))
    bounded_leak_count = 0
    bounded_unsafe_preview_count = 0
    bounded_financial_summary_present_count = 0
    bounded_financial_summary_complete = True
    if not isinstance(packets_bounded, list):
        issues.append(issue("owner_email_packet_packets_bounded_invalid", type(packets_bounded).__name__))
    else:
        for index, packet in enumerate(packets_bounded):
            if not isinstance(packet, dict):
                issues.append(issue("owner_email_packet_bounded_packet_invalid", f"{index}:{type(packet).__name__}"))
                continue
            packet_property_count = compact_count(packet.get("property_count"))
            properties_bounded = packet.get("properties_bounded") if isinstance(packet.get("properties_bounded"), list) else []
            if packet_property_count != len(properties_bounded):
                bounded_financial_summary_complete = False
            bounded_financial_summary_present_count += sum(
                1 for prop in properties_bounded if isinstance(prop, dict) and prop.get("monthly_financial_summary_present") is True
            )
            marker_count = compact_count(packet.get("property_update_marker_count"))
            dated_heading_count = compact_count(packet.get("dated_update_heading_count"))
            body_guard_issue_count = compact_count(packet.get("body_guard_issue_count"))
            body_guard_issues = [str(item) for item in (packet.get("body_guard_issues") or [])]
            has_marker_leak = marker_count > packet_property_count
            has_dated_heading_leak = dated_heading_count > 0
            source_history_leak_issue_count = sum(
                1
                for item in body_guard_issues
                if item.startswith(
                    (
                        "property_update_marker_count=",
                        "dated_update_heading_count=",
                        "full_updates_header_count=",
                        "full_source_updates_md_embedded:",
                        "historical_update_date_leaked:",
                    )
                )
            )
            has_source_history_leak = source_history_leak_issue_count > 0
            has_body_guard_issue = body_guard_issue_count > 0
            if has_marker_leak or has_dated_heading_leak or has_source_history_leak or has_body_guard_issue:
                bounded_unsafe_preview_count += 1
            if has_marker_leak:
                issues.append(
                    issue(
                        "owner_email_packet_bounded_full_history_marker_leak",
                        f"{index}:markers={marker_count},properties={packet_property_count}",
                    )
                )
            if has_dated_heading_leak:
                issues.append(
                    issue(
                        "owner_email_packet_bounded_dated_heading_leak",
                        f"{index}:dated_headings={dated_heading_count}",
                    )
                )
            if has_source_history_leak:
                issues.append(
                    issue(
                        "owner_email_packet_bounded_source_history_leak",
                        f"{index}:source_history_guard_issues={source_history_leak_issue_count}",
                    )
                )
            if has_marker_leak or has_dated_heading_leak or has_source_history_leak:
                bounded_leak_count += 1
            if has_body_guard_issue:
                issues.append(
                    issue(
                        "owner_email_packet_bounded_body_guard_issue",
                        f"{index}:body_guard_issues={body_guard_issue_count}",
                    )
                )
        if compact_count(payload.get("packet_count")) > 0 and compact_count(payload.get("packet_count")) == len(packets_bounded) and bounded_financial_summary_complete:
            if compact_count(payload.get("monthly_financial_summary_present_property_count")) != bounded_financial_summary_present_count:
                issues.append(
                    issue(
                        "owner_email_packet_financial_summary_present_count_mismatch",
                        f"{compact_count(payload.get('monthly_financial_summary_present_property_count'))}!={bounded_financial_summary_present_count}",
                    )
                )
        if compact_count(payload.get("full_history_leak_count")) < bounded_leak_count:
            issues.append(
                issue(
                    "owner_email_packet_full_history_leak_count_underreported",
                    f"full_history={compact_count(payload.get('full_history_leak_count'))},bounded={bounded_leak_count}",
                )
            )
        if compact_count(payload.get("packet_count")) == len(packets_bounded) and compact_count(payload.get("full_history_leak_count")) != bounded_leak_count:
            issues.append(
                issue(
                    "owner_email_packet_full_history_leak_count_mismatch",
                    f"full_history={compact_count(payload.get('full_history_leak_count'))},bounded={bounded_leak_count}",
                )
            )
        if compact_count(payload.get("packet_count")) == len(packets_bounded) and unsafe_preview_packet_count != bounded_unsafe_preview_count:
            issues.append(
                issue(
                    "owner_email_packet_unsafe_preview_packet_count_mismatch",
                    f"unsafe_preview={unsafe_preview_packet_count},bounded={bounded_unsafe_preview_count}",
                )
            )
        if unsafe_preview_packet_count < bounded_unsafe_preview_count:
            issues.append(
                issue(
                    "owner_email_packet_unsafe_preview_packet_count_underreported",
                    f"unsafe_preview={unsafe_preview_packet_count},bounded={bounded_unsafe_preview_count}",
                )
            )
    total_present = compact_count(payload.get("monthly_financial_summary_present_total_property_count"))
    reported_present_parts = compact_count(payload.get("monthly_financial_summary_present_property_count")) + compact_count(
        payload.get("property_unavailable_monthly_financial_summary_present_count")
    )
    if total_present != reported_present_parts:
        issues.append(
            issue(
                "owner_email_packet_financial_summary_present_total_mismatch",
                f"{total_present}!={reported_present_parts}",
            )
        )
    expected_missing = max(0, compact_count(payload.get("property_count")) - total_present)
    if compact_count(payload.get("monthly_financial_summary_missing_property_count")) != expected_missing:
        issues.append(
            issue(
                "owner_email_packet_financial_summary_missing_count_mismatch",
                f"{compact_count(payload.get('monthly_financial_summary_missing_property_count'))}!={expected_missing}",
            )
        )
    if unsafe_preview_packet_count:
        issues.append(issue("owner_email_packet_unsafe_preview_packets", str(unsafe_preview_packet_count)))
    if stale_preview_cleanup_error_count:
        issues.append(issue("owner_email_packet_stale_preview_cleanup_errors", str(stale_preview_cleanup_error_count)))
    if preview_file_write_allowed and compact_count(payload.get("packet_count")) <= 0:
        issues.append(issue("owner_email_packet_preview_allowed_without_packets", str(payload.get("packet_count"))))
    if preview_file_write_allowed and unsafe_preview_packet_count:
        issues.append(issue("owner_email_packet_preview_allowed_with_unsafe_packets", str(unsafe_preview_packet_count)))
    if preview_file_write_allowed and preview_write_blocked_reason:
        issues.append(issue("owner_email_packet_preview_allowed_with_blocked_reason", preview_write_blocked_reason))
    if not preview_file_write_allowed and compact_count(payload.get("packet_count")) <= 0 and not preview_write_blocked_reason:
        issues.append(issue("owner_email_packet_preview_block_missing_reason", "no packets"))
    if not preview_file_write_allowed and unsafe_preview_packet_count and not preview_write_blocked_reason:
        issues.append(issue("owner_email_packet_preview_block_missing_reason", "unsafe packets"))
    if previews is not None:
        if not isinstance(previews, list):
            issues.append(issue("owner_email_packet_previews_invalid", type(previews).__name__))
        elif not preview_file_write_allowed and previews:
            issues.append(issue("owner_email_packet_previews_written_when_blocked", str(len(previews))))
        elif preview_file_write_allowed and compact_count(payload.get("packet_count")) == len(packets_bounded if isinstance(packets_bounded, list) else []):
            if len(previews) != compact_count(payload.get("packet_count")):
                issues.append(
                    issue(
                        "owner_email_packet_preview_count_mismatch",
                        f"previews={len(previews)},packets={compact_count(payload.get('packet_count'))}",
                    )
                )
    if max_once_monthly_ok == already_sent:
        issues.append(
            issue(
                "owner_email_packet_idempotency_flag_mismatch",
                f"already_sent={payload.get('already_sent_for_run_month')},max_once_monthly_ok={payload.get('max_once_monthly_ok')}",
            )
        )
    if send_failed_count > send_result_count:
        issues.append(
            issue(
                "owner_email_packet_send_failed_count_exceeds_results",
                f"{send_failed_count}>{send_result_count}",
            )
        )
    if send_failed_count:
        issues.append(issue("owner_email_packet_send_failed_count_nonzero", str(send_failed_count)))
        if payload.get("status") == "ok":
            issues.append(issue("owner_email_packet_status_ok_with_send_failures", str(send_failed_count)))
    if safe_to_send and send_requested and send_result_count <= 0:
        issues.append(issue("owner_email_packet_safe_send_without_results", str(send_result_count)))
    if send_results is not None:
        if not isinstance(send_results, list):
            issues.append(issue("owner_email_packet_send_results_invalid", type(send_results).__name__))
        elif len(send_results) > send_result_count:
            issues.append(
                issue(
                    "owner_email_packet_send_results_bounded_count_exceeds_total",
                    f"{len(send_results)}>{send_result_count}",
                )
            )
    if safe_to_send:
        if compact_count(payload.get("recipient_count")) <= 0:
            issues.append(issue("owner_email_packet_safe_without_recipients", str(payload.get("recipient_count"))))
        if compact_count(payload.get("packet_count")) <= 0:
            issues.append(issue("owner_email_packet_safe_without_packets", str(payload.get("packet_count"))))
        if payload.get("max_once_monthly_ok") is not True:
            issues.append(issue("owner_email_packet_safe_without_idempotency", str(payload.get("max_once_monthly_ok"))))
        if payload.get("already_sent_for_run_month") is True:
            issues.append(issue("owner_email_packet_safe_after_already_sent", str(payload.get("already_sent_for_run_month"))))
    if send_requested and safe_to_send is not True:
        issues.append(issue("owner_email_packet_send_requested_when_unsafe", str(payload.get("safe_to_send_now"))))
    if not send_requested:
        if send_result_count:
            issues.append(issue("owner_email_packet_send_results_without_request", str(send_result_count)))
        if send_failed_count:
            issues.append(issue("owner_email_packet_send_failures_without_request", str(send_failed_count)))
        if isinstance(send_results, list) and send_results:
            issues.append(issue("owner_email_packet_send_results_bounded_without_request", str(len(send_results))))
    if sent_state_written and not send_requested:
        issues.append(issue("owner_email_packet_state_written_without_send", str(payload.get("sent_state_written"))))
    if sent_state_written and safe_to_send is not True:
        issues.append(issue("owner_email_packet_state_written_when_unsafe", str(payload.get("safe_to_send_now"))))
    if sent_state_written and send_failed_count:
        issues.append(issue("owner_email_packet_state_written_with_send_failures", str(send_failed_count)))
    if sent_state_written and send_result_count <= 0:
        issues.append(issue("owner_email_packet_state_written_without_results", str(send_result_count)))
    return issues


def integrity_format_money(value: object) -> str:
    if value is None:
        return "Not available"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def integrity_is_number(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def parse_integrity_amount(value: object) -> float:
    text = str(value or "0").replace("$", "").replace(",", "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    try:
        return float(text or 0)
    except ValueError:
        return 0.0


def monthly_candidate_source_gl_issues(summary: dict, property_name: str) -> list[dict]:
    issues: list[dict] = []
    source = str(summary.get("eco_gl_column_e_source") or "").strip()
    if not source or str(summary.get("eco_gl_column_e_source_mode") or "") == "source_ledger_zero_rows":
        return issues
    path = Path(source)
    if not path.is_file():
        issues.append(issue("monthly_candidate_eco_gl_source_file_missing", f"{property_name}:{source}"))
        return issues
    if path.suffix.lower() != ".csv":
        return issues
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except Exception as exc:  # noqa: BLE001
        issues.append(issue("monthly_candidate_eco_gl_source_file_unreadable", f"{property_name}:{type(exc).__name__}:{exc}"))
        return issues
    actual_row_count = len(rows)
    actual_sum = round(sum(parse_integrity_amount(row.get("Amount")) for row in rows), 2)
    expected_row_count = compact_count(summary.get("eco_gl_column_e_row_count"))
    expected_sum = round(float(summary.get("eco_gl_column_e_sum") or 0), 2) if integrity_is_number(summary.get("eco_gl_column_e_sum")) else None
    if expected_row_count != actual_row_count:
        issues.append(issue("monthly_candidate_eco_gl_source_row_count_mismatch", f"{property_name}:{expected_row_count}!={actual_row_count}:{source}"))
    if expected_sum is None or abs(expected_sum - actual_sum) > 0.01:
        issues.append(issue("monthly_candidate_eco_gl_source_sum_mismatch", f"{property_name}:{expected_sum}!={actual_sum}:{source}"))
    return issues


def monthly_candidate_file_consistency_issues(record: dict, summary: dict, property_name: str) -> list[dict]:
    issues: list[dict] = []
    reserve = integrity_format_money(summary.get("lofty_curr_maintenance_reserve"))
    eco_sum = integrity_format_money(summary.get("eco_gl_column_e_sum"))
    eco_cash = integrity_format_money(
        summary.get("eco_operating_cash")
        if integrity_is_number(summary.get("eco_operating_cash"))
        else summary.get("eco_gl_column_e_sum")
    )
    row_count = compact_count(summary.get("eco_gl_column_e_row_count"))
    cash_row_count = compact_count(
        summary.get("eco_operating_cash_row_count")
        if summary.get("eco_operating_cash_row_count") is not None
        else summary.get("eco_gl_column_e_row_count")
    )
    update_candidate = Path(str(record.get("update_candidate") or ""))
    if not str(record.get("update_candidate") or "").strip():
        issues.append(issue("monthly_candidate_update_candidate_missing", property_name))
    elif not update_candidate.is_file():
        issues.append(issue("monthly_candidate_update_candidate_file_missing", f"{property_name}:{update_candidate}"))
    else:
        text = update_candidate.read_text(encoding="utf-8", errors="replace")
        expected_reserve_line = f"- Lofty-held current maintenance reserve: {reserve}"
        expected_eco_line = f"- ECO GL Column E sum: {eco_sum} ({row_count} rows)"
        concise_reserve_line = f"Lofty Operating Cash: {reserve} (Lofty curr_maintenance_reserve)"
        concise_eco_line = f"ECO Operating Cash: {eco_cash} (ECO Systems General Ledger Column E ({cash_row_count} rows))"
        concise_gl_line = f"ECO General Ledger: {eco_sum} (ECO Systems General Ledger Column E ({row_count} rows))"
        sentence_eco_line = f"ECO Operating Cash is {eco_cash} from the full ECO Systems General Ledger Column E balance."
        position_eco_line = f"ECO GL Column E position is {eco_sum}"
        if expected_reserve_line not in text and concise_reserve_line not in text:
            issues.append(issue("monthly_candidate_update_reserve_summary_mismatch", f"{property_name}:{expected_reserve_line}"))
        if (
            concise_eco_line not in text
            and sentence_eco_line not in text
            and position_eco_line not in text
            and expected_eco_line not in text
        ):
            issues.append(issue("monthly_candidate_update_eco_summary_mismatch", f"{property_name}:{expected_eco_line}"))
        for match in re.finditer(r"Lofty Operating Cash of (Not available|-?\$[\d,]+\.\d{2})", text, re.I):
            if match.group(1) != reserve:
                issues.append(issue("monthly_candidate_update_stale_reserve_value", f"{property_name}:{match.group(1)}!={reserve}"))
        for match in re.finditer(
            r"ECO Systems General Ledger Column E operating cash balance is \*\*(Not available|-?\$[\d,]+\.\d{2})\*\* across \*\*(\d+)\s+rows\*\*",
            text,
            re.I,
        ):
            if match.group(1) != eco_sum or compact_count(match.group(2)) != row_count:
                issues.append(issue("monthly_candidate_update_stale_eco_value", f"{property_name}:{match.group(1)}:{match.group(2)}!={eco_sum}:{row_count}"))
    financial_candidate = Path(str(record.get("financial_candidate") or ""))
    if not str(record.get("financial_candidate") or "").strip():
        issues.append(issue("monthly_candidate_financial_candidate_missing", property_name))
    elif not financial_candidate.is_file():
        issues.append(issue("monthly_candidate_financial_candidate_file_missing", f"{property_name}:{financial_candidate}"))
    else:
        text = financial_candidate.read_text(encoding="utf-8", errors="replace")
        expected_reserve_row = f"| Lofty Operating Cash | {reserve} | Lofty `curr_maintenance_reserve` |"
        expected_eco_row = f"| ECO Operating Cash | {eco_cash} | ECO Systems General Ledger Column E ({cash_row_count} rows) |"
        expected_gl_row = f"| ECO General Ledger | {eco_sum} | ECO Systems General Ledger Column E ({row_count} rows) |"
        if expected_reserve_row not in text:
            issues.append(issue("monthly_candidate_financial_reserve_summary_mismatch", f"{property_name}:{expected_reserve_row}"))
        if expected_eco_row not in text and expected_gl_row not in text:
            issues.append(issue("monthly_candidate_financial_eco_summary_mismatch", f"{property_name}:{expected_eco_row}"))
    return issues


def validate_monthly_review_candidate_packet(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed"}))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "run_month",
                "property_count",
                "update_candidate_count",
                "financial_candidate_count",
                "issue_count",
                "records",
            },
        )
    )
    records = payload.get("records")
    if not isinstance(records, list):
        issues.append(issue("monthly_candidate_records_invalid", type(records).__name__))
        return issues
    property_count = compact_count(payload.get("property_count"))
    manifest_source_issues = payload.get("review_manifest_source_issues")
    manifest_source_issue_count = compact_count(payload.get("manifest_source_issue_count"))
    if isinstance(manifest_source_issues, list):
        manifest_source_issue_count = max(manifest_source_issue_count, len([item for item in manifest_source_issues if item]))
    if manifest_source_issue_count:
        detail = ";".join(str(item) for item in (manifest_source_issues or []) if item) or str(manifest_source_issue_count)
        issues.append(issue("monthly_candidate_manifest_source_issues", detail))
    if str(payload.get("empty_candidate_packet_reason") or "").strip():
        issues.append(issue("monthly_candidate_empty_packet_reason", str(payload.get("empty_candidate_packet_reason"))))
    if property_count != len(records):
        issues.append(issue("monthly_candidate_property_count_mismatch", f"property_count={property_count},records={len(records)}"))
    if compact_count(payload.get("update_candidate_count")) != len(records):
        issues.append(issue("monthly_candidate_update_count_mismatch", f"update={payload.get('update_candidate_count')},records={len(records)}"))
    if compact_count(payload.get("financial_candidate_count")) != len(records):
        issues.append(issue("monthly_candidate_financial_count_mismatch", f"financial={payload.get('financial_candidate_count')},records={len(records)}"))
    record_issue_count = 0
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            issues.append(issue("monthly_candidate_record_invalid", f"{index}:{type(record).__name__}"))
            continue
        property_name = str(record.get("property_name") or record.get("input_property_name") or f"record[{index}]")
        record_issues = record.get("issues")
        if isinstance(record_issues, list):
            record_issue_count += len(record_issues)
        elif record_issues is not None:
            issues.append(issue("monthly_candidate_record_issues_invalid", property_name))
        summary = record.get("monthly_financial_summary")
        if not isinstance(summary, dict):
            issues.append(issue("monthly_candidate_missing_financial_summary", property_name))
            continue
        if summary.get("lofty_curr_maintenance_reserve") is None:
            issues.append(issue("monthly_candidate_missing_lofty_curr_maintenance_reserve", property_name))
        elif not integrity_is_number(summary.get("lofty_curr_maintenance_reserve")):
            issues.append(issue("monthly_candidate_invalid_lofty_curr_maintenance_reserve", f"{property_name}:{summary.get('lofty_curr_maintenance_reserve')}"))
        if not str(summary.get("lofty_curr_maintenance_reserve_source") or "").strip():
            issues.append(issue("monthly_candidate_missing_lofty_curr_maintenance_reserve_source", property_name))
        if summary.get("eco_gl_column_e_sum") is None:
            issues.append(issue("monthly_candidate_missing_eco_gl_column_e_sum", property_name))
        elif not integrity_is_number(summary.get("eco_gl_column_e_sum")):
            issues.append(issue("monthly_candidate_invalid_eco_gl_column_e_sum", f"{property_name}:{summary.get('eco_gl_column_e_sum')}"))
        if summary.get("eco_gl_column_e_status") != "ok":
            issues.append(issue("monthly_candidate_eco_gl_column_e_not_ok", f"{property_name}:{summary.get('eco_gl_column_e_status')}"))
        row_count = summary.get("eco_gl_column_e_row_count")
        if not isinstance(row_count, int) or row_count < 0:
            issues.append(issue("monthly_candidate_invalid_eco_gl_column_e_row_count", f"{property_name}:{row_count}"))
        issues.extend(monthly_candidate_source_gl_issues(summary, property_name))
        eco_source = str(summary.get("eco_gl_column_e_source") or "")
        if not eco_source:
            issues.append(issue("monthly_candidate_missing_eco_gl_column_e_source", property_name))
        else:
            if LEGACY_PUBLIC_FINANCIALS_PATH in eco_source:
                issues.append(issue("monthly_candidate_legacy_financials_source", eco_source))
            source_mode = str(summary.get("eco_gl_column_e_source_mode") or "")
            aggregate_zero_rows_ok = (
                source_mode == "source_ledger_zero_rows"
                and row_count == 0
                and "/Dropbox/Projects/assetrail/" in eco_source
            )
            if aggregate_zero_rows_ok and not (
                record.get("zero_row_source_ledger_reviewed") is True
                and str(record.get("zero_row_source_ledger_decision") or "") in {"include_active_no_activity", "exclude_no_dao_activity"}
            ):
                issues.append(issue("monthly_candidate_zero_row_source_ledger_decision_missing", property_name))
            if (
                not is_canonical_owner_statement_source_path(eco_source)
                and not is_canonical_financials_path(eco_source, file_name="")
                and not aggregate_zero_rows_ok
            ):
                issues.append(issue("monthly_candidate_noncanonical_financials_source", eco_source))
        update_target = str(record.get("update_approval_target") or "")
        financial_target = str(record.get("financial_approval_target") or "")
        if LEGACY_PUBLIC_UPDATES_PATH in update_target:
            issues.append(issue("monthly_candidate_legacy_updates_target", update_target))
        if LEGACY_PUBLIC_FINANCIALS_PATH in financial_target:
            issues.append(issue("monthly_candidate_legacy_financials_target", financial_target))
        if update_target and not is_canonical_updates_path(update_target, file_name=""):
            issues.append(issue("monthly_candidate_noncanonical_updates_target", update_target))
        if financial_target and not is_canonical_financials_path(financial_target, file_name=""):
            issues.append(issue("monthly_candidate_noncanonical_financials_target", financial_target))
        issues.extend(monthly_candidate_file_consistency_issues(record, summary, property_name))
    if compact_count(payload.get("issue_count")) < record_issue_count:
        issues.append(issue("monthly_candidate_issue_count_underreported", f"issue_count={payload.get('issue_count')},record_issues={record_issue_count}"))
    if payload.get("status") == "ok" and record_issue_count:
        issues.append(issue("monthly_candidate_status_ok_with_record_issues", str(record_issue_count)))
    payload_issue_count = compact_count(payload.get("issue_count"))
    if payload_issue_count:
        issues.append(issue("monthly_candidate_packet_issue_count_nonzero", str(payload_issue_count)))
    financial_gate_issue_count = compact_count(payload.get("financial_candidate_gate_issue_count"))
    if financial_gate_issue_count:
        issues.append(issue("monthly_candidate_financial_gate_issue_count_nonzero", str(financial_gate_issue_count)))
    return issues


def validate_empty_updates_backfill_queue(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed"}))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "issue_count",
                "mutates_dropbox_files",
                "mutates_lofty_listing",
                "sends_owner_email",
                "commands_require_explicit_approval",
                "approval_copy_requires_current_rent_roll",
                "property_count",
                "ready_local_backfill_from_approved_count",
                "needs_update_approval_target_count",
                "blocked_count",
                "record_status_counts",
                "empty_updates_backfill_idempotency_digest",
                "queue_csv",
                "local_backfill_from_approved_commands_file",
                "approval_copy_commands_requires_current_rent_roll_and_explicit_approval_file",
                "records",
            },
        )
    )
    if payload.get("mutates_dropbox_files") is not False:
        issues.append(issue("empty_updates_queue_mutates_dropbox", str(payload.get("mutates_dropbox_files"))))
    if payload.get("mutates_lofty_listing") is not False:
        issues.append(issue("empty_updates_queue_mutates_lofty", str(payload.get("mutates_lofty_listing"))))
    if payload.get("sends_owner_email") is not False:
        issues.append(issue("empty_updates_queue_sends_owner_email", str(payload.get("sends_owner_email"))))
    if payload.get("commands_require_explicit_approval") is not True:
        issues.append(issue("empty_updates_queue_commands_not_approval_gated", str(payload.get("commands_require_explicit_approval"))))
    if payload.get("approval_copy_requires_current_rent_roll") is not True:
        issues.append(issue("empty_updates_queue_approval_not_rent_roll_gated", str(payload.get("approval_copy_requires_current_rent_roll"))))
    if compact_count(payload.get("issue_count")):
        issues.append(issue("empty_updates_queue_issue_count_nonzero", str(payload.get("issue_count"))))
    digest = str(payload.get("empty_updates_backfill_idempotency_digest") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        issues.append(issue("empty_updates_queue_invalid_digest", digest or "missing"))
    records = payload.get("records")
    if not isinstance(records, list):
        issues.append(issue("empty_updates_queue_records_invalid", type(records).__name__))
        records = []
    status_counts = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            issues.append(issue("empty_updates_queue_record_invalid", f"{index}:{type(record).__name__}"))
            continue
        status = str(record.get("status") or "")
        status_counts[status] = status_counts.get(status, 0) + 1
        if record.get("mutates_lofty_listing") is not False:
            issues.append(issue("empty_updates_queue_record_mutates_lofty", f"{index}:{record.get('property_name') or '?'}"))
        if record.get("sends_owner_email") is not False:
            issues.append(issue("empty_updates_queue_record_sends_email", f"{index}:{record.get('property_name') or '?'}"))
        if status == "ready_local_backfill_from_approved" and not str(record.get("local_backfill_command_requires_explicit_approval") or "").strip():
            issues.append(issue("empty_updates_queue_ready_missing_local_backfill_command", f"{index}:{record.get('property_name') or '?'}"))
        if status in {"needs_update_approval_target", "needs_update_approval_target_refresh"} and not str(
            record.get("approval_copy_command_requires_current_rent_roll_and_explicit_approval") or ""
        ).strip():
            issues.append(issue("empty_updates_queue_approval_missing_command", f"{index}:{record.get('property_name') or '?'}"))
        if status == "ready_local_backfill_from_approved" and not str(record.get("source_for_backfill_sha256") or "").strip():
            issues.append(issue("empty_updates_queue_ready_missing_source_digest", f"{index}:{record.get('property_name') or '?'}"))
    if compact_count(payload.get("property_count")) != len(records):
        issues.append(issue("empty_updates_queue_property_count_mismatch", f"{payload.get('property_count')}!={len(records)}"))
    reported_counts = payload.get("record_status_counts")
    if isinstance(reported_counts, dict):
        normalized_reported = {str(key): compact_count(value) for key, value in reported_counts.items()}
        if normalized_reported != dict(sorted(status_counts.items())):
            issues.append(issue("empty_updates_queue_status_counts_mismatch", json.dumps({"reported": normalized_reported, "records": dict(sorted(status_counts.items()))}, sort_keys=True)))
    else:
        issues.append(issue("empty_updates_queue_status_counts_invalid", type(reported_counts).__name__))
    ready_count = status_counts.get("ready_local_backfill_from_approved", 0)
    approval_count = status_counts.get("needs_update_approval_target", 0) + status_counts.get("needs_update_approval_target_refresh", 0)
    blocked_count = sum(count for status, count in status_counts.items() if status.startswith("blocked"))
    if compact_count(payload.get("ready_local_backfill_from_approved_count")) != ready_count:
        issues.append(issue("empty_updates_queue_ready_count_mismatch", f"{payload.get('ready_local_backfill_from_approved_count')}!={ready_count}"))
    if compact_count(payload.get("needs_update_approval_target_count")) != approval_count:
        issues.append(issue("empty_updates_queue_approval_count_mismatch", f"{payload.get('needs_update_approval_target_count')}!={approval_count}"))
    if compact_count(payload.get("blocked_count")) != blocked_count:
        issues.append(issue("empty_updates_queue_blocked_count_mismatch", f"{payload.get('blocked_count')}!={blocked_count}"))
    if compact_count(payload.get("local_backfill_from_approved_command_count")) != ready_count:
        issues.append(issue("empty_updates_queue_local_command_count_mismatch", f"{payload.get('local_backfill_from_approved_command_count')}!={ready_count}"))
    if compact_count(payload.get("approval_copy_command_requires_current_rent_roll_count")) != approval_count:
        issues.append(issue("empty_updates_queue_approval_command_count_mismatch", f"{payload.get('approval_copy_command_requires_current_rent_roll_count')}!={approval_count}"))
    return issues


def validate_listing_cleanup_dry_run_verify(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed"}))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "issue_count",
                "mutates_lofty_listing",
                "sends_owner_email",
                "dry_run_only",
                "ready_listing_cleanup_count",
                "dry_run_command_count",
                "verified_record_count",
                "send_step_count",
                "unsafe_send_step_count",
                "bad_verified_record_count",
                "ready_cleanup_idempotency_digest",
                "listing_update_scope",
                "requires_monthly_financial_summary",
            },
        )
    )
    if payload.get("status") != "ok":
        issues.append(issue("listing_cleanup_dry_run_verify_not_ok", str(payload.get("status"))))
    if compact_count(payload.get("issue_count")):
        issues.append(issue("listing_cleanup_dry_run_verify_issue_count_nonzero", str(payload.get("issue_count"))))
    if payload.get("mutates_lofty_listing") is not False:
        issues.append(issue("listing_cleanup_dry_run_verify_mutates_lofty", str(payload.get("mutates_lofty_listing"))))
    if payload.get("sends_owner_email") is not False:
        issues.append(issue("listing_cleanup_dry_run_verify_sends_owner_email", str(payload.get("sends_owner_email"))))
    if payload.get("dry_run_only") is not True:
        issues.append(issue("listing_cleanup_dry_run_verify_not_dry_run_only", str(payload.get("dry_run_only"))))
    allowed_cleanup_scopes = {"full_history"}
    if payload.get("listing_update_scope") not in allowed_cleanup_scopes:
        issues.append(issue("listing_cleanup_dry_run_verify_scope_not_supported", str(payload.get("listing_update_scope"))))
    requires_monthly_financial_summary = payload.get("requires_monthly_financial_summary") is True
    if payload.get("requires_monthly_financial_summary") is not True:
        issues.append(
            issue(
                "listing_cleanup_dry_run_verify_monthly_financial_summary_not_required",
                str(payload.get("requires_monthly_financial_summary")),
            )
        )
    ready_count = compact_count(payload.get("ready_listing_cleanup_count"))
    verified_count = compact_count(payload.get("verified_record_count"))
    command_count = compact_count(payload.get("dry_run_command_count"))
    send_step_count = compact_count(payload.get("send_step_count"))
    if verified_count != ready_count:
        issues.append(issue("listing_cleanup_dry_run_verify_verified_count_mismatch", f"{verified_count}!={ready_count}"))
    if command_count != ready_count:
        issues.append(issue("listing_cleanup_dry_run_verify_command_count_mismatch", f"{command_count}!={ready_count}"))
    if send_step_count != ready_count:
        issues.append(issue("listing_cleanup_dry_run_verify_send_step_count_mismatch", f"{send_step_count}!={ready_count}"))
    if compact_count(payload.get("unsafe_send_step_count")):
        issues.append(issue("listing_cleanup_dry_run_verify_unsafe_send_steps", str(payload.get("unsafe_send_step_count"))))
    if compact_count(payload.get("bad_verified_record_count")):
        issues.append(issue("listing_cleanup_dry_run_verify_bad_records", str(payload.get("bad_verified_record_count"))))
    digest = str(payload.get("ready_cleanup_idempotency_digest") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        issues.append(issue("listing_cleanup_dry_run_verify_invalid_digest", digest or "missing"))
    records = payload.get("verified_records_bounded")
    if isinstance(records, list):
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                issues.append(issue("listing_cleanup_dry_run_verify_record_invalid", f"{index}:{type(record).__name__}"))
                continue
            if record.get("will_send") is not False:
                issues.append(issue("listing_cleanup_dry_run_verify_record_will_send", f"{index}:{record.get('state_file') or '?'}"))
            if record.get("skip_send") is not True:
                issues.append(issue("listing_cleanup_dry_run_verify_record_skip_send_not_true", f"{index}:{record.get('state_file') or '?'}"))
            if record.get("dry_run") is not True:
                issues.append(issue("listing_cleanup_dry_run_verify_record_dry_run_not_true", f"{index}:{record.get('state_file') or '?'}"))
            if record.get("listing_update_scope") not in allowed_cleanup_scopes:
                issues.append(issue("listing_cleanup_dry_run_verify_record_scope_not_supported", f"{index}:{record.get('state_file') or '?'}"))
            if requires_monthly_financial_summary and record.get("financial_summary_enriched") is not True:
                issues.append(
                    issue(
                        "listing_cleanup_dry_run_verify_record_financial_summary_not_enriched",
                        f"{index}:{record.get('state_file') or '?'}",
                    )
                )
            if requires_monthly_financial_summary and record.get("require_monthly_financial_summary") is not True:
                issues.append(
                    issue(
                        "listing_cleanup_dry_run_verify_record_financial_summary_not_required",
                        f"{index}:{record.get('state_file') or '?'}",
                    )
                )
    return issues


def validate_listing_cleanup_local_live_verify(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed"}))
    issues.extend(require_keys(payload, {"status", "target_count", "ok_count", "issue_count", "records"}))
    if payload.get("status") != "ok":
        issues.append(issue("listing_cleanup_local_live_verify_not_ok", str(payload.get("status"))))
    if compact_count(payload.get("issue_count")):
        issues.append(issue("listing_cleanup_local_live_verify_issue_count_nonzero", str(payload.get("issue_count"))))
    if compact_count(payload.get("target_count")) != compact_count(payload.get("ok_count")):
        issues.append(issue("listing_cleanup_local_live_verify_count_mismatch", f"target={payload.get('target_count')},ok={payload.get('ok_count')}"))
    records = payload.get("records")
    if not isinstance(records, list):
        issues.append(issue("listing_cleanup_local_live_verify_records_not_list", type(records).__name__))
    elif compact_count(payload.get("target_count")) != len(records):
        issues.append(issue("listing_cleanup_local_live_verify_record_count_mismatch", f"target={payload.get('target_count')},records={len(records)}"))
    return issues


def validate_listing_cleanup_queue(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed", "skipped_publish_disabled"}))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "issue_count",
                "mutates_lofty_listing",
                "sends_owner_email",
                "live_apply_requires_explicit_approval",
                "live_apply_preflight_required",
                "ready_listing_cleanup_count",
                "blocked_count",
                "property_count",
                "record_count",
                "dry_run_command_count",
                "live_apply_command_requires_explicit_approval_count",
                "ready_cleanup_idempotency_digest",
                "queue_idempotency_digest",
                "ready_cleanup_manifest",
                "records",
                "dry_run_commands_file",
                "live_apply_commands_requires_explicit_approval_file",
                "live_apply_approval_env_var",
                "live_apply_approval_env_required_value",
                "live_apply_approval_digest_env_var",
                "live_apply_approval_digest_required_value",
                "live_apply_preflight_command",
                "live_apply_preflight_report",
                "live_apply_dry_run_verify_report",
                "requires_monthly_financial_summary",
                "yhome_transition_column_b_rule_ok",
                "excluded_ready_cleanup_count",
                "candidate_update_approval_csv",
                "candidate_update_approval_idempotency_digest",
                "candidate_update_approval_manifest",
                "candidate_update_approval_copy_requires_current_rent_roll",
                "candidate_update_approval_copy_requires_explicit_approval",
                "candidate_update_approval_copy_command_requires_current_rent_roll_count",
                "candidate_update_approval_copy_commands_requires_current_rent_roll_and_explicit_approval_file",
                "candidate_update_approval_copy_approval_env_var",
                "candidate_update_approval_copy_approval_required_value",
                "candidate_update_approval_copy_current_rent_roll_env_var",
                "candidate_update_approval_copy_current_rent_roll_required_value",
                "candidate_update_approval_copy_digest_env_var",
                "candidate_update_approval_copy_digest_required_value",
            },
        )
    )
    if compact_count(payload.get("issue_count")):
        issues.append(issue("listing_cleanup_queue_issue_count_nonzero", str(payload.get("issue_count"))))
    if payload.get("mutates_lofty_listing") is not False:
        issues.append(issue("listing_cleanup_queue_mutates_lofty", str(payload.get("mutates_lofty_listing"))))
    if payload.get("sends_owner_email") is not False:
        issues.append(issue("listing_cleanup_queue_sends_owner_email", str(payload.get("sends_owner_email"))))
    if payload.get("live_apply_requires_explicit_approval") is not True:
        issues.append(issue("listing_cleanup_queue_live_apply_not_approval_gated", str(payload.get("live_apply_requires_explicit_approval"))))
    if payload.get("live_apply_preflight_required") is not True:
        issues.append(issue("listing_cleanup_queue_live_apply_preflight_not_required", str(payload.get("live_apply_preflight_required"))))
    if payload.get("requires_monthly_financial_summary") is not True:
        issues.append(issue("listing_cleanup_queue_monthly_financial_summary_not_required", str(payload.get("requires_monthly_financial_summary"))))
    if payload.get("yhome_transition_column_b_rule_ok") is not True:
        issues.append(issue("listing_cleanup_queue_yhome_exclusion_guard_not_ok", str(payload.get("yhome_transition_column_b_rule_ok"))))
    if compact_count(payload.get("excluded_ready_cleanup_count")):
        issues.append(issue("listing_cleanup_queue_excluded_ready_cleanup", str(payload.get("excluded_ready_cleanup_count"))))
    queue_digest = str(payload.get("queue_idempotency_digest") or "")
    ready_digest = str(payload.get("ready_cleanup_idempotency_digest") or "")
    for label, digest in (("queue", queue_digest), ("ready", ready_digest)):
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            issues.append(issue(f"listing_cleanup_queue_{label}_digest_invalid", digest or "missing"))
    if str(payload.get("live_apply_approval_env_var") or "") != "LOFTY_LISTING_CLEANUP_APPLY_APPROVED":
        issues.append(issue("listing_cleanup_queue_approval_env_unexpected", str(payload.get("live_apply_approval_env_var") or "missing")))
    if str(payload.get("live_apply_approval_env_required_value") or "") != "1":
        issues.append(issue("listing_cleanup_queue_approval_env_value_unexpected", str(payload.get("live_apply_approval_env_required_value") or "missing")))
    if str(payload.get("live_apply_approval_digest_env_var") or "") != "LOFTY_LISTING_CLEANUP_APPLY_DIGEST":
        issues.append(issue("listing_cleanup_queue_digest_env_unexpected", str(payload.get("live_apply_approval_digest_env_var") or "missing")))
    if ready_digest and str(payload.get("live_apply_approval_digest_required_value") or "") != ready_digest:
        issues.append(issue("listing_cleanup_queue_digest_env_value_mismatch", str(payload.get("live_apply_approval_digest_required_value") or "missing")))
    if Path(str(payload.get("live_apply_dry_run_verify_report") or "")).name != "lofty_listing_cleanup_dry_run_verify.json":
        issues.append(issue("listing_cleanup_queue_dry_run_verify_report_unexpected", str(payload.get("live_apply_dry_run_verify_report") or "missing")))
    if Path(str(payload.get("live_apply_preflight_report") or "")).name != "lofty_listing_update_cleanup_queue.live-apply-preflight.json":
        issues.append(issue("listing_cleanup_queue_apply_preflight_report_unexpected", str(payload.get("live_apply_preflight_report") or "missing")))
    if Path(str(payload.get("candidate_update_approval_csv") or "")).name != "lofty_listing_update_cleanup_queue.candidate-approval.csv":
        issues.append(issue("listing_cleanup_queue_candidate_approval_csv_unexpected", str(payload.get("candidate_update_approval_csv") or "missing")))
    if (
        Path(str(payload.get("candidate_update_approval_copy_commands_requires_current_rent_roll_and_explicit_approval_file") or "")).name
        != "lofty_listing_update_cleanup_queue.candidate-approval-copy.requires-current-rent-roll-and-explicit-approval.sh"
    ):
        issues.append(
            issue(
                "listing_cleanup_queue_candidate_approval_copy_file_unexpected",
                str(payload.get("candidate_update_approval_copy_commands_requires_current_rent_roll_and_explicit_approval_file") or "missing"),
            )
        )
    preflight_command = str(payload.get("live_apply_preflight_command") or "")
    for token, code in (
        ("lofty_listing_cleanup_apply_preflight.py", "listing_cleanup_queue_apply_preflight_command_missing_script"),
        ("LOFTY_LISTING_CLEANUP_DRY_RUN_VERIFY_REPORT", "listing_cleanup_queue_apply_preflight_command_missing_verify_env"),
        ("LOFTY_LISTING_CLEANUP_APPLY_PREFLIGHT_REPORT", "listing_cleanup_queue_apply_preflight_command_missing_report_env"),
        ("LOFTY_LISTING_CLEANUP_APPLY_DIGEST", "listing_cleanup_queue_apply_preflight_command_missing_digest_env"),
    ):
        if token not in preflight_command:
            issues.append(issue(code, preflight_command or "missing"))
    ready_count = compact_count(payload.get("ready_listing_cleanup_count"))
    blocked_count = compact_count(payload.get("blocked_count"))
    property_count = compact_count(payload.get("property_count"))
    record_count = compact_count(payload.get("record_count"))
    dry_run_command_count = compact_count(payload.get("dry_run_command_count"))
    live_command_count = compact_count(payload.get("live_apply_command_requires_explicit_approval_count"))
    candidate_approval_copy_count = compact_count(payload.get("candidate_update_approval_copy_command_requires_current_rent_roll_count"))
    candidate_approval_digest = str(payload.get("candidate_update_approval_idempotency_digest") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", candidate_approval_digest):
        issues.append(issue("listing_cleanup_queue_candidate_approval_digest_invalid", candidate_approval_digest or "missing"))
    if payload.get("candidate_update_approval_copy_requires_current_rent_roll") is not True:
        issues.append(
            issue(
                "listing_cleanup_queue_candidate_approval_copy_not_rent_roll_gated",
                str(payload.get("candidate_update_approval_copy_requires_current_rent_roll")),
            )
        )
    if payload.get("candidate_update_approval_copy_requires_explicit_approval") is not True:
        issues.append(
            issue(
                "listing_cleanup_queue_candidate_approval_copy_not_approval_gated",
                str(payload.get("candidate_update_approval_copy_requires_explicit_approval")),
            )
        )
    expected_candidate_env = {
        "candidate_update_approval_copy_approval_env_var": "LOFTY_LISTING_UPDATE_APPROVAL_COPY_APPROVED",
        "candidate_update_approval_copy_approval_required_value": "1",
        "candidate_update_approval_copy_current_rent_roll_env_var": "LOFTY_LISTING_UPDATE_APPROVAL_CURRENT_RENT_ROLL_CONFIRMED",
        "candidate_update_approval_copy_current_rent_roll_required_value": "1",
        "candidate_update_approval_copy_digest_env_var": "LOFTY_LISTING_UPDATE_APPROVAL_COPY_DIGEST",
    }
    for key, expected_value in expected_candidate_env.items():
        if str(payload.get(key) or "") != expected_value:
            issues.append(issue(f"listing_cleanup_queue_{key}_unexpected", str(payload.get(key) or "missing")))
    if candidate_approval_digest and str(payload.get("candidate_update_approval_copy_digest_required_value") or "") != candidate_approval_digest:
        issues.append(
            issue(
                "listing_cleanup_queue_candidate_approval_copy_digest_value_mismatch",
                str(payload.get("candidate_update_approval_copy_digest_required_value") or "missing"),
            )
        )
    if property_count != ready_count + blocked_count:
        issues.append(issue("listing_cleanup_queue_property_count_mismatch", f"{property_count}!={ready_count}+{blocked_count}"))
    if record_count and record_count != property_count:
        issues.append(issue("listing_cleanup_queue_record_count_mismatch", f"{record_count}!={property_count}"))
    if dry_run_command_count != ready_count:
        issues.append(issue("listing_cleanup_queue_dry_run_command_count_mismatch", f"{dry_run_command_count}!={ready_count}"))
    if live_command_count != ready_count:
        issues.append(issue("listing_cleanup_queue_live_command_count_mismatch", f"{live_command_count}!={ready_count}"))
    manifest = payload.get("ready_cleanup_manifest")
    if not isinstance(manifest, list):
        issues.append(issue("listing_cleanup_queue_ready_manifest_invalid", type(manifest).__name__))
        manifest = []
    if len(manifest) != ready_count:
        issues.append(issue("listing_cleanup_queue_ready_manifest_count_mismatch", f"{len(manifest)}!={ready_count}"))
    for index, record in enumerate(manifest):
        if not isinstance(record, dict):
            issues.append(issue("listing_cleanup_queue_ready_manifest_record_invalid", f"{index}:{type(record).__name__}"))
            continue
        updates_md = str(record.get("updates_md") or "").replace("\\", "/")
        if "/Dropbox/Real Estate/" not in updates_md or not is_canonical_updates_path(updates_md):
            issues.append(issue("listing_cleanup_queue_ready_manifest_updates_path_noncanonical", f"{index}:{updates_md or 'missing'}"))
        if record.get("financial_summary_enriched") is not True:
            issues.append(issue("listing_cleanup_queue_ready_manifest_financial_summary_not_enriched", f"{index}:{record.get('property_name') or '?'}"))
        if not re.fullmatch(r"[0-9a-f]{64}", str(record.get("latest_listing_update_sha256") or "")):
            issues.append(issue("listing_cleanup_queue_ready_manifest_latest_digest_invalid", f"{index}:{record.get('property_name') or '?'}"))
    candidate_manifest = payload.get("candidate_update_approval_manifest")
    if not isinstance(candidate_manifest, list):
        issues.append(issue("listing_cleanup_queue_candidate_approval_manifest_invalid", type(candidate_manifest).__name__))
        candidate_manifest = []
    if len(candidate_manifest) != candidate_approval_copy_count:
        issues.append(
            issue(
                "listing_cleanup_queue_candidate_approval_manifest_count_mismatch",
                f"{len(candidate_manifest)}!={candidate_approval_copy_count}",
            )
        )
    for index, record in enumerate(candidate_manifest):
        if not isinstance(record, dict):
            issues.append(issue("listing_cleanup_queue_candidate_approval_manifest_record_invalid", f"{index}:{type(record).__name__}"))
            continue
        updates_md = str(record.get("updates_md") or "").replace("\\", "/")
        approval_target = str(record.get("candidate_update_approval_target") or "").replace("\\", "/")
        candidate = str(record.get("candidate_update_candidate") or "")
        candidate_sha = str(record.get("candidate_update_candidate_sha256") or "")
        if "/Dropbox/Real Estate/" not in updates_md or not is_canonical_updates_path(updates_md):
            issues.append(issue("listing_cleanup_queue_candidate_approval_manifest_updates_path_noncanonical", f"{index}:{updates_md or 'missing'}"))
        if "/Dropbox/Real Estate/" not in approval_target or "/00 - README & Property Snapshot/" not in approval_target:
            issues.append(
                issue(
                    "listing_cleanup_queue_candidate_approval_manifest_target_noncanonical",
                    f"{index}:{approval_target or 'missing'}",
                )
            )
        if not candidate:
            issues.append(issue("listing_cleanup_queue_candidate_approval_manifest_candidate_missing", f"{index}:{record.get('property_name') or '?'}"))
        if not re.fullmatch(r"[0-9a-f]{64}", candidate_sha):
            issues.append(issue("listing_cleanup_queue_candidate_approval_manifest_sha_invalid", f"{index}:{record.get('property_name') or '?'}"))
    records = payload.get("records")
    if not isinstance(records, list):
        issues.append(issue("listing_cleanup_queue_records_invalid", type(records).__name__))
        records = []
    ready_records = 0
    blocked_records = 0
    candidate_approval_copy_records = 0
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            issues.append(issue("listing_cleanup_queue_record_invalid", f"{index}:{type(record).__name__}"))
            continue
        if record.get("mutates_lofty_listing") is not False:
            issues.append(issue("listing_cleanup_queue_record_mutates_lofty", f"{index}:{record.get('property_name') or '?'}"))
        if record.get("sends_owner_email") is not False:
            issues.append(issue("listing_cleanup_queue_record_sends_email", f"{index}:{record.get('property_name') or '?'}"))
        status = str(record.get("status") or "")
        candidate_approval_command = str(record.get("candidate_update_approval_copy_command_requires_current_rent_roll_and_explicit_approval") or "")
        if candidate_approval_command:
            candidate_approval_copy_records += 1
            if status != "blocked_unsafe_latest_update":
                issues.append(issue("listing_cleanup_queue_candidate_approval_record_status_unexpected", f"{index}:{status or '?'}"))
            if record.get("excluded_from_live_cleanup") is not False:
                issues.append(issue("listing_cleanup_queue_candidate_approval_record_excluded", f"{index}:{record.get('property_name') or '?'}"))
            if record.get("candidate_update_candidate_exists") is not True:
                issues.append(issue("listing_cleanup_queue_candidate_approval_record_candidate_missing", f"{index}:{record.get('property_name') or '?'}"))
            if record.get("candidate_update_approval_target_exists") is not False:
                issues.append(issue("listing_cleanup_queue_candidate_approval_record_target_exists", f"{index}:{record.get('property_name') or '?'}"))
            if compact_count(record.get("candidate_update_quality_issue_count")):
                issues.append(issue("listing_cleanup_queue_candidate_approval_record_quality_nonzero", f"{index}:{record.get('property_name') or '?'}"))
            if compact_count(record.get("candidate_financial_gate_issue_count")):
                issues.append(issue("listing_cleanup_queue_candidate_approval_record_financial_gate_nonzero", f"{index}:{record.get('property_name') or '?'}"))
            for token, code in (
                ("test -d", "listing_cleanup_queue_candidate_approval_command_missing_parent_check"),
                ("test ! -e", "listing_cleanup_queue_candidate_approval_command_missing_idempotency_check"),
                ("cp --", "listing_cleanup_queue_candidate_approval_command_missing_copy"),
            ):
                if token not in candidate_approval_command:
                    issues.append(issue(code, f"{index}:{record.get('property_name') or '?'}"))
            if "publish" in candidate_approval_command or "--skip-send" in candidate_approval_command:
                issues.append(issue("listing_cleanup_queue_candidate_approval_command_not_copy_only", f"{index}:{record.get('property_name') or '?'}"))
        if status == "ready_listing_cleanup":
            ready_records += 1
            if "--skip-send" not in str(record.get("dry_run_command") or ""):
                issues.append(issue("listing_cleanup_queue_ready_dry_run_missing_skip_send", f"{index}:{record.get('property_name') or '?'}"))
            if "--dry-run" not in str(record.get("dry_run_command") or ""):
                issues.append(issue("listing_cleanup_queue_ready_dry_run_missing_dry_run", f"{index}:{record.get('property_name') or '?'}"))
            live_command = str(record.get("live_apply_command_requires_explicit_approval") or "")
            if "--skip-send" not in live_command:
                issues.append(issue("listing_cleanup_queue_ready_live_command_missing_skip_send", f"{index}:{record.get('property_name') or '?'}"))
            if "--dry-run" in live_command:
                issues.append(issue("listing_cleanup_queue_ready_live_command_contains_dry_run", f"{index}:{record.get('property_name') or '?'}"))
            if record.get("financial_summary_enriched") is not True:
                issues.append(issue("listing_cleanup_queue_ready_financial_summary_not_enriched", f"{index}:{record.get('property_name') or '?'}"))
        elif status.startswith("blocked"):
            blocked_records += 1
    if ready_records != ready_count:
        issues.append(issue("listing_cleanup_queue_ready_record_count_mismatch", f"{ready_records}!={ready_count}"))
    if blocked_records != blocked_count:
        issues.append(issue("listing_cleanup_queue_blocked_record_count_mismatch", f"{blocked_records}!={blocked_count}"))
    if candidate_approval_copy_records != candidate_approval_copy_count:
        issues.append(
            issue(
                "listing_cleanup_queue_candidate_approval_copy_count_mismatch",
                f"{candidate_approval_copy_records}!={candidate_approval_copy_count}",
            )
        )
    return issues


def validate_listing_cleanup_apply_preflight(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed"}))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "issue_count",
                "queue_report",
                "dry_run_verify_report",
                "monthly_readiness_report",
                "monthly_readiness_status",
                "monthly_readiness_blocker_count",
                "monthly_readiness_owner_email_allowed",
                "expected_digest",
                "queue_digest",
                "dry_run_verify_digest",
                "ready_listing_cleanup_count",
                "verified_record_count",
                "dry_run_only",
                "sends_owner_email",
                "mutates_lofty_listing",
                "listing_update_scope",
            },
        )
    )
    payload_issues = {str(item) for item in (payload.get("issues") or [])}
    safe_monthly_readiness_block = (
        payload.get("status") == "review"
        and payload_issues
        and payload_issues <= {"monthly_readiness_not_clean", "monthly_readiness_has_publish_email_hold"}
        and compact_count(payload.get("issue_count")) == len(payload_issues)
        and payload.get("monthly_readiness_status") == "review"
        and compact_count(payload.get("monthly_readiness_blocker_count")) > 0
        and payload.get("monthly_readiness_owner_email_allowed") is False
    )
    if payload.get("status") != "ok" and not safe_monthly_readiness_block:
        issues.append(issue("listing_cleanup_apply_preflight_not_ok", str(payload.get("status"))))
    if compact_count(payload.get("issue_count")) and not safe_monthly_readiness_block:
        issues.append(issue("listing_cleanup_apply_preflight_issue_count_nonzero", str(payload.get("issue_count"))))
    expected_digest = str(payload.get("expected_digest") or "")
    queue_digest = str(payload.get("queue_digest") or "")
    verify_digest = str(payload.get("dry_run_verify_digest") or "")
    for label, digest in (
        ("expected", expected_digest),
        ("queue", queue_digest),
        ("dry_run_verify", verify_digest),
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            issues.append(issue(f"listing_cleanup_apply_preflight_{label}_digest_invalid", digest or "missing"))
    if expected_digest and queue_digest and expected_digest != queue_digest:
        issues.append(issue("listing_cleanup_apply_preflight_queue_digest_mismatch", f"{queue_digest}!={expected_digest}"))
    if expected_digest and verify_digest and expected_digest != verify_digest:
        issues.append(issue("listing_cleanup_apply_preflight_verify_digest_mismatch", f"{verify_digest}!={expected_digest}"))
    ready_count = compact_count(payload.get("ready_listing_cleanup_count"))
    verified_count = compact_count(payload.get("verified_record_count"))
    if verified_count != ready_count:
        issues.append(issue("listing_cleanup_apply_preflight_verified_count_mismatch", f"{verified_count}!={ready_count}"))
    if payload.get("dry_run_only") is not True:
        issues.append(issue("listing_cleanup_apply_preflight_not_dry_run_only", str(payload.get("dry_run_only"))))
    if payload.get("sends_owner_email") is not False:
        issues.append(issue("listing_cleanup_apply_preflight_send_risk", str(payload.get("sends_owner_email"))))
    if payload.get("mutates_lofty_listing") is not False:
        issues.append(issue("listing_cleanup_apply_preflight_mutation_risk", str(payload.get("mutates_lofty_listing"))))
    if payload.get("listing_update_scope") != "full_history":
        issues.append(issue("listing_cleanup_apply_preflight_scope_not_supported", str(payload.get("listing_update_scope"))))
    if Path(str(payload.get("queue_report") or "")).name != "lofty_listing_update_cleanup_queue.json":
        issues.append(issue("listing_cleanup_apply_preflight_queue_report_unexpected", str(payload.get("queue_report") or "missing")))
    if Path(str(payload.get("dry_run_verify_report") or "")).name != "lofty_listing_cleanup_dry_run_verify.json":
        issues.append(issue("listing_cleanup_apply_preflight_verify_report_unexpected", str(payload.get("dry_run_verify_report") or "missing")))
    return issues


def validate_monthly_guarded_apply(payload: dict) -> list[dict]:
    issues = []
    issues.extend(
        validate_status(
            payload,
            allowed={
                "ok",
                "review",
                "failed",
                "skipped_disabled_dry_run",
                "failed_required_dry_run_disabled",
                "failed_timeout_dry_run",
                "review_dry_run",
                "ok_dry_run",
                "ok_not_applied",
                "review_not_applied",
                "failed_timeout_not_applied",
                "failed_required_not_applied",
                "failed_not_required_not_applied",
                "skipped_missing_script",
            },
        )
    )
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "run_month",
                "apply",
                "record_count",
                "records",
                "excluded_property_count",
                "excluded_property_names",
                "externally_excluded_property_count",
                "externally_excluded_property_names",
                "skipped_closed_property_count",
                "skipped_closed_property_names",
                "excluded_total_property_count",
                "excluded_total_property_names",
            },
        )
    )
    apply_enabled = payload.get("apply") is True
    records = payload.get("records")
    if not isinstance(records, list):
        issues.append(issue("invalid_guarded_apply_records", "not_list"))
        records = []
    if compact_count(payload.get("record_count")) != len(records):
        issues.append(issue("guarded_apply_record_count_mismatch", f"{compact_count(payload.get('record_count'))}!={len(records)}"))
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            issues.append(issue("invalid_guarded_apply_record", f"index={index}"))
            continue
        property_name = str(record.get("property_name") or record.get("property_path") or index)
        updates_md = str(record.get("updates_md") or "")
        financials_md = str(record.get("financials_md") or "")
        if updates_md and not is_canonical_updates_path(updates_md):
            issues.append(issue("guarded_apply_noncanonical_updates_path", property_name))
        if financials_md and not is_canonical_financials_path(financials_md):
            issues.append(issue("guarded_apply_noncanonical_financials_path", property_name))
        if LEGACY_PUBLIC_UPDATES_PATH in updates_md or LEGACY_PUBLIC_FINANCIALS_PATH in financials_md:
            issues.append(issue("guarded_apply_legacy_public_path", property_name))
        update_state = record.get("updates") if isinstance(record.get("updates"), dict) else {}
        financial_state = record.get("financials") if isinstance(record.get("financials"), dict) else {}
        for label, state in (("updates", update_state), ("financials", financial_state)):
            state_status = str(state.get("status") or "")
            if apply_enabled and state_status.endswith("failed"):
                issues.append(issue("guarded_apply_apply_with_failed_guard", f"{property_name}:{label}:{state_status}"))
            if state_status == "applied" and apply_enabled is not True:
                issues.append(issue("guarded_apply_applied_in_non_apply_run", f"{property_name}:{label}"))
        approved_draft = str(financial_state.get("approved_draft") or "")
        approved_draft_path = Path(approved_draft) if approved_draft else None
        if approved_draft_path and approved_draft_path.is_file():
            approved_text = approved_draft_path.read_text(encoding="utf-8", errors="replace")
            approved_lower = approved_text.lower()
            generated_markers = (
                "review before investor email/publish" in approved_lower
                or "no reviewed markdown `financials.md` source existed yet" in approved_lower
                or "\n## ledger summary" in approved_lower
                or approved_lower.startswith("## ledger summary")
            )
            if generated_markers and str(financial_state.get("status") or "") != "approved_financials_unreviewed":
                issues.append(issue("guarded_apply_generated_approved_financials_not_blocked", f"{property_name}:{approved_draft}"))
        if str(financial_state.get("status") or "") == "approved_financials_unreviewed" and not financial_state.get("approved_financials_quality_issues"):
            issues.append(issue("guarded_apply_unreviewed_financials_missing_quality_issues", property_name))
    exclusion_counts, exclusion_mismatches = guarded_apply_exclusion_counts({"records": records})
    for mismatch in exclusion_mismatches:
        issues.append(issue("guarded_apply_exclusion_status_mismatch", mismatch))
    expected_external = exclusion_counts["external"]
    expected_skipped = exclusion_counts["skipped"]
    expected_total = exclusion_counts["total"]
    if "externally_excluded_property_count" in payload:
        actual_external = compact_count(payload.get("externally_excluded_property_count"))
    else:
        actual_external = compact_count(payload.get("excluded_property_count"))
    actual_skipped = compact_count(payload.get("skipped_closed_property_count"))
    if "excluded_total_property_count" in payload:
        actual_total = compact_count(payload.get("excluded_total_property_count"))
    else:
        actual_total = compact_count(payload.get("excluded_property_count"))
    if actual_external != expected_external:
        issues.append(issue("guarded_apply_excluded_count_mismatch", f"reported={actual_external},computed={expected_external}"))
    if actual_skipped != expected_skipped:
        issues.append(issue("guarded_apply_skipped_closed_count_mismatch", f"reported={actual_skipped},computed={expected_skipped}"))
    if actual_total != expected_total:
        issues.append(issue("guarded_apply_excluded_total_count_mismatch", f"reported={actual_total},computed={expected_total}"))
    if actual_external + actual_skipped != actual_total:
        issues.append(issue("guarded_apply_excluded_components_mismatch", f"external={actual_external},skipped={actual_skipped},total={actual_total}"))
    name_checks = (
        ("externally_excluded_property_names", actual_external, "guarded_apply_excluded_name_count_mismatch"),
        ("skipped_closed_property_names", actual_skipped, "guarded_apply_skipped_closed_name_count_mismatch"),
        ("excluded_total_property_names", actual_total, "guarded_apply_excluded_total_name_count_mismatch"),
    )
    for key, expected_count, code in name_checks:
        names = payload.get(key)
        if isinstance(names, list):
            if len(names) != expected_count:
                issues.append(issue(code, f"{key}={len(names)},count={expected_count}"))
            if len(set(str(name) for name in names)) != len(names):
                issues.append(issue("guarded_apply_duplicate_exclusion_name", key))
    return issues


def validate_monthly_live_capture(payload: dict, *, expected_doc: str, canonical_suffix: str, command_token: str) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed"}))
    issues.extend(require_keys(payload, {"status", "apply", "issue_count", "records"}))
    blocker_prefix = "live_update" if expected_doc == "UPDATES" else "live_financial"
    expected_semantics = "authenticated_read_and_guard_registration_only"
    if payload.get("mutates_lofty_listing") is not False:
        issues.append(issue(f"{blocker_prefix}_capture_mutates_lofty_listing_not_false", str(payload.get("mutates_lofty_listing"))))
    if payload.get("mutates_external_system") is not False:
        issues.append(issue(f"{blocker_prefix}_capture_mutates_external_system_not_false", str(payload.get("mutates_external_system"))))
    if compact_count(payload.get("external_mutation_count")) != 0:
        issues.append(issue(f"{blocker_prefix}_capture_external_mutation_count_nonzero", str(payload.get("external_mutation_count"))))
    if payload.get("capture_semantics") != expected_semantics:
        issues.append(issue(f"{blocker_prefix}_capture_semantics_mismatch", str(payload.get("capture_semantics") or "missing")))
    capture_contract = payload.get("capture_contract") if isinstance(payload.get("capture_contract"), dict) else {}
    if capture_contract.get("mutates_lofty_listing") is not False:
        issues.append(issue(f"{blocker_prefix}_capture_contract_mutates_lofty_listing_not_false", str(capture_contract.get("mutates_lofty_listing"))))
    if capture_contract.get("mutates_external_system") is not False:
        issues.append(issue(f"{blocker_prefix}_capture_contract_mutates_external_system_not_false", str(capture_contract.get("mutates_external_system"))))
    if compact_count(capture_contract.get("external_mutation_count")) != 0:
        issues.append(issue(f"{blocker_prefix}_capture_contract_external_mutation_count_nonzero", str(capture_contract.get("external_mutation_count"))))
    if capture_contract.get("capture_semantics") != expected_semantics:
        issues.append(issue(f"{blocker_prefix}_capture_contract_semantics_mismatch", str(capture_contract.get("capture_semantics") or "missing")))
    if compact_count(payload.get("issue_count")) < 0:
        issues.append(issue("invalid_live_capture_issue_count", str(payload.get("issue_count"))))
    records = payload.get("records")
    if not isinstance(records, list):
        issues.append(issue("invalid_live_capture_records", "not_list"))
        records = []
    computed_status_counts: dict[str, int] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            issues.append(issue("invalid_live_capture_record", f"index={index}"))
            continue
        record_status = str(record.get("status") or "unknown")
        computed_status_counts[record_status] = computed_status_counts.get(record_status, 0) + 1
        property_name = str(record.get("property_name") or index)
        target = str(record.get("updates_md") or record.get("financials_md") or "")
        canonical_target = is_canonical_updates_path(target) if expected_doc == "UPDATES" else is_canonical_financials_path(target)
        if not canonical_target:
            issues.append(issue(f"live_{expected_doc}_capture_noncanonical_target", property_name))
        legacy_doc_path = "/Public/" + expected_doc + "/"
        if legacy_doc_path in target or LEGACY_PUBLIC_UPDATES_PATH in target or LEGACY_PUBLIC_FINANCIALS_PATH in target:
            issues.append(issue(f"live_{expected_doc}_capture_legacy_public_path", property_name))
        next_action_file = str(record.get("next_action_file") or record.get("snapshot_path") or "")
        if expected_doc.upper() not in next_action_file.upper():
            issues.append(issue(f"live_{expected_doc}_capture_missing_doc_snapshot", property_name))
        next_action_command = str(record.get("next_action_command") or "")
        if record.get("status") == "planned" and command_token not in next_action_command:
            issues.append(issue(f"live_{expected_doc}_capture_missing_guard_command", property_name))
    reported_status_counts = payload.get("record_status_counts")
    if reported_status_counts is not None:
        if not isinstance(reported_status_counts, dict):
            issues.append(issue(f"{blocker_prefix}_capture_invalid_record_status_counts", type(reported_status_counts).__name__))
        elif {str(key): compact_count(value) for key, value in reported_status_counts.items()} != computed_status_counts:
            issues.append(issue(f"{blocker_prefix}_capture_record_status_count_mismatch", f"reported={reported_status_counts},computed={computed_status_counts}"))
    target_count = compact_count(payload.get("target_count"))
    check_ok_count = compact_count(payload.get("check_ok_count"))
    if "unverified_count" in payload:
        expected_unverified = max(0, target_count - check_ok_count)
        if compact_count(payload.get("unverified_count")) != expected_unverified:
            issues.append(issue(f"{blocker_prefix}_capture_unverified_count_mismatch", f"{payload.get('unverified_count')}!={expected_unverified}"))
    if "planned_count" in payload:
        expected_planned = computed_status_counts.get("planned", 0)
        if compact_count(payload.get("planned_count")) != expected_planned:
            issues.append(issue(f"{blocker_prefix}_capture_planned_count_mismatch", f"{payload.get('planned_count')}!={expected_planned}"))
    if "blocked_count" in payload:
        expected_blocked = sum(count for status, count in computed_status_counts.items() if status.startswith("blocked_"))
        if compact_count(payload.get("blocked_count")) != expected_blocked:
            issues.append(issue(f"{blocker_prefix}_capture_blocked_count_mismatch", f"{payload.get('blocked_count')}!={expected_blocked}"))
    if "mismatch_count" in payload:
        expected_mismatch = computed_status_counts.get("needs_reconcile", 0)
        if compact_count(payload.get("mismatch_count")) != expected_mismatch:
            issues.append(issue(f"{blocker_prefix}_capture_mismatch_count_mismatch", f"{payload.get('mismatch_count')}!={expected_mismatch}"))
    review_blockers = payload.get("review_blockers")
    review_blocker_count = payload.get("review_blocker_count")
    if review_blockers is not None or review_blocker_count is not None:
        if not isinstance(review_blockers, list):
            issues.append(issue(f"{blocker_prefix}_capture_invalid_review_blockers", type(review_blockers).__name__))
            review_blockers = []
        if compact_count(review_blocker_count) != len(review_blockers):
            issues.append(issue(f"{blocker_prefix}_capture_review_blocker_count_mismatch", f"{review_blocker_count}!={len(review_blockers)}"))
        expected_summary = str(review_blockers[0]) if review_blockers else None
        if payload.get("review_blocker_summary") != expected_summary:
            issues.append(issue(f"{blocker_prefix}_capture_review_blocker_summary_mismatch", f"{payload.get('review_blocker_summary')}!={expected_summary}"))
        if payload.get("status") == "ok" and review_blockers:
            issues.append(issue(f"{blocker_prefix}_capture_ok_has_review_blockers", str(review_blockers[:3])))
        if payload.get("status") == "review" and not review_blockers:
            issues.append(issue(f"{blocker_prefix}_capture_review_missing_blockers", "empty"))
    return issues


def validate_monthly_live_update_capture(payload: dict) -> list[dict]:
    return validate_monthly_live_capture(
        payload,
        expected_doc="UPDATES",
        canonical_suffix="Public/00 - README & Property Snapshot/UPDATES.md",
        command_token="lofty-updates-guard.py",
    )


def validate_monthly_live_financial_capture(payload: dict) -> list[dict]:
    return validate_monthly_live_capture(
        payload,
        expected_doc="FINANCIALS",
        canonical_suffix="Public/00 - README & Property Snapshot/FINANCIALS.md",
        command_token="lofty-live-file-guard.py",
    )


def validate_lofty_financial_patch_readiness(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed"}))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "issue_count",
                "mutates_lofty_listing",
                "sends_owner_email",
                "property_count",
                "ready_financial_patch_count",
                "guard_reconcile_required_count",
                "guard_reconcile_required_field_count",
                "blocked_empty_patch_count",
                "blocked_empty_patch_candidate_source_count",
                "blocked_empty_patch_candidate_quality_issue_count",
                "blocked_count",
                "field_count_total",
                "financial_patch_readiness_digest",
                "guard_reconcile_csv",
                "blocked_empty_patch_csv",
                "record_status_counts",
                "records",
                "next_action",
            },
        )
    )
    if payload.get("mutates_lofty_listing") is not False:
        issues.append(issue("lofty_financial_patch_readiness_mutates_lofty", str(payload.get("mutates_lofty_listing"))))
    if payload.get("sends_owner_email") is not False:
        issues.append(issue("lofty_financial_patch_readiness_sends_owner_email", str(payload.get("sends_owner_email"))))
    digest = str(payload.get("financial_patch_readiness_digest") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        issues.append(issue("lofty_financial_patch_readiness_invalid_digest", digest or "missing"))
    records = payload.get("records")
    if not isinstance(records, list):
        issues.append(issue("lofty_financial_patch_readiness_records_invalid", type(records).__name__))
        records = []
    computed_status_counts: dict[str, int] = {}
    computed_field_total = 0
    computed_guard_field_count = 0
    computed_empty_candidate_source_count = 0
    computed_empty_candidate_quality_issue_count = 0
    for record in records:
        if not isinstance(record, dict):
            issues.append(issue("lofty_financial_patch_readiness_record_invalid", type(record).__name__))
            continue
        status = str(record.get("status") or "")
        computed_status_counts[status] = computed_status_counts.get(status, 0) + 1
        field_count = compact_count(record.get("field_count"))
        computed_field_total += max(field_count, 0)
        if status == "patch_ready_guard_reconcile_required":
            computed_guard_field_count += max(field_count, 0)
        if status == "blocked_empty_patch":
            candidate_source = str(record.get("candidate_financial_source") or "").strip()
            candidate_sha = str(record.get("candidate_financial_sha256") or "").strip()
            candidate_quality_issues = record.get("candidate_financial_quality_issues")
            approval_target_exists = record.get("approval_target_exists")
            approval_command = str(record.get("approval_copy_command_requires_current_rent_roll_and_explicit_approval") or "")
            if candidate_source:
                computed_empty_candidate_source_count += 1
            else:
                issues.append(issue("lofty_financial_patch_readiness_empty_candidate_source_missing", str(record.get("property_name") or "unknown")))
            if not re.fullmatch(r"[0-9a-f]{64}", candidate_sha):
                issues.append(issue("lofty_financial_patch_readiness_empty_candidate_sha_invalid", str(record.get("property_name") or "unknown")))
            candidate_has_quality_issues = False
            if not isinstance(candidate_quality_issues, list):
                issues.append(issue("lofty_financial_patch_readiness_empty_candidate_quality_invalid", str(record.get("property_name") or "unknown")))
            else:
                computed_empty_candidate_quality_issue_count += len(candidate_quality_issues)
                candidate_has_quality_issues = bool(candidate_quality_issues)
                if candidate_quality_issues:
                    issues.append(
                        issue(
                            "lofty_financial_patch_readiness_empty_candidate_quality_nonzero",
                            f"{record.get('property_name') or 'unknown'}:{len(candidate_quality_issues)}",
                        )
                    )
            if approval_target_exists not in {True, False}:
                issues.append(issue("lofty_financial_patch_readiness_empty_approval_target_invalid", str(record.get("property_name") or "unknown")))
            if candidate_has_quality_issues and approval_command:
                issues.append(issue("lofty_financial_patch_readiness_empty_approval_command_with_quality_issues", str(record.get("property_name") or "unknown")))
            if not candidate_has_quality_issues and approval_target_exists is False:
                if "cp --" not in approval_command or "test ! -e" not in approval_command or "test -d" not in approval_command:
                    issues.append(issue("lofty_financial_patch_readiness_empty_approval_command_missing", str(record.get("property_name") or "unknown")))
            if not candidate_has_quality_issues and approval_target_exists is True and approval_command:
                issues.append(issue("lofty_financial_patch_readiness_empty_approval_command_target_exists", str(record.get("property_name") or "unknown")))
            if approval_command and ("cmp -s" in approval_command or "|| cp" in approval_command):
                issues.append(issue("lofty_financial_patch_readiness_empty_approval_command_can_overwrite", str(record.get("property_name") or "unknown")))
    if compact_count(payload.get("property_count")) != len(records):
        issues.append(issue("lofty_financial_patch_readiness_property_count_mismatch", f"{payload.get('property_count')}!={len(records)}"))
    for key, expected in computed_status_counts.items():
        if compact_count((payload.get("record_status_counts") if isinstance(payload.get("record_status_counts"), dict) else {}).get(key)) != expected:
            issues.append(issue("lofty_financial_patch_readiness_status_count_mismatch", f"{key}:{expected}"))
    ready_count = computed_status_counts.get("ready_financial_patch", 0)
    guard_count = computed_status_counts.get("patch_ready_guard_reconcile_required", 0)
    blocked_empty_count = computed_status_counts.get("blocked_empty_patch", 0)
    blocked_count = sum(count for status, count in computed_status_counts.items() if status.startswith("blocked_") or status == "patch_ready_guard_reconcile_required")
    if compact_count(payload.get("ready_financial_patch_count")) != ready_count:
        issues.append(issue("lofty_financial_patch_readiness_ready_count_mismatch", f"{payload.get('ready_financial_patch_count')}!={ready_count}"))
    if compact_count(payload.get("guard_reconcile_required_count")) != guard_count:
        issues.append(issue("lofty_financial_patch_readiness_guard_count_mismatch", f"{payload.get('guard_reconcile_required_count')}!={guard_count}"))
    if compact_count(payload.get("blocked_empty_patch_count")) != blocked_empty_count:
        issues.append(issue("lofty_financial_patch_readiness_empty_count_mismatch", f"{payload.get('blocked_empty_patch_count')}!={blocked_empty_count}"))
    if compact_count(payload.get("blocked_empty_patch_candidate_source_count")) != computed_empty_candidate_source_count:
        issues.append(
            issue(
                "lofty_financial_patch_readiness_empty_candidate_source_count_mismatch",
                f"{payload.get('blocked_empty_patch_candidate_source_count')}!={computed_empty_candidate_source_count}",
            )
        )
    if compact_count(payload.get("blocked_empty_patch_candidate_quality_issue_count")) != computed_empty_candidate_quality_issue_count:
        issues.append(
            issue(
                "lofty_financial_patch_readiness_empty_candidate_quality_count_mismatch",
                f"{payload.get('blocked_empty_patch_candidate_quality_issue_count')}!={computed_empty_candidate_quality_issue_count}",
            )
        )
    if compact_count(payload.get("blocked_count")) != blocked_count:
        issues.append(issue("lofty_financial_patch_readiness_blocked_count_mismatch", f"{payload.get('blocked_count')}!={blocked_count}"))
    if compact_count(payload.get("field_count_total")) != computed_field_total:
        issues.append(issue("lofty_financial_patch_readiness_field_total_mismatch", f"{payload.get('field_count_total')}!={computed_field_total}"))
    if compact_count(payload.get("guard_reconcile_required_field_count")) != computed_guard_field_count:
        issues.append(
            issue(
                "lofty_financial_patch_readiness_guard_field_count_mismatch",
                f"{payload.get('guard_reconcile_required_field_count')}!={computed_guard_field_count}",
            )
        )
    guard_csv = str(payload.get("guard_reconcile_csv") or "")
    empty_csv = str(payload.get("blocked_empty_patch_csv") or "")
    next_action = str(payload.get("next_action") or "")
    if guard_count > 0 and "guard-reconcile.csv" not in guard_csv:
        issues.append(issue("lofty_financial_patch_readiness_guard_csv_missing", guard_csv or "missing"))
    if blocked_empty_count > 0 and "blocked-empty-patch.csv" not in empty_csv:
        issues.append(issue("lofty_financial_patch_readiness_empty_csv_missing", empty_csv or "missing"))
    if guard_count > 0 and guard_csv and guard_csv not in next_action:
        issues.append(issue("lofty_financial_patch_readiness_next_action_missing_guard_csv", guard_csv))
    for csv_path, expected_count, missing_code, mismatch_code in (
        (
            guard_csv,
            guard_count,
            "lofty_financial_patch_readiness_guard_csv_file_missing",
            "lofty_financial_patch_readiness_guard_csv_row_count_mismatch",
        ),
        (
            empty_csv,
            blocked_empty_count,
            "lofty_financial_patch_readiness_empty_csv_file_missing",
            "lofty_financial_patch_readiness_empty_csv_row_count_mismatch",
        ),
    ):
        if not csv_path:
            continue
        row_count, read_issue = csv_data_row_count(Path(csv_path))
        if read_issue:
            issues.append(issue(missing_code, f"{csv_path}:{read_issue}"))
        elif row_count != expected_count:
            issues.append(issue(mismatch_code, f"{csv_path}:{row_count}!={expected_count}"))
    if blocked_empty_count > 0 and empty_csv:
        rows, fieldnames, read_issue = csv_rows(Path(empty_csv))
        if not read_issue:
            required_empty_csv_fields = {
                "candidate_financial_source",
                "candidate_financial_sha256",
                "candidate_financial_quality_issues",
                "approval_target_exists",
                "approval_copy_command_requires_current_rent_roll_and_explicit_approval",
            }
            missing_fields = sorted(required_empty_csv_fields - set(fieldnames))
            if missing_fields:
                issues.append(issue("lofty_financial_patch_readiness_empty_csv_missing_candidate_fields", ",".join(missing_fields)))
            for index, row in enumerate(rows, start=1):
                if not str(row.get("candidate_financial_source") or "").strip():
                    issues.append(issue("lofty_financial_patch_readiness_empty_csv_candidate_source_missing", str(index)))
                if not re.fullmatch(r"[0-9a-f]{64}", str(row.get("candidate_financial_sha256") or "").strip()):
                    issues.append(issue("lofty_financial_patch_readiness_empty_csv_candidate_sha_invalid", str(index)))
                candidate_quality_text = str(row.get("candidate_financial_quality_issues") or "").strip()
                csv_candidate_has_quality_issues = candidate_quality_text not in {"[]", ""}
                if csv_candidate_has_quality_issues:
                    issues.append(issue("lofty_financial_patch_readiness_empty_csv_candidate_quality_nonzero", str(index)))
                approval_target_text = str(row.get("approval_target_exists") or "").strip()
                if approval_target_text not in {"True", "False"}:
                    issues.append(issue("lofty_financial_patch_readiness_empty_csv_approval_target_invalid", str(index)))
                approval_command = str(row.get("approval_copy_command_requires_current_rent_roll_and_explicit_approval") or "")
                if csv_candidate_has_quality_issues and approval_command:
                    issues.append(issue("lofty_financial_patch_readiness_empty_csv_approval_command_with_quality_issues", str(index)))
                if not csv_candidate_has_quality_issues and approval_target_text == "False" and (
                    "cp --" not in approval_command or "test ! -e" not in approval_command or "test -d" not in approval_command
                ):
                    issues.append(issue("lofty_financial_patch_readiness_empty_csv_approval_command_missing", str(index)))
                if not csv_candidate_has_quality_issues and approval_target_text == "True" and approval_command:
                    issues.append(issue("lofty_financial_patch_readiness_empty_csv_approval_command_target_exists", str(index)))
                if approval_command and ("cmp -s" in approval_command or "|| cp" in approval_command):
                    issues.append(issue("lofty_financial_patch_readiness_empty_csv_approval_command_can_overwrite", str(index)))
    return issues


def validate_public_path_guard(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed"}))
    issues.extend(require_keys(payload, {"status", "issue_count", "issues"}))
    if compact_count(payload.get("issue_count")):
        issues.append(issue("public_path_guard_issue_count_nonzero", str(payload.get("issue_count"))))
    guard_issues = payload.get("issues")
    if not isinstance(guard_issues, list):
        issues.append(issue("invalid_public_path_guard_issues", "not_list"))
    elif guard_issues:
        issues.append(issue("public_path_guard_has_issues", str(len(guard_issues))))
    return issues


GUARDED_APPLY_EXTERNAL_STATUS = "excluded_no_live_update_or_email"
GUARDED_APPLY_SKIPPED_STATUSES = {
    "sold",
    "skipped_sold",
    "skipped_closed",
    "closed",
    "delisted",
    "skipped_delisted",
}


def guarded_apply_state_statuses(record: dict) -> tuple[str, str, str]:
    status = str(record.get("status") or "").strip().lower()
    update_status = str((record.get("updates") if isinstance(record.get("updates"), dict) else {}).get("status") or "").strip().lower()
    financial_status = str((record.get("financials") if isinstance(record.get("financials"), dict) else {}).get("status") or "").strip().lower()
    return status, update_status, financial_status


def guarded_apply_exclusion_counts(payload: dict) -> tuple[dict[str, int], list[str]]:
    records = payload.get("records")
    if not isinstance(records, list):
        return {"external": 0, "skipped": 0, "total": 0}, []
    external = 0
    skipped = 0
    mismatches = []
    for record in records:
        if not isinstance(record, dict):
            continue
        property_name = str(record.get("property_name") or record.get("property_path") or "?")
        status, update_status, financial_status = guarded_apply_state_statuses(record)
        update_external = update_status == GUARDED_APPLY_EXTERNAL_STATUS
        financial_external = financial_status == GUARDED_APPLY_EXTERNAL_STATUS
        update_skipped = update_status in GUARDED_APPLY_SKIPPED_STATUSES
        financial_skipped = financial_status in GUARDED_APPLY_SKIPPED_STATUSES
        if status == GUARDED_APPLY_EXTERNAL_STATUS or (update_external and financial_external):
            if update_status and financial_status and update_external != financial_external:
                mismatches.append(f"{property_name}:external updates={update_status},financials={financial_status}")
            external += 1
            continue
        if update_external != financial_external:
            mismatches.append(f"{property_name}:external updates={update_status},financials={financial_status}")
        if status in GUARDED_APPLY_SKIPPED_STATUSES or (update_skipped and financial_skipped):
            if update_status and financial_status and update_skipped != financial_skipped:
                mismatches.append(f"{property_name}:skipped updates={update_status},financials={financial_status}")
            skipped += 1
            continue
        if update_skipped != financial_skipped:
            mismatches.append(f"{property_name}:skipped updates={update_status},financials={financial_status}")
    return {"external": external, "skipped": skipped, "total": external + skipped}, mismatches


def guarded_apply_excluded_total(payload: dict) -> int | None:
    records = payload.get("records")
    if not isinstance(records, list):
        return None
    reported_total = payload.get("excluded_total_property_count")
    if reported_total is not None:
        return compact_count(reported_total)
    counts, _ = guarded_apply_exclusion_counts(payload)
    return counts["total"]


def guarded_apply_publish_mismatch_is_raw_yhome_marker_context(
    *,
    guarded: dict,
    eod_report: dict,
    guarded_total: int,
    publish_excluded: int,
) -> bool:
    """Allow raw Yhome marker counts to exceed active PM/email targets.

    Guarded apply audits every Yhome column-B marker for safety, while Lofty PM
    publish/email reports only active monthly publish targets. The EOD owner
    exclusion summary is the cross-report contract that proves that distinction.
    """
    owner_exclusion = eod_report.get("owner_exclusion_summary")
    if not isinstance(owner_exclusion, dict):
        return False
    active_counts = owner_exclusion.get("active_total_source_counts")
    if not isinstance(active_counts, dict):
        return False
    guarded_yhome = guarded.get("yhome_transition_guard") if isinstance(guarded.get("yhome_transition_guard"), dict) else {}
    yhome_marker_count = compact_count(owner_exclusion.get("yhome_column_b_marker_count")) or compact_count(guarded_yhome.get("column_b_marker_count")) or compact_count(guarded_yhome.get("excluded_count"))
    active_publish_count = compact_count(active_counts.get("lofty_pm_publish_excluded_property_count"))
    policy = str(owner_exclusion.get("policy") or "")
    return (
        bool(yhome_marker_count)
        and guarded_total == yhome_marker_count
        and publish_excluded == active_publish_count
        and owner_exclusion.get("active_total_source_counts_match") is True
        and "raw Yhome column-B markers" in policy
    )


def latest_eod_report_payload(payloads: dict[str, dict]) -> dict:
    canonical = payloads.get("baselane_eod_telegram_report.json") or {}
    preview = payloads.get("baselane_eod_telegram_preview_report.json") or {}
    canonical_time = iso_timestamp(canonical.get("generated_at"))
    preview_time = iso_timestamp(preview.get("generated_at"))
    preview_quality = preview.get("message_quality") if isinstance(preview.get("message_quality"), dict) else {}
    preview_ok = preview.get("status") == "ok" and preview_quality.get("ok") is True
    if preview and preview_time and (not canonical_time or preview_time > canonical_time or (preview_time == canonical_time and preview_ok)):
        return preview
    return canonical


def monthly_statement_aggregate_mismatches(monthly_run: dict, statements_gate: dict) -> list[str]:
    if not monthly_run or not statements_gate:
        return []
    if monthly_run.get("status") in {"missing", "unreadable"} or statements_gate.get("status") in {"missing", "unreadable"}:
        return []

    field_pairs = (
        ("monthly_statements_gate_status", "status"),
        ("monthly_statements_gate_reason", "reason"),
        ("monthly_statements_gate_action", "action"),
        ("monthly_statements_target_year", "target_year"),
        ("monthly_statements_target_month", "target_month"),
        ("monthly_statements_captured_unique_count", "captured_unique_count"),
        ("monthly_statements_min_captured_required", "min_captured_required"),
        ("monthly_statements_download_ok", "download_ok"),
        ("monthly_statements_download_new_files_count", "download_new_files_count"),
        ("monthly_statements_download_error_class", "download_error_class"),
        ("monthly_statements_operator_status", "operator_status"),
        ("monthly_statements_operator_issue_count", "operator_issue_count"),
        ("monthly_statements_auth_recovery_status", "auth_recovery_status"),
        ("monthly_statements_auth_recovery_attempted", "auth_recovery_attempted"),
        ("monthly_statements_auth_recovery_manual_auth_required", "auth_recovery_manual_auth_required"),
    )
    count_fields = {
        "monthly_statements_target_year",
        "monthly_statements_target_month",
        "monthly_statements_captured_unique_count",
        "monthly_statements_min_captured_required",
        "monthly_statements_download_new_files_count",
        "monthly_statements_operator_issue_count",
    }
    bool_fields = {
        "monthly_statements_download_ok",
        "monthly_statements_auth_recovery_attempted",
        "monthly_statements_auth_recovery_manual_auth_required",
    }
    mirrored_field_present = any(monthly_key in monthly_run for monthly_key, _gate_key in field_pairs)
    if not mirrored_field_present:
        return []

    mismatches = []
    for monthly_key, gate_key in field_pairs:
        if monthly_key not in monthly_run:
            continue
        if monthly_key not in monthly_run and gate_key not in statements_gate:
            continue
        monthly_value = monthly_run.get(monthly_key)
        gate_value = statements_gate.get(gate_key)
        if monthly_key in count_fields:
            monthly_value = compact_count(monthly_value)
            gate_value = compact_count(gate_value)
        elif monthly_key in bool_fields:
            monthly_value = monthly_value is True
            gate_value = gate_value is True
        if monthly_value != gate_value:
            mismatches.append(f"{monthly_key}:monthly={monthly_value!r},gate={gate_value!r}")
    return mismatches


def local_model_fallback_operational(payload: dict) -> bool:
    return False


def validate_cross_report_consistency(payloads: dict[str, dict]) -> list[dict]:
    issues = []
    publish = payloads.get("baselane_financials_monthly_lofty_pm_publish.json") or {}
    guarded = payloads.get("baselane_financials_monthly_guarded_apply.json") or {}
    live_updates = payloads.get("baselane_financials_monthly_live_update_capture.json") or {}
    live_financials = payloads.get("baselane_financials_monthly_live_financial_capture.json") or {}
    owner_gate = payloads.get("baselane_monthly_owner_review_gate.json") or {}
    readiness = payloads.get("baselane_financials_monthly_readiness.json") or {}
    owner_email_guard = payloads.get("baselane_monthly_owner_email_send_guard.json") or {}
    owner_email_packet = payloads.get("baselane_monthly_owner_email_packet.json") or {}
    empty_updates_queue = payloads.get("lofty_empty_updates_backfill_queue.json") or {}
    listing_cleanup_queue = payloads.get("lofty_listing_update_cleanup_queue.json") or {}
    listing_cleanup_dry_run = payloads.get("lofty_listing_cleanup_dry_run_verify.json") or {}
    listing_cleanup_preflight = payloads.get("lofty_listing_update_cleanup_queue.live-apply-preflight.json") or {}
    listing_cleanup_local_live_verify = payloads.get("lofty_listing_update_cleanup_queue.local-live-verify.json") or {}
    daily_sync = payloads.get("baselane_daily_sync_report.json") or {}
    daily_run = payloads.get("baselane_daily_run_report.json") or {}
    eod_report = latest_eod_report_payload(payloads)
    local_model = payloads.get("baselane_local_model_preflight_report.json") or {}
    monthly_run = payloads.get("baselane_financials_monthly_run_report.json") or {}
    post_auth_resume = payloads.get("baselane_financials_post_auth_resume_report.json") or {}
    weekly_run = payloads.get("baselane_weekly_file_updates_run_report.json") or {}
    weekly_cf = payloads.get("baselane_weekly_cf_statement_sync_report.json") or {}
    lofty_cdp_preflight = payloads.get("lofty_cdp_preflight_report.json") or {}
    monthly_statements_gate = payloads.get("baselane_monthly_statements_idempotent_report.json") or {}
    transfer_reconciliation = payloads.get("baselane_lofty_transfer_requirements.json") or {}
    monthly_transfer_alias = payloads.get("baselane_monthly_transfer_reconciliation_report.json") or {}
    legacy_monthly_transfer_alias = payloads.get("baselane_monthly_transfer_reconciliation.json") or {}
    monthly_close_status = payloads.get("baselane_financials_monthly_close_status.json") or {}
    monthly_pipeline_coverage = payloads.get("baselane_monthly_pipeline_candidate_coverage_audit.json") or {}

    if monthly_close_status:
        close_generated_at = str(monthly_close_status.get("generated_at") or "").strip()
        close_status_generated_at = str(monthly_close_status.get("close_status_generated_at") or "").strip()
        close_source_generated_at = str(monthly_close_status.get("source_report_generated_at") or "").strip()
        monthly_generated_at = str(monthly_run.get("generated_at") or "").strip()
        if not close_generated_at:
            issues.append(issue("monthly_close_status_missing_generated_at", "generated_at"))
        if close_status_generated_at and close_generated_at and close_status_generated_at != close_generated_at:
            issues.append(
                issue(
                    "monthly_close_status_generated_at_mismatch",
                    f"generated_at={close_generated_at},close_status_generated_at={close_status_generated_at}",
                )
            )
        if monthly_generated_at and close_source_generated_at != monthly_generated_at:
            issues.append(
                issue(
                    "monthly_close_status_source_report_generated_at_mismatch",
                    f"close={close_source_generated_at or 'missing'},monthly={monthly_generated_at}",
                )
            )
        if monthly_run.get("monthly_close_status_write_status") == "written":
            if monthly_close_status.get("close_status_write_status") != "written":
                issues.append(
                    issue(
                        "monthly_close_status_write_status_mismatch",
                        f"monthly=written,close={monthly_close_status.get('close_status_write_status')}",
                    )
                )
            if str(monthly_close_status.get("status") or "") != str(monthly_run.get("status") or ""):
                issues.append(
                    issue(
                        "monthly_close_status_status_mismatch",
                        f"monthly={monthly_run.get('status')},close={monthly_close_status.get('status')}",
                    )
                )
            if str(monthly_close_status.get("run_month") or "") != str(monthly_run.get("run_month") or ""):
                issues.append(
                    issue(
                        "monthly_close_status_run_month_mismatch",
                        f"monthly={monthly_run.get('run_month')},close={monthly_close_status.get('run_month')}",
                    )
                )
            if str(monthly_close_status.get("failed_step") or "") != str(monthly_run.get("failed_step") or ""):
                issues.append(
                    issue(
                        "monthly_close_status_failed_step_mismatch",
                        f"monthly={monthly_run.get('failed_step')},close={monthly_close_status.get('failed_step')}",
                    )
                )
    elif monthly_run.get("monthly_close_status_write_status") == "written":
        issues.append(issue("monthly_close_status_missing_after_written_status", "baselane_financials_monthly_close_status.json"))

    if monthly_transfer_alias:
        if not transfer_reconciliation:
            issues.append(issue("monthly_transfer_reconciliation_alias_without_canonical", "baselane_lofty_transfer_requirements.json"))
        elif monthly_transfer_alias != transfer_reconciliation:
            for key in (
                "status",
                "property_count",
                "eco_operating_cash_full_balance_total",
                "recommended_send_to_lofty_total",
                "recommended_send_to_lofty_total_is_final",
                "source_blocker_count",
            ):
                if monthly_transfer_alias.get(key) != transfer_reconciliation.get(key):
                    issues.append(
                        issue(
                            "monthly_transfer_reconciliation_alias_mismatch",
                            f"{key}:alias={monthly_transfer_alias.get(key)},canonical={transfer_reconciliation.get(key)}",
                        )
                    )
                    break
            else:
                issues.append(issue("monthly_transfer_reconciliation_alias_mismatch", "payload differs from canonical transfer report"))

    if legacy_monthly_transfer_alias:
        if not transfer_reconciliation:
            issues.append(
                issue(
                    "legacy_monthly_transfer_reconciliation_alias_without_canonical",
                    "baselane_lofty_transfer_requirements.json",
                )
            )
        elif legacy_monthly_transfer_alias != transfer_reconciliation:
            issues.append(
                issue(
                    "legacy_monthly_transfer_reconciliation_alias_mismatch",
                    "payload differs from canonical transfer report",
                )
            )

    if monthly_pipeline_coverage and transfer_reconciliation:
        coverage_transfer = (
            monthly_pipeline_coverage.get("transfer_reconciliation")
            if isinstance(monthly_pipeline_coverage.get("transfer_reconciliation"), dict)
            else {}
        )
        coverage_digests = (
            monthly_pipeline_coverage.get("input_digests")
            if isinstance(monthly_pipeline_coverage.get("input_digests"), dict)
            else {}
        )
        coverage_telegram = (
            monthly_pipeline_coverage.get("telegram_reconciliation")
            if isinstance(monthly_pipeline_coverage.get("telegram_reconciliation"), dict)
            else {}
        )
        canonical_source_blockers = transfer_reconciliation.get("source_blockers") if isinstance(transfer_reconciliation.get("source_blockers"), list) else []
        coverage_source_blockers = coverage_transfer.get("source_blockers") if isinstance(coverage_transfer.get("source_blockers"), list) else []
        for key in (
            "status",
            "recommended_send_to_lofty_total",
            "recommended_send_to_lofty_total_is_final",
            "eco_cash_shortfall_total",
        ):
            if coverage_transfer.get(key) != transfer_reconciliation.get(key):
                issues.append(
                    issue(
                        "monthly_pipeline_candidate_coverage_transfer_mismatch",
                        f"{key}:coverage={coverage_transfer.get(key)},canonical={transfer_reconciliation.get(key)}",
                    )
                )
        if compact_count(coverage_transfer.get("source_blocker_count")) != compact_count(transfer_reconciliation.get("source_blocker_count")):
            issues.append(
                issue(
                    "monthly_pipeline_candidate_coverage_source_blocker_count_mismatch",
                    f"coverage={coverage_transfer.get('source_blocker_count')},canonical={transfer_reconciliation.get('source_blocker_count')}",
                )
            )
        if coverage_source_blockers != canonical_source_blockers[: len(coverage_source_blockers)] or (
            len(canonical_source_blockers) <= 25 and coverage_source_blockers != canonical_source_blockers
        ):
            issues.append(issue("monthly_pipeline_candidate_coverage_source_blockers_mismatch", "coverage transfer source_blockers differ from canonical transfer report"))
        transfer_digest = str(coverage_digests.get("transfer_report") or "").strip()
        if transfer_digest and coverage_telegram.get("current_transfer_report_digest") and transfer_digest != coverage_telegram.get("current_transfer_report_digest"):
            issues.append(
                issue(
                    "monthly_pipeline_candidate_coverage_digest_mismatch",
                    f"input={transfer_digest},telegram={coverage_telegram.get('current_transfer_report_digest')}",
                )
            )

    transfer_rows = transfer_reconciliation.get("rows") if isinstance(transfer_reconciliation.get("rows"), list) else []
    for index, row in enumerate(transfer_rows, start=1):
        if not isinstance(row, dict):
            issues.append(issue("monthly_transfer_reconciliation_row_not_object", f"row={index}"))
            continue
        property_name = str(row.get("property") or row.get("property_name") or f"row-{index}").strip()
        eco_operating_cash = compact_float(row.get("eco_operating_cash"))
        full_column_e_sum = compact_float(row.get("eco_gl_column_e_sum"))
        if eco_operating_cash is None:
            issues.append(issue("monthly_transfer_reconciliation_row_missing_eco_operating_cash", property_name))
        if full_column_e_sum is None:
            issues.append(issue("monthly_transfer_reconciliation_row_missing_full_column_e_sum", property_name))
        if eco_operating_cash is not None and full_column_e_sum is not None and abs(eco_operating_cash - full_column_e_sum) > 0.01:
            issues.append(
                issue(
                    "monthly_transfer_reconciliation_eco_cash_not_full_column_e",
                    f"{property_name}:eco_operating_cash={eco_operating_cash},eco_gl_column_e_sum={full_column_e_sum}",
                )
            )
        balance_basis = str(row.get("eco_operating_cash_balance_basis") or "").strip()
        balance_scope = str(row.get("eco_operating_cash_balance_scope") or "").strip()
        if balance_basis != "full_property_split_ecogl_column_e_all_rows":
            issues.append(
                issue(
                    "monthly_transfer_reconciliation_wrong_eco_cash_balance_basis",
                    f"{property_name}:{balance_basis or 'missing'}",
                )
            )
        if balance_scope and balance_scope != "all_property_split_rows":
            issues.append(
                issue(
                    "monthly_transfer_reconciliation_wrong_eco_cash_balance_scope",
                    f"{property_name}:{balance_scope}",
                )
            )
        if row.get("eco_gl_column_e_sum_as_of_month") is not None:
            reporting_policy = str(row.get("eco_operating_cash_reporting_month_policy") or "").lower()
            if "does not limit eco operating cash rows" not in reporting_policy:
                issues.append(
                    issue(
                        "monthly_transfer_reconciliation_missing_full_balance_reporting_policy",
                        property_name,
                    )
                )
    if transfer_rows:
        policy_text = " ".join(
            str(transfer_reconciliation.get(key) or "")
            for key in (
                "eco_operating_cash_full_balance_total_policy",
                "eco_operating_cash_source_policy",
                "eco_operating_cash_reporting_month_policy",
                "eco_operating_cash_vs_send_to_lofty_policy",
            )
        ).lower()
        if "full" not in policy_text or "column e" not in policy_text:
            issues.append(issue("monthly_transfer_reconciliation_missing_full_column_e_policy", "policy text missing full Column E basis"))
        if transfer_reconciliation.get("recommended_send_to_lofty_total_is_cash_balance") is not False:
            issues.append(
                issue(
                    "monthly_transfer_reconciliation_send_total_marked_as_cash_balance",
                    str(transfer_reconciliation.get("recommended_send_to_lofty_total_is_cash_balance")),
                )
            )

    active_cash_rows = transfer_reconciliation.get("active_dao_cash_balance_rows")
    if isinstance(active_cash_rows, list) and active_cash_rows:
        missing_active_cash_count = sum(
            1
            for row in active_cash_rows
            if not isinstance(row, dict) or row.get("cash_balance_status") != "ok"
        )
        reported_missing_active_cash_count = compact_count(
            transfer_reconciliation.get("active_dao_cash_balance_missing_source_count")
        )
        if reported_missing_active_cash_count != missing_active_cash_count:
            issues.append(
                issue(
                    "monthly_transfer_reconciliation_active_cash_missing_count_mismatch",
                    f"reported={reported_missing_active_cash_count},actual={missing_active_cash_count}",
                )
            )
        if missing_active_cash_count:
            if transfer_reconciliation.get("active_dao_eco_operating_cash_total_is_complete") is not False:
                issues.append(
                    issue(
                        "monthly_transfer_reconciliation_active_cash_total_claimed_complete",
                        f"missing_active_dao_cash_count={missing_active_cash_count}",
                    )
                )
            if transfer_reconciliation.get("active_dao_eco_operating_cash_total") is not None:
                issues.append(
                    issue(
                        "monthly_transfer_reconciliation_active_cash_total_not_null_when_incomplete",
                        f"missing_active_dao_cash_count={missing_active_cash_count}",
                    )
                )
        elif transfer_reconciliation.get("active_dao_eco_operating_cash_total") is None:
            issues.append(
                issue(
                    "monthly_transfer_reconciliation_active_cash_total_missing_when_complete",
                    "active DAO cash rows are complete but total is null",
                )
            )

    statement_mismatches = monthly_statement_aggregate_mismatches(monthly_run, monthly_statements_gate)
    if statement_mismatches:
        issues.append(issue("monthly_statement_aggregate_mismatch", ";".join(statement_mismatches)))

    month_sources = {
        "readiness": readiness.get("run_month"),
        "monthly_run": monthly_run.get("run_month"),
        "owner_gate": owner_gate.get("run_month"),
        "owner_email_guard": owner_email_guard.get("run_month"),
        "owner_email_packet": owner_email_packet.get("run_month"),
        "goal_audit": (payloads.get("baselane_financials_goal_audit.json") or {}).get("run_month"),
        "post_auth_resume": post_auth_resume.get("run_month"),
    }
    valid_month_sources = {name: value for name, value in month_sources.items() if valid_run_month(value)}
    if len(set(valid_month_sources.values())) > 1:
        issues.append(issue("cross_monthly_run_month_mismatch", json.dumps(valid_month_sources, sort_keys=True)))

    publish_excluded = compact_count(publish.get("excluded_property_count"))
    live_update_excluded = compact_count(live_updates.get("excluded_property_count"))
    live_financial_excluded = compact_count(live_financials.get("excluded_property_count"))
    if live_update_excluded and live_financial_excluded and live_update_excluded != live_financial_excluded:
        issues.append(issue("cross_live_excluded_count_mismatch", f"updates={live_update_excluded},financials={live_financial_excluded}"))
    if publish_excluded and live_update_excluded and publish_excluded != live_update_excluded:
        issues.append(issue("cross_publish_live_update_excluded_count_mismatch", f"publish={publish_excluded},updates={live_update_excluded}"))
    if publish_excluded and live_financial_excluded and publish_excluded != live_financial_excluded:
        issues.append(issue("cross_publish_live_financial_excluded_count_mismatch", f"publish={publish_excluded},financials={live_financial_excluded}"))

    for label, payload in (("updates", live_updates), ("financials", live_financials)):
        skipped = compact_count(payload.get("skipped_index_count"))
        external = compact_count(payload.get("externally_excluded_count"))
        total = compact_count(payload.get("excluded_property_count"))
        if total and skipped + external != total:
            issues.append(issue("cross_live_excluded_components_mismatch", f"{label}:skipped={skipped},external={external},total={total}"))

    owner_total = compact_count(owner_gate.get("property_excluded_total_count"))
    if not owner_total:
        owner_skipped = compact_count(owner_gate.get("property_skipped_count"))
        owner_external = compact_count(owner_gate.get("property_external_excluded_count"))
        owner_total = owner_skipped + owner_external
    owner_total_authoritative = owner_total >= publish_excluded if publish_excluded else bool(owner_total)
    if owner_total_authoritative and publish_excluded and owner_total != publish_excluded:
        issues.append(issue("cross_owner_gate_publish_excluded_count_mismatch", f"owner={owner_total},publish={publish_excluded}"))

    guarded_total = guarded_apply_excluded_total(guarded)
    guarded_status = str(guarded.get("status") or "")
    guarded_is_noop = guarded_status.startswith("skipped_") and compact_count(guarded.get("record_count")) == 0
    if guarded_total is not None and publish_excluded and guarded_total != publish_excluded and not guarded_is_noop:
        if not guarded_apply_publish_mismatch_is_raw_yhome_marker_context(
            guarded=guarded,
            eod_report=eod_report,
            guarded_total=guarded_total,
            publish_excluded=publish_excluded,
        ):
            issues.append(issue("cross_guarded_apply_publish_excluded_count_mismatch", f"guarded={guarded_total},publish={publish_excluded}"))

    readiness_skip = readiness.get("monthly_skip_policy") if isinstance(readiness.get("monthly_skip_policy"), dict) else {}
    readiness_count_checks = (
        ("live_update_excluded_property_count", live_update_excluded, "cross_readiness_live_update_excluded_count_mismatch"),
        ("live_financial_excluded_property_count", live_financial_excluded, "cross_readiness_live_financial_excluded_count_mismatch"),
        ("lofty_pm_publish_excluded_property_count", publish_excluded, "cross_readiness_publish_excluded_count_mismatch"),
        ("owner_review_gate_property_excluded_total_count", owner_total, "cross_readiness_owner_gate_excluded_count_mismatch"),
        ("live_update_skipped_index_count", compact_count(live_updates.get("skipped_index_count")), "cross_readiness_live_update_skipped_count_mismatch"),
        ("live_financial_skipped_index_count", compact_count(live_financials.get("skipped_index_count")), "cross_readiness_live_financial_skipped_count_mismatch"),
        (
            "lofty_pm_publish_excluded_payload_file_count",
            compact_count(publish.get("excluded_payload_file_count")),
            "cross_readiness_publish_excluded_payload_count_mismatch",
        ),
        (
            "lofty_pm_publish_excluded_owner_email_candidate_count",
            compact_count(publish.get("excluded_owner_email_candidate_count")),
            "cross_readiness_publish_excluded_owner_email_candidate_count_mismatch",
        ),
    )
    for key, expected, code in readiness_count_checks:
        if key in readiness_skip and compact_count(readiness_skip.get(key)) != expected:
            issues.append(issue(code, f"readiness={compact_count(readiness_skip.get(key))},source={expected}"))

    readiness_comms = readiness.get("monthly_comms_gates") if isinstance(readiness.get("monthly_comms_gates"), dict) else {}
    for key, code in (
        ("owner_email_send_guard_ok", "cross_readiness_owner_email_guard_ok_mismatch"),
        ("owner_email_send_guard_active_property_proof_ok", "cross_readiness_owner_email_active_property_proof_mismatch"),
        ("owner_email_send_guard_max_once_monthly_ok", "cross_readiness_owner_email_max_once_mismatch"),
        ("owner_email_send_guard_no_spam_guard_ok", "cross_readiness_owner_email_no_spam_mismatch"),
        ("owner_email_send_guard_send_allowed", "cross_readiness_owner_email_send_allowed_mismatch"),
    ):
        readiness_value = readiness.get(key)
        comms_value = readiness_comms.get(key)
        guard_key = {
            "owner_email_send_guard_ok": "guard_ok",
            "owner_email_send_guard_active_property_proof_ok": "active_property_guard_proof",
            "owner_email_send_guard_send_allowed": "send_allowed",
        }.get(key, key.replace("owner_email_send_guard_", ""))
        if key == "owner_email_send_guard_active_property_proof_ok":
            proof = owner_email_guard.get("active_property_guard_proof")
            if isinstance(proof, dict):
                guard_value = proof.get("ok") is True
            else:
                guard_value = False
        else:
            guard_value = owner_email_guard.get(guard_key)
        if key in readiness and readiness_value != guard_value:
            issues.append(issue(code, f"readiness={readiness_value},guard={guard_value}"))
        if key in readiness_comms and comms_value != guard_value:
            issues.append(issue(code.replace("cross_readiness_", "cross_readiness_comms_"), f"readiness={comms_value},guard={guard_value}"))

    for readiness_key, preflight_key, code in (
        ("lofty_cdp_preflight_status", "status", "cross_readiness_lofty_cdp_status_mismatch"),
        ("lofty_cdp_preflight_pm_tab_count", "pm_tab_count", "cross_readiness_lofty_cdp_pm_tab_count_mismatch"),
        ("lofty_cdp_preflight_login_tab_count", "login_tab_count", "cross_readiness_lofty_cdp_login_tab_count_mismatch"),
        ("lofty_cdp_preflight_recovery_performed", "login_recovery_performed", "cross_readiness_lofty_cdp_recovery_performed_mismatch"),
        ("lofty_cdp_preflight_recovery_try_count", "login_recovery_try_count", "cross_readiness_lofty_cdp_recovery_try_count_mismatch"),
        ("lofty_cdp_preflight_manual_auth_required", "manual_auth_required", "cross_readiness_lofty_cdp_manual_auth_required_mismatch"),
    ):
        if readiness_key in readiness_comms and preflight_key in lofty_cdp_preflight and readiness_comms.get(readiness_key) != lofty_cdp_preflight.get(preflight_key):
            issues.append(issue(code, f"readiness={readiness_comms.get(readiness_key)},preflight={lofty_cdp_preflight.get(preflight_key)}"))

    eod_daily = eod_report.get("daily_sync_summary") if isinstance(eod_report.get("daily_sync_summary"), dict) else {}
    for key, expected, code in (
        ("daily_run_status", daily_run.get("status"), "cross_eod_daily_run_status_mismatch"),
        ("daily_run_return_code", daily_run.get("return_code"), "cross_eod_daily_run_return_code_mismatch"),
        ("daily_run_failed_step", daily_run.get("failed_step"), "cross_eod_daily_run_failed_step_mismatch"),
        ("daily_run_generated_at", daily_run.get("generated_at"), "cross_eod_daily_run_generated_at_mismatch"),
        ("daily_run_started_at", daily_run.get("started_at"), "cross_eod_daily_run_started_at_mismatch"),
        ("daily_run_ended_at", daily_run.get("ended_at"), "cross_eod_daily_run_ended_at_mismatch"),
        ("daily_run_duration_seconds", daily_run.get("duration_seconds"), "cross_eod_daily_run_duration_mismatch"),
    ):
        if key in eod_daily and eod_daily.get(key) != expected:
            issues.append(issue(code, f"eod={eod_daily.get(key)},run={expected}"))
    eod_daily_run_report = str(eod_daily.get("daily_run_report") or "")
    if eod_daily_run_report and Path(eod_daily_run_report).name != "baselane_daily_run_report.json":
        issues.append(issue("cross_eod_daily_run_report_path_unexpected", eod_daily_run_report))
    for key in (
        "status",
        "effective_status",
        "sync_report_status",
        "issue_count",
        "local_model_ready",
        "assetrail_live_status",
        "deterministic_sync_recovery_status",
    ):
        if key in eod_daily and key in daily_sync and eod_daily.get(key) != daily_sync.get(key):
            issues.append(issue("cross_eod_daily_sync_summary_mismatch", f"{key}:eod={eod_daily.get(key)},daily={daily_sync.get(key)}"))
    message = str(eod_report.get("message") or "")
    recovery_status = str(daily_sync.get("deterministic_sync_recovery_status") or "")
    if recovery_status.startswith("recovered_") and "recovered" not in message.lower():
        issues.append(issue("cross_eod_message_missing_recovery_status", recovery_status))
    if daily_sync.get("assetrail_live_status") == "ok" and "assetrail" not in message.lower():
        issues.append(issue("cross_eod_message_missing_assetrail_status", "assetrail_live_status=ok"))
    eod_empty_updates = eod_report.get("lofty_empty_updates_backfill_queue_summary") if isinstance(eod_report.get("lofty_empty_updates_backfill_queue_summary"), dict) else {}
    for key, code in (
        ("status", "cross_eod_empty_updates_queue_status_mismatch"),
        ("issue_count", "cross_eod_empty_updates_queue_issue_count_mismatch"),
        ("property_count", "cross_eod_empty_updates_queue_property_count_mismatch"),
        ("ready_local_backfill_from_approved_count", "cross_eod_empty_updates_queue_ready_count_mismatch"),
        ("needs_update_approval_target_count", "cross_eod_empty_updates_queue_approval_count_mismatch"),
        ("blocked_count", "cross_eod_empty_updates_queue_blocked_count_mismatch"),
        ("mutates_dropbox_files", "cross_eod_empty_updates_queue_dropbox_flag_mismatch"),
        ("mutates_lofty_listing", "cross_eod_empty_updates_queue_lofty_flag_mismatch"),
        ("sends_owner_email", "cross_eod_empty_updates_queue_email_flag_mismatch"),
    ):
        if key in eod_empty_updates and key in empty_updates_queue and eod_empty_updates.get(key) != empty_updates_queue.get(key):
            issues.append(issue(code, f"eod={eod_empty_updates.get(key)},queue={empty_updates_queue.get(key)}"))
    owner_reason_counts = owner_email_packet.get("property_unavailable_reason_counts") if isinstance(owner_email_packet.get("property_unavailable_reason_counts"), dict) else {}
    expected_eod_gap_counts = {
        "listing_gap_count": compact_count(owner_reason_counts.get("listing_history_cleanup_required"))
        + compact_count(owner_reason_counts.get("live_update_guard_not_reconciled")),
        "latest_update_body_guard_count": compact_count(owner_reason_counts.get("latest_update_body_guard")),
        "missing_or_empty_update_count": compact_count(owner_reason_counts.get("updates_md_empty"))
        + compact_count(owner_reason_counts.get("updates_md_missing")),
    }
    expected_eod_gap_compact_parts = []
    if expected_eod_gap_counts["listing_gap_count"]:
        expected_eod_gap_compact_parts.append(f"L{expected_eod_gap_counts['listing_gap_count']}")
    if expected_eod_gap_counts["latest_update_body_guard_count"]:
        expected_eod_gap_compact_parts.append(f"C{expected_eod_gap_counts['latest_update_body_guard_count']}")
    if expected_eod_gap_counts["missing_or_empty_update_count"]:
        expected_eod_gap_compact_parts.append(f"M{expected_eod_gap_counts['missing_or_empty_update_count']}")
    expected_eod_gap_compact = "/".join(expected_eod_gap_compact_parts)
    eod_gap_summary = eod_report.get("owner_email_gap_summary") if isinstance(eod_report.get("owner_email_gap_summary"), dict) else {}
    if any(expected_eod_gap_counts.values()):
        if not eod_gap_summary:
            issues.append(issue("cross_eod_owner_email_gap_summary_missing", json.dumps(expected_eod_gap_counts, sort_keys=True)))
        else:
            for key, expected in expected_eod_gap_counts.items():
                if compact_count(eod_gap_summary.get(key)) != expected:
                    issues.append(issue("cross_eod_owner_email_gap_summary_mismatch", f"{key}:eod={eod_gap_summary.get(key)},packet={expected}"))
            if str(eod_gap_summary.get("compact") or "") != expected_eod_gap_compact:
                issues.append(
                    issue(
                        "cross_eod_owner_email_gap_compact_mismatch",
                        f"eod={eod_gap_summary.get('compact')},packet={expected_eod_gap_compact}",
                    )
                )
    message_gap_match = re.search(r"email gaps\s+L(\d+)(?:/C(\d+))?(?:/M(\d+))?", message)
    if message_gap_match:
        message_gap_counts = {
            "listing_gap_count": compact_count(message_gap_match.group(1)),
            "latest_update_body_guard_count": compact_count(message_gap_match.group(2)),
            "missing_or_empty_update_count": compact_count(message_gap_match.group(3)),
        }
        for key, expected in expected_eod_gap_counts.items():
            if message_gap_counts.get(key) != expected:
                issues.append(issue("cross_eod_message_owner_email_gap_mismatch", f"{key}:message={message_gap_counts.get(key)},packet={expected}"))
    owner_empty_count = compact_count(owner_reason_counts.get("updates_md_empty")) + compact_count(owner_reason_counts.get("updates_md_missing"))
    queue_property_count = compact_count(empty_updates_queue.get("property_count"))
    owner_email_recipient_blocked = (
        compact_count(owner_email_packet.get("recipient_count")) == 0
        and compact_count(owner_email_packet.get("packet_count")) == 0
        and "no recipient" in str(owner_email_packet.get("preview_write_blocked_reason") or "").lower()
    )
    if owner_empty_count or queue_property_count:
        if not owner_email_recipient_blocked and owner_empty_count != queue_property_count:
            issues.append(issue("cross_owner_email_empty_updates_queue_count_mismatch", f"owner_email={owner_empty_count},queue={queue_property_count}"))
        if empty_updates_queue.get("mutates_dropbox_files") is not False:
            issues.append(issue("cross_empty_updates_queue_mutates_dropbox", str(empty_updates_queue.get("mutates_dropbox_files"))))
        if empty_updates_queue.get("mutates_lofty_listing") is not False:
            issues.append(issue("cross_empty_updates_queue_mutates_lofty", str(empty_updates_queue.get("mutates_lofty_listing"))))
        if empty_updates_queue.get("sends_owner_email") is not False:
            issues.append(issue("cross_empty_updates_queue_sends_email", str(empty_updates_queue.get("sends_owner_email"))))
        if empty_updates_queue.get("approval_copy_requires_current_rent_roll") is not True:
            issues.append(issue("cross_empty_updates_queue_not_rent_roll_gated", str(empty_updates_queue.get("approval_copy_requires_current_rent_roll"))))

    listing_ready_count = compact_count(listing_cleanup_queue.get("ready_listing_cleanup_count"))
    listing_ready_digest = str(listing_cleanup_queue.get("ready_cleanup_idempotency_digest") or "")
    dry_run_ready_count = compact_count(listing_cleanup_dry_run.get("ready_listing_cleanup_count"))
    dry_run_verified_count = compact_count(listing_cleanup_dry_run.get("verified_record_count"))
    dry_run_digest = str(listing_cleanup_dry_run.get("ready_cleanup_idempotency_digest") or "")
    preflight_ready_count = compact_count(listing_cleanup_preflight.get("ready_listing_cleanup_count"))
    preflight_verified_count = compact_count(listing_cleanup_preflight.get("verified_record_count"))
    preflight_expected_digest = str(listing_cleanup_preflight.get("expected_digest") or "")
    preflight_queue_digest = str(listing_cleanup_preflight.get("queue_digest") or "")
    preflight_verify_digest = str(listing_cleanup_preflight.get("dry_run_verify_digest") or "")
    listing_cleanup_live_repaired_noop = (
        listing_ready_count == 0
        and dry_run_ready_count == 0
        and dry_run_verified_count == 0
        and preflight_ready_count > 0
        and preflight_verified_count == preflight_ready_count
        and listing_cleanup_local_live_verify.get("status") == "ok"
        and compact_count(listing_cleanup_local_live_verify.get("target_count")) == preflight_verified_count
        and compact_count(listing_cleanup_local_live_verify.get("ok_count")) == preflight_verified_count
        and compact_count(listing_cleanup_local_live_verify.get("issue_count")) == 0
    )
    if listing_cleanup_queue and listing_cleanup_dry_run:
        if dry_run_ready_count != listing_ready_count:
            issues.append(issue("cross_listing_cleanup_dry_run_ready_count_mismatch", f"dry_run={dry_run_ready_count},queue={listing_ready_count}"))
        if dry_run_verified_count != listing_ready_count:
            issues.append(issue("cross_listing_cleanup_dry_run_verified_count_mismatch", f"dry_run={dry_run_verified_count},queue={listing_ready_count}"))
        if listing_ready_digest and dry_run_digest and listing_ready_digest != dry_run_digest:
            issues.append(issue("cross_listing_cleanup_dry_run_digest_mismatch", f"dry_run={dry_run_digest},queue={listing_ready_digest}"))
    if listing_cleanup_queue and listing_cleanup_preflight and not listing_cleanup_live_repaired_noop:
        if preflight_ready_count != listing_ready_count:
            issues.append(issue("cross_listing_cleanup_preflight_ready_count_mismatch", f"preflight={preflight_ready_count},queue={listing_ready_count}"))
        if preflight_verified_count != listing_ready_count:
            issues.append(issue("cross_listing_cleanup_preflight_verified_count_mismatch", f"preflight={preflight_verified_count},queue={listing_ready_count}"))
        for label, digest in (
            ("expected", preflight_expected_digest),
            ("queue", preflight_queue_digest),
            ("dry_run_verify", preflight_verify_digest),
        ):
            if listing_ready_digest and digest and digest != listing_ready_digest:
                issues.append(issue(f"cross_listing_cleanup_preflight_{label}_digest_mismatch", f"preflight={digest},queue={listing_ready_digest}"))
    if listing_cleanup_dry_run and listing_cleanup_preflight and not listing_cleanup_live_repaired_noop:
        if dry_run_digest and preflight_verify_digest and preflight_verify_digest != dry_run_digest:
            issues.append(issue("cross_listing_cleanup_preflight_verify_digest_mismatch", f"preflight={preflight_verify_digest},dry_run={dry_run_digest}"))
    if eod_gap_summary and "ready_listing_cleanup_count" in eod_gap_summary and listing_cleanup_queue:
        if compact_count(eod_gap_summary.get("ready_listing_cleanup_count")) != listing_ready_count:
            issues.append(
                issue(
                    "cross_eod_owner_email_gap_ready_cleanup_mismatch",
                    f"eod={eod_gap_summary.get('ready_listing_cleanup_count')},queue={listing_ready_count}",
                )
            )

    if local_model:
        for daily_key, model_key, code in (
            ("local_model", "model", "cross_daily_local_model_name_mismatch"),
            ("local_model_status", "status", "cross_daily_local_model_status_mismatch"),
            ("local_model_validation_digest", "validation_digest", "cross_daily_local_model_digest_mismatch"),
        ):
            if daily_key in daily_sync and model_key in local_model and daily_sync.get(daily_key) != local_model.get(model_key):
                issues.append(issue(code, f"daily={daily_sync.get(daily_key)},model={local_model.get(model_key)}"))
        if (
            daily_sync.get("local_model_ready") is True
            and local_model.get("status") != "ok"
            and not local_model_fallback_operational(local_model)
        ):
            issues.append(issue("cross_daily_local_model_ready_but_preflight_not_ok", str(local_model.get("status") or "missing")))

    readiness_actionable = readiness.get("actionable_summary") if isinstance(readiness.get("actionable_summary"), dict) else {}
    monthly_failed_step = str(monthly_run.get("effective_failed_step") or monthly_run.get("failed_step") or "")
    downstream_generation_blocked = monthly_run.get("status") in {"failed", "review"} and monthly_failed_step in {
        "baselane_disk_space_preflight",
        "baselane_monthly_finance_truth_refresh",
    }
    owner_email_packet_checks = (
        ("monthly_readiness_owner_email_allowed", readiness.get("owner_email_allowed"), "cross_monthly_run_readiness_owner_email_allowed_mismatch"),
        ("monthly_readiness_blocker_count", compact_count(readiness.get("blocker_count")), "cross_monthly_run_readiness_blocker_count_mismatch"),
        (
            "monthly_readiness_actionable_blocker_count",
            compact_count(readiness_actionable.get("actionable_blocker_count")),
            "cross_monthly_run_readiness_actionable_count_mismatch",
        ),
        ("owner_email_send_guard_status", owner_email_guard.get("status"), "cross_monthly_run_owner_email_guard_status_mismatch"),
        ("owner_email_send_guard_send_allowed", owner_email_guard.get("send_allowed"), "cross_monthly_run_owner_email_guard_send_allowed_mismatch"),
        ("owner_email_send_guard_issue_count", compact_count(owner_email_guard.get("issue_count")), "cross_monthly_run_owner_email_guard_issue_count_mismatch"),
    )
    owner_email_packet_downstream_checks = (
        ("owner_email_packet_status", owner_email_guard.get("owner_email_packet_status"), "cross_monthly_run_owner_email_packet_status_mismatch"),
        ("owner_email_packet_issue_count", compact_count(owner_email_guard.get("owner_email_packet_issue_count")), "cross_monthly_run_owner_email_packet_issue_count_mismatch"),
        ("owner_email_packet_safe_to_send_now", owner_email_guard.get("owner_email_packet_safe_to_send_now"), "cross_monthly_run_owner_email_packet_safe_mismatch"),
        (
            "owner_email_packet_full_history_leak_count",
            compact_count(owner_email_guard.get("owner_email_packet_full_history_leak_count")),
            "cross_monthly_run_owner_email_packet_full_history_leak_mismatch",
        ),
        (
            "owner_email_packet_body_guard_issue_count",
            compact_count(owner_email_guard.get("owner_email_packet_body_guard_issue_count")),
            "cross_monthly_run_owner_email_packet_body_guard_issue_mismatch",
        ),
    )
    checks = owner_email_packet_checks + (() if downstream_generation_blocked else owner_email_packet_downstream_checks)
    for run_key, expected, code in checks:
        if run_key in monthly_run and monthly_run.get(run_key) != expected:
            issues.append(issue(code, f"monthly_run={monthly_run.get(run_key)},source={expected}"))
    if (
        transfer_reconciliation
        and transfer_reconciliation.get("status") not in {"missing", "unreadable"}
        and not downstream_generation_blocked
    ):
        transfer_run_checks = (
            (
                "transfer_reconciliation_status",
                transfer_reconciliation.get("status"),
                "cross_monthly_run_transfer_status_mismatch",
            ),
            (
                "transfer_reconciliation_ready_count",
                compact_count(transfer_reconciliation.get("ready_to_send_property_count")),
                "cross_monthly_run_transfer_ready_count_mismatch",
            ),
            (
                "transfer_reconciliation_held_count",
                compact_count(transfer_reconciliation.get("held_property_count")),
                "cross_monthly_run_transfer_held_count_mismatch",
            ),
            (
                "transfer_reconciliation_missing_bank_action_count",
                compact_count(transfer_reconciliation.get("missing_bank_action_count")),
                "cross_monthly_run_transfer_missing_bank_action_count_mismatch",
            ),
            (
                "transfer_reconciliation_recommended_total",
                transfer_reconciliation.get("recommended_send_to_lofty_total"),
                "cross_monthly_run_transfer_recommended_total_mismatch",
            ),
            (
                "transfer_reconciliation_recommended_total_is_final",
                transfer_reconciliation.get("recommended_send_to_lofty_total_is_final") is True,
                "cross_monthly_run_transfer_final_flag_mismatch",
            ),
        )
        for run_key, expected, code in transfer_run_checks:
            if run_key in monthly_run and monthly_run.get(run_key) != expected:
                issues.append(issue(code, f"monthly_run={monthly_run.get(run_key)},source={expected}"))
        source_bank_actions = transfer_reconciliation.get("bank_action_counts")
        if isinstance(source_bank_actions, dict) and "transfer_reconciliation_bank_action_counts" in monthly_run:
            if monthly_run.get("transfer_reconciliation_bank_action_counts") != source_bank_actions:
                issues.append(
                    issue(
                        "cross_monthly_run_transfer_bank_action_counts_mismatch",
                        f"monthly_run={monthly_run.get('transfer_reconciliation_bank_action_counts')},source={source_bank_actions}",
                    )
                )
    effective_values = {
        "monthly_run": monthly_run.get("effective_send_owner_emails"),
        "owner_email_guard": owner_email_guard.get("effective_send_owner_emails"),
        "publish": publish.get("effective_send_owner_emails"),
    }
    if len({value for value in effective_values.values() if value is not None}) > 1:
        issues.append(issue("cross_monthly_effective_owner_email_mismatch", json.dumps(effective_values, sort_keys=True)))
    if monthly_run.get("effective_send_owner_emails") is True:
        if readiness.get("owner_email_allowed") is not True:
            issues.append(issue("cross_monthly_effective_email_without_readiness", str(readiness.get("owner_email_allowed"))))
        if owner_email_guard.get("send_allowed") is not True:
            issues.append(issue("cross_monthly_effective_email_without_guard", str(owner_email_guard.get("send_allowed"))))
        if owner_email_guard.get("owner_email_packet_ok_for_send") is not True:
            issues.append(issue("cross_monthly_effective_email_without_packet_ok", str(owner_email_guard.get("owner_email_packet_ok_for_send"))))
        if publish.get("effective_send_owner_emails") is not True:
            issues.append(issue("cross_monthly_effective_email_without_publish", str(publish.get("effective_send_owner_emails"))))

    weekly_review_safe = weekly_run.get("review_safe_idempotency") if isinstance(weekly_run.get("review_safe_idempotency"), dict) else {}
    weekly_scheduled_noop = weekly_run.get("status") in {"skipped_not_friday", "already_done_for_week"} and weekly_review_safe.get("scheduled_noop") is True
    if not weekly_scheduled_noop:
        for weekly_key, cf_key, code in (
            ("cf_statement_sync_status", "status", "cross_weekly_cf_status_mismatch"),
            ("cf_statement_sync_source_cash_balance_violation_count", "source_cash_balance_violation_count", "cross_weekly_cf_source_cash_violation_mismatch"),
            ("cf_statement_sync_source_cash_balance_update_count", "source_cash_balance_update_count", "cross_weekly_cf_source_cash_update_mismatch"),
            ("cf_statement_sync_no_mortgage_debt_violation_count", "no_mortgage_debt_violation_count", "cross_weekly_cf_no_mortgage_mismatch"),
            ("cf_statement_sync_conflict_count", "conflict_count", "cross_weekly_cf_conflict_mismatch"),
            ("cf_statement_sync_untagged_review_required_count", "untagged_review_required_count", "cross_weekly_cf_untagged_mismatch"),
            ("cf_statement_sync_canonical_cf_property_count", "canonical_cf_property_count", "cross_weekly_cf_property_count_mismatch"),
            ("cf_statement_sync_missing_canonical_cf_count", "missing_canonical_cf_count", "cross_weekly_cf_missing_canonical_mismatch"),
        ):
            if weekly_key in weekly_run and cf_key in weekly_cf and weekly_run.get(weekly_key) != weekly_cf.get(cf_key):
                issues.append(issue(code, f"weekly={weekly_run.get(weekly_key)},cf={weekly_cf.get(cf_key)}"))

    return issues


def validate_daily_sync(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "effective_status",
                "issue_count",
                "sync_report_status",
                "deterministic_sync_recovery_status",
                "wrapper_return_code",
                "wrapper_recovered_by_standalone_sync",
                "daily_missing_step_names",
                "wrapper_consistency_issues",
                "steps",
                "wrapper_steps",
                "source_statuses",
                "local_model_ready",
                "local_model",
                "local_model_status",
                "local_model_validation_digest",
                "source_cash_balance_status",
                "source_cash_balance_report_fresh",
                "source_cash_balance_report_age_hours",
                "source_cash_balance_max_age_hours",
                "source_cash_balance_update_count",
                "source_cash_balance_violation_count",
                "source_cash_balance_missing_row_count",
                "source_cash_balance_missing_month_column_count",
                "first_day_pm_fee_audit_status",
                "first_day_pm_fee_count",
                "split_write_attempted",
                "split_output_missing_count",
                "split_output_stale_count",
                "split_output_unreadable_count",
                "split_output_mismatch_count",
                "split_unresolved_property_count",
                "split_unresolved_row_count",
                "assetrail_push_status",
                "assetrail_live_status",
                "assetrail_live_temp_ledger_status_count",
                "next_action",
            },
        )
    )
    if str(payload.get("effective_status") or "").strip() not in OK_STATUSES:
        issues.append(issue("invalid_effective_status", str(payload.get("effective_status") or "missing")))
    if compact_count(payload.get("issue_count")) < 0:
        issues.append(issue("invalid_issue_count", str(payload.get("issue_count"))))
    if not isinstance(payload.get("local_model_ready"), bool):
        issues.append(issue("invalid_local_model_ready", "not_bool"))
    if payload.get("local_model") != EXPECTED_LOCAL_MODEL:
        issues.append(issue("daily_sync_unexpected_local_model", str(payload.get("local_model") or "missing")))
    local_model_effectively_ready = (
        payload.get("local_model_ready") is True
        and payload.get("local_model_status") == "review"
        and payload.get("local_model_operational") is True
        and payload.get("local_model_fallback_smoke_ok") is True
    )
    if payload.get("local_model_status") != "ok" and not local_model_effectively_ready:
        issues.append(issue("daily_sync_local_model_status_not_ok", str(payload.get("local_model_status") or "missing")))
    if not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("local_model_validation_digest") or "")):
        issues.append(issue("daily_sync_invalid_local_model_validation_digest", str(payload.get("local_model_validation_digest") or "missing")))
    steps = payload.get("steps") if isinstance(payload.get("steps"), dict) else {}
    source_statuses = payload.get("source_statuses") if isinstance(payload.get("source_statuses"), dict) else {}
    wrapper_return_code = compact_count(payload.get("wrapper_return_code"))
    effective_return_code = compact_count(payload.get("effective_return_code"))
    recovery_status = str(payload.get("deterministic_sync_recovery_status") or "")
    if not isinstance(payload.get("steps"), dict):
        issues.append(issue("daily_sync_steps_missing_or_invalid", type(payload.get("steps")).__name__))
    if not isinstance(payload.get("wrapper_steps"), dict):
        issues.append(issue("daily_sync_wrapper_steps_missing_or_invalid", type(payload.get("wrapper_steps")).__name__))
    if not isinstance(payload.get("source_statuses"), dict):
        issues.append(issue("daily_sync_source_statuses_missing_or_invalid", type(payload.get("source_statuses")).__name__))
    if payload.get("daily_missing_step_names") not in ([], None):
        issues.append(issue("daily_sync_missing_step_names_nonempty", str(payload.get("daily_missing_step_names"))))
    if payload.get("wrapper_consistency_issues") not in ([], None):
        issues.append(issue("daily_sync_wrapper_consistency_issues_nonempty", str(payload.get("wrapper_consistency_issues"))))
    wrapper_recovery_required = (
        payload.get("deterministic_sync_recovery_required") is not False
        or payload.get("status") == "ok"
        or payload.get("sync_report_status") == "ok"
    )
    effective_wrapper_failure = effective_return_code != 0 or bool(payload.get("effective_failed_step"))
    if wrapper_return_code != 0 and effective_wrapper_failure and wrapper_recovery_required:
        if payload.get("wrapper_recovered_by_standalone_sync") is not True:
            issues.append(issue("daily_sync_wrapper_failed_without_recovery", str(payload.get("wrapper_recovered_by_standalone_sync"))))
        if recovery_status != "recovered_by_newer_successful_sync":
            issues.append(issue("daily_sync_wrapper_recovery_status_invalid", recovery_status or "missing"))
        if payload.get("sync_report_status") != "ok":
            issues.append(issue("daily_sync_wrapper_recovery_sync_report_not_ok", str(payload.get("sync_report_status") or "missing")))
    if payload.get("status") == "ok":
        if str(payload.get("effective_status") or "").strip() != "ok":
            issues.append(issue("daily_sync_status_ok_effective_not_ok", str(payload.get("effective_status") or "missing")))
        if payload.get("sync_report_status") != "ok":
            issues.append(issue("daily_sync_status_ok_sync_report_not_ok", str(payload.get("sync_report_status") or "missing")))
        if payload.get("local_model_ready") is not True:
            issues.append(issue("daily_sync_status_ok_local_model_not_ready", str(payload.get("local_model_ready"))))
        if payload.get("source_cash_balance_status") != "ok":
            issues.append(issue("daily_sync_source_cash_balance_status_not_ok", str(payload.get("source_cash_balance_status") or "missing")))
        if payload.get("source_cash_balance_report_fresh") is not True:
            issues.append(issue("daily_sync_source_cash_balance_report_not_fresh", str(payload.get("source_cash_balance_report_fresh"))))
        source_cash_age = payload.get("source_cash_balance_report_age_hours")
        source_cash_max_age = payload.get("source_cash_balance_max_age_hours")
        try:
            source_cash_age_value = float(source_cash_age)
            source_cash_max_age_value = float(source_cash_max_age)
        except (TypeError, ValueError):
            issues.append(issue("daily_sync_source_cash_balance_report_age_invalid", f"{source_cash_age}>{source_cash_max_age}"))
        else:
            if source_cash_age_value < -1 or source_cash_age_value > source_cash_max_age_value:
                issues.append(issue("daily_sync_source_cash_balance_report_stale", f"{source_cash_age_value}>{source_cash_max_age_value}"))
        if payload.get("first_day_pm_fee_audit_status") != "ok":
            issues.append(issue("daily_sync_first_day_pm_fee_audit_not_ok", str(payload.get("first_day_pm_fee_audit_status") or "missing")))
        if payload.get("split_write_attempted") is not True:
            issues.append(issue("daily_sync_split_write_not_attempted", str(payload.get("split_write_attempted"))))
        if payload.get("assetrail_live_status") != "ok":
            issues.append(issue("daily_sync_assetrail_live_status_not_ok", str(payload.get("assetrail_live_status") or "missing")))
        if payload.get("assetrail_push_status") not in DAILY_ASSETRAIL_PUSH_OK_STATUSES:
            issues.append(issue("daily_sync_assetrail_push_status_not_ok", str(payload.get("assetrail_push_status") or "missing")))
        if payload.get("daily_missing_step_names") != []:
            issues.append(issue("daily_sync_status_ok_missing_steps_present", str(payload.get("daily_missing_step_names"))))
        if payload.get("wrapper_consistency_issues") != []:
            issues.append(issue("daily_sync_status_ok_wrapper_consistency_issues_present", str(payload.get("wrapper_consistency_issues"))))
        if steps.get("local_model_preflight") != "ok":
            issues.append(issue("daily_sync_step_local_model_preflight_not_ok", str(steps.get("local_model_preflight") or "missing")))
        if steps.get("session_seed") not in DAILY_SESSION_SEED_OK_STATUSES:
            issues.append(issue("daily_sync_step_session_seed_not_ok", str(steps.get("session_seed") or "missing")))
        if steps.get("deterministic_sync") != "ok" and not recovery_status.startswith("recovered_"):
            issues.append(issue("daily_sync_step_deterministic_sync_not_ok", str(steps.get("deterministic_sync") or "missing")))
        for source_key, expected in (
            ("sync", "ok"),
            ("source_cash_balance", "ok"),
            ("source_transaction_index", "ok"),
            ("split_property_csvs", "current"),
        ):
            if source_statuses.get(source_key) != expected:
                issues.append(
                    issue(
                        f"daily_sync_source_status_{source_key}_not_{expected}",
                        str(source_statuses.get(source_key) or "missing"),
                    )
                )
        if source_statuses.get("local_model_preflight") != "ok" and not (
            source_statuses.get("local_model_preflight") == "review" and local_model_effectively_ready
        ):
            issues.append(
                issue(
                    "daily_sync_source_status_local_model_preflight_not_ok",
                    str(source_statuses.get("local_model_preflight") or "missing"),
                )
            )
    for key, code in (
        ("source_cash_balance_violation_count", "daily_sync_source_cash_balance_violation_count_nonzero"),
        ("source_cash_balance_missing_row_count", "daily_sync_source_cash_balance_missing_row_count_nonzero"),
        ("source_cash_balance_missing_month_column_count", "daily_sync_source_cash_balance_missing_month_column_count_nonzero"),
        ("first_day_pm_fee_count", "daily_sync_first_day_pm_fee_count_nonzero"),
        ("split_output_missing_count", "daily_sync_split_output_missing_count_nonzero"),
        ("split_output_stale_count", "daily_sync_split_output_stale_count_nonzero"),
        ("split_output_unreadable_count", "daily_sync_split_output_unreadable_count_nonzero"),
        ("split_output_mismatch_count", "daily_sync_split_output_mismatch_count_nonzero"),
        ("split_unresolved_property_count", "daily_sync_split_unresolved_property_count_nonzero"),
        ("split_unresolved_row_count", "daily_sync_split_unresolved_row_count_nonzero"),
        ("assetrail_live_temp_ledger_status_count", "daily_sync_assetrail_temp_ledger_status_count_nonzero"),
    ):
        if compact_count(payload.get(key)):
            issues.append(issue(code, str(payload.get(key))))
    return issues


def validate_local_model_preflight(payload: dict, *, max_age_hours: float) -> list[dict]:
    issues = []
    direct_smoke = payload.get("direct_smoke") if isinstance(payload.get("direct_smoke"), dict) else {}
    finance_smoke = payload.get("finance_contract_smoke") if isinstance(payload.get("finance_contract_smoke"), dict) else {}
    contract = payload.get("validation_contract") if isinstance(payload.get("validation_contract"), dict) else {}
    scope = payload.get("model_execution_scope") if isinstance(payload.get("model_execution_scope"), dict) else {}
    expected_finance_response = payload.get("finance_contract_expected_response")
    fallback_operational = False
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed"}))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "issue_count",
                "model",
                "provider",
                "model_id",
                "configured_model_present",
                "selected_endpoint_from_config",
                "model_available",
                "small_model_execution_allowed",
                "small_model_pipeline_execution_allowed",
                "small_model_task_scoped_execution_allowed",
                "direct_smoke",
                "finance_contract_smoke",
                "finance_contract_expected_response",
                "validation_contract",
                "validation_digest",
                "generated_at",
            },
        )
    )
    if payload.get("model") != EXPECTED_LOCAL_MODEL:
        issues.append(issue("unexpected_model", str(payload.get("model") or "missing")))
    if payload.get("provider") != EXPECTED_LOCAL_PROVIDER:
        issues.append(issue("unexpected_provider", str(payload.get("provider") or "missing")))
    if payload.get("model_id") != EXPECTED_LOCAL_MODEL_ID:
        issues.append(issue("unexpected_model_id", str(payload.get("model_id") or "missing")))
    if payload.get("configured_model_present") is not True:
        issues.append(issue("configured_model_not_present", str(payload.get("configured_model_present"))))
    if payload.get("selected_endpoint_from_config") is not True:
        issues.append(issue("selected_endpoint_not_from_config", str(payload.get("selected_endpoint_from_config"))))
    if payload.get("model_available") is not True and not fallback_operational:
        issues.append(issue("model_not_available", str(payload.get("model_available"))))
    if payload.get("small_model_execution_allowed") is not False:
        issues.append(issue("small_model_execution_must_not_allow_pipeline", str(payload.get("small_model_execution_allowed"))))
    if payload.get("small_model_pipeline_execution_allowed") is not False:
        issues.append(
            issue(
                "small_model_pipeline_execution_must_be_denied",
                str(payload.get("small_model_pipeline_execution_allowed")),
            )
        )
    if payload.get("small_model_task_scoped_execution_allowed") is not True:
        issues.append(
            issue(
                "small_model_task_scoped_execution_not_allowed",
                str(payload.get("small_model_task_scoped_execution_allowed")),
            )
        )
    if scope.get("deterministic_only") is not True:
        issues.append(issue("small_model_scope_not_deterministic_only", str(scope.get("deterministic_only"))))
    if scope.get("pipeline_execution_allowed") is not False:
        issues.append(issue("small_model_scope_pipeline_execution_not_denied", str(scope.get("pipeline_execution_allowed"))))
    direct_smoke_ok = direct_smoke.get("attempted") is True and direct_smoke.get("ok") is True
    if not direct_smoke_ok and not fallback_operational:
        issues.append(issue("direct_smoke_not_ok", json.dumps(direct_smoke, sort_keys=True)))
    elif direct_smoke_ok and direct_smoke.get("response") != "BASELANE_MODEL_OK":
        issues.append(issue("direct_smoke_contract_mismatch", str(direct_smoke.get("response") or "missing")))
    finance_smoke_ok = finance_smoke.get("attempted") is True and finance_smoke.get("ok") is True
    if not finance_smoke_ok:
        issues.append(issue("finance_contract_smoke_not_ok", json.dumps(finance_smoke, sort_keys=True)))
    elif finance_smoke.get("response") != expected_finance_response:
        issues.append(issue("finance_contract_smoke_mismatch", str(finance_smoke.get("response") or "missing")))
    if contract.get("selected_endpoint_from_config") is not True:
        issues.append(issue("validation_contract_endpoint_not_from_config", json.dumps(contract, sort_keys=True)))
    if not fallback_operational and contract.get("direct_smoke_ok") is True and contract.get("direct_smoke_response") != "BASELANE_MODEL_OK":
        issues.append(issue("validation_contract_mismatch", json.dumps(contract, sort_keys=True)))
    if contract.get("finance_contract_smoke_ok") is not True:
        issues.append(issue("validation_contract_finance_smoke_not_ok", json.dumps(contract, sort_keys=True)))
    elif contract.get("finance_contract_response") != expected_finance_response:
        issues.append(issue("validation_contract_finance_mismatch", json.dumps(contract, sort_keys=True)))
    if not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("validation_digest") or "")):
        issues.append(issue("invalid_validation_digest", str(payload.get("validation_digest") or "missing")))
    age_hours = iso_age_hours(payload.get("generated_at"))
    if age_hours is None or age_hours < -1 or age_hours > max_age_hours:
        issues.append(issue("stale_local_model_preflight", str(age_hours)))
    if compact_count(payload.get("issue_count")) != 0 and not fallback_operational:
        issues.append(issue("local_model_preflight_has_issues", str(payload.get("issue_count"))))
    return issues


def validate_unreviewed_financial_approval_quarantine(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review", "failed"}))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "source_report",
                "apply",
                "mutates_dropbox_files",
                "mutates_lofty_listing",
                "sends_owner_email",
                "approval_env_var",
                "approval_required_value",
                "digest_env_var",
                "digest_required_value",
                "commands_file",
                "command_count",
                "unreviewed_approved_financial_count",
                "ready_to_quarantine_count",
                "review_count",
                "entries",
            },
        )
    )
    if payload.get("mutates_lofty_listing") is not False:
        issues.append(issue("unreviewed_financial_quarantine_mutates_lofty_listing", str(payload.get("mutates_lofty_listing"))))
    if payload.get("sends_owner_email") is not False:
        issues.append(issue("unreviewed_financial_quarantine_sends_owner_email", str(payload.get("sends_owner_email"))))
    if payload.get("apply") is not True and payload.get("mutates_dropbox_files") is not False:
        issues.append(issue("unreviewed_financial_quarantine_dry_run_mutates_dropbox", str(payload.get("mutates_dropbox_files"))))
    if payload.get("approval_env_var") != "LOFTY_UNREVIEWED_FINANCIAL_APPROVAL_QUARANTINE_APPROVED":
        issues.append(issue("unreviewed_financial_quarantine_bad_approval_env", str(payload.get("approval_env_var"))))
    if str(payload.get("approval_required_value") or "") != "1":
        issues.append(issue("unreviewed_financial_quarantine_bad_approval_value", str(payload.get("approval_required_value"))))
    if payload.get("digest_env_var") != "LOFTY_UNREVIEWED_FINANCIAL_APPROVAL_QUARANTINE_DIGEST":
        issues.append(issue("unreviewed_financial_quarantine_bad_digest_env", str(payload.get("digest_env_var"))))
    digest = str(payload.get("digest_required_value") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        issues.append(issue("unreviewed_financial_quarantine_bad_digest", digest or "missing"))
    entries = payload.get("entries")
    if not isinstance(entries, list):
        issues.append(issue("unreviewed_financial_quarantine_entries_not_list", type(entries).__name__))
        entries = []
    ready_entries = [entry for entry in entries if isinstance(entry, dict) and entry.get("status") == "ready_to_quarantine"]
    review_entries = [entry for entry in entries if isinstance(entry, dict) and entry.get("status") != "ready_to_quarantine"]
    if compact_count(payload.get("unreviewed_approved_financial_count")) != len(entries):
        issues.append(issue("unreviewed_financial_quarantine_count_mismatch", f"{payload.get('unreviewed_approved_financial_count')}!={len(entries)}"))
    if compact_count(payload.get("ready_to_quarantine_count")) != len(ready_entries):
        issues.append(issue("unreviewed_financial_quarantine_ready_count_mismatch", f"{payload.get('ready_to_quarantine_count')}!={len(ready_entries)}"))
    if compact_count(payload.get("review_count")) != len(review_entries):
        issues.append(issue("unreviewed_financial_quarantine_review_count_mismatch", f"{payload.get('review_count')}!={len(review_entries)}"))
    if compact_count(payload.get("command_count")) != len(ready_entries):
        issues.append(issue("unreviewed_financial_quarantine_command_count_mismatch", f"{payload.get('command_count')}!={len(ready_entries)}"))
    commands_file = Path(str(payload.get("commands_file") or ""))
    if commands_file.name != "lofty_unreviewed_financial_approval_quarantine.requires-explicit-approval.sh":
        issues.append(issue("unreviewed_financial_quarantine_commands_file_unexpected", str(payload.get("commands_file") or "missing")))
    elif commands_file.is_file():
        command_text = commands_file.read_text(encoding="utf-8", errors="replace")
        command_count = compact_count(payload.get("command_count"))
        required_tokens = [
            "LOFTY_UNREVIEWED_FINANCIAL_APPROVAL_QUARANTINE_APPROVED",
            "LOFTY_UNREVIEWED_FINANCIAL_APPROVAL_QUARANTINE_DIGEST",
            digest,
        ]
        if command_count == 0:
            required_tokens.append("exit 0")
        else:
            required_tokens.append("mv --")
        for token in required_tokens:
            if token and token not in command_text:
                issues.append(issue("unreviewed_financial_quarantine_command_missing_token", token))
        forbidden_tokens = ("rm ", "send", "publish", "lofty-pm", "telegram")
        lowered = command_text.lower()
        for token in forbidden_tokens:
            if token in lowered:
                issues.append(issue("unreviewed_financial_quarantine_command_forbidden_token", token.strip()))
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            issues.append(issue("unreviewed_financial_quarantine_bad_entry", str(index)))
            continue
        source = str(entry.get("approved_draft") or "")
        target = str(entry.get("target") or "")
        if source and not is_canonical_financials_path(source, file_name=""):
            issues.append(issue("unreviewed_financial_quarantine_noncanonical_source", source))
        if target and not is_canonical_financials_path(target, file_name=""):
            issues.append(issue("unreviewed_financial_quarantine_noncanonical_target", target))
        if source and not source.endswith("-approved.md") and not source.endswith(".approved.md"):
            issues.append(issue("unreviewed_financial_quarantine_source_not_approved_name", source))
        if target and "review-needed" not in Path(target).name:
            issues.append(issue("unreviewed_financial_quarantine_target_not_review_needed", target))
    return issues


def validate_lofty_financial_payload_contracts(report_dir: Path) -> list[dict]:
    issues = []
    try:
        payload_dirs = [
            entry
            for entry in report_dir.iterdir()
            if entry.is_dir() and entry.name.startswith("lofty-pm-payloads")
        ]
    except OSError as exc:
        return [issue("lofty_financial_payload_contract_scan_failed", f"{report_dir}:{exc}")]
    for payload_dir in sorted(payload_dirs):
        for path in sorted(payload_dir.rglob("*.payload.patch.json")):
            payload, read_issue = read_json(path)
            if read_issue:
                issues.append(issue("lofty_financial_payload_patch_read_error", f"{path.name}:{read_issue}"))
                continue
            if not isinstance(payload, dict):
                continue
            if "cash_flow" not in payload or "projected_annual_cash_flow" not in payload:
                continue
            if not integrity_is_number(payload.get("cash_flow")) or not integrity_is_number(
                payload.get("projected_annual_cash_flow")
            ):
                issues.append(issue("lofty_financial_payload_cash_flow_not_numeric", str(path)))
                continue
            cash_flow = parse_integrity_amount(payload.get("cash_flow"))
            projected_annual = parse_integrity_amount(payload.get("projected_annual_cash_flow"))
            if abs(cash_flow - projected_annual) > 0.01:
                issues.append(
                    issue(
                        "lofty_financial_payload_cash_flow_not_annualized",
                        f"{path}:cash_flow={cash_flow},projected_annual_cash_flow={projected_annual}",
                    )
                )
    return issues


def validate_monthly_pipeline_candidate_coverage(payload: dict) -> list[dict]:
    issues = []
    issues.extend(validate_status(payload, allowed={"ok", "review"}))
    issues.extend(
        require_keys(
            payload,
            {
                "status",
                "generated_at",
                "mismatch_count",
                "input_digests",
                "lofty_publish",
                "owner_email_packet",
                "transfer_reconciliation",
                "telegram_reconciliation",
                "mismatches",
            },
        )
    )
    age_hours = iso_age_hours(payload.get("generated_at"))
    if age_hours is None or age_hours < -1 or age_hours > 30:
        issues.append(issue("monthly_pipeline_candidate_coverage_stale", str(age_hours)))
    mismatch_count = compact_count(payload.get("mismatch_count"))
    mismatches = payload.get("mismatches") if isinstance(payload.get("mismatches"), list) else []
    if payload.get("status") == "ok" and mismatch_count:
        issues.append(issue("monthly_pipeline_candidate_coverage_ok_with_mismatches", str(mismatch_count)))
    if mismatch_count != len(mismatches):
        issues.append(issue("monthly_pipeline_candidate_coverage_mismatch_count_wrong", f"{mismatch_count}!={len(mismatches)}"))
    input_digests = payload.get("input_digests") if isinstance(payload.get("input_digests"), dict) else {}
    if not input_digests:
        issues.append(issue("monthly_pipeline_candidate_coverage_missing_input_digests", "input_digests"))
    for key in (
        "property_update_map",
        "review_candidate_packet",
        "lofty_publish_report",
        "owner_email_packet",
        "discord_send_report",
        "transfer_report",
        "telegram_send_report",
    ):
        digest = str(input_digests.get(key) or "").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            issues.append(issue("monthly_pipeline_candidate_coverage_invalid_input_digest", f"{key}={digest or 'missing'}"))
    transfer = payload.get("transfer_reconciliation") if isinstance(payload.get("transfer_reconciliation"), dict) else {}
    telegram = payload.get("telegram_reconciliation") if isinstance(payload.get("telegram_reconciliation"), dict) else {}
    if not transfer:
        issues.append(issue("monthly_pipeline_candidate_coverage_missing_transfer_section", "transfer_reconciliation"))
    if not telegram:
        issues.append(issue("monthly_pipeline_candidate_coverage_missing_telegram_section", "telegram_reconciliation"))
    source_blocker_count = compact_count(transfer.get("source_blocker_count"))
    source_blockers = transfer.get("source_blockers") if isinstance(transfer.get("source_blockers"), list) else []
    if source_blocker_count != len(source_blockers):
        issues.append(issue("monthly_pipeline_candidate_coverage_source_blocker_count_wrong", f"{source_blocker_count}!={len(source_blockers)}"))
    transfer_digest = str(input_digests.get("transfer_report") or "").strip()
    telegram_current_digest = str(telegram.get("current_transfer_report_digest") or "").strip()
    telegram_report_digest = str(telegram.get("transfer_report_digest") or "").strip()
    if telegram_current_digest and transfer_digest and telegram_current_digest != transfer_digest:
        issues.append(
            issue(
                "monthly_pipeline_candidate_coverage_telegram_current_digest_mismatch",
                f"telegram={telegram_current_digest},input={transfer_digest}",
            )
        )
    if telegram_report_digest and transfer_digest and telegram_report_digest != transfer_digest:
        issues.append(
            issue(
                "monthly_pipeline_candidate_coverage_telegram_report_digest_mismatch",
                f"telegram={telegram_report_digest},input={transfer_digest}",
            )
        )
    if telegram.get("transfer_report_digest_matches_current") is not True:
        issues.append(issue("monthly_pipeline_candidate_coverage_telegram_digest_not_current", str(telegram.get("transfer_report_digest_matches_current"))))
    if transfer.get("recommended_send_to_lofty_total_is_final") is not True and telegram.get("telegram_send_ok") is True and telegram.get("dry_run") is not True:
        issues.append(issue("monthly_pipeline_candidate_coverage_live_telegram_for_nonfinal_transfer", str(transfer.get("status") or "missing")))
    return issues


REPORT_VALIDATORS = {
    "baselane_monthly_owner_review_gate.json": validate_owner_review_gate,
    "baselane_financials_goal_audit.json": validate_goal_audit,
    "baselane_financials_post_auth_resume_report.json": validate_post_auth_resume,
    "baselane_eod_telegram_report.json": validate_eod_telegram_report,
    "baselane_financials_monthly_readiness.json": validate_monthly_readiness,
    "baselane_financials_monthly_run_report.json": validate_monthly_run,
    "baselane_financials_monthly_close_status.json": validate_monthly_close_status,
    "baselane_financials_monthly_lofty_pm_publish.json": validate_lofty_pm_publish,
    "baselane_financials_monthly_guarded_apply.json": validate_monthly_guarded_apply,
    "baselane_financials_monthly_live_update_capture.json": validate_monthly_live_update_capture,
    "baselane_financials_monthly_live_financial_capture.json": validate_monthly_live_financial_capture,
    "lofty_financial_patch_readiness.json": validate_lofty_financial_patch_readiness,
    "lofty_public_path_guard_report.json": validate_public_path_guard,
    "baselane_no_mortgage_financials_cleanup_report.json": validate_no_mortgage_financials,
    "future_cf_statement_values_apply_report.json": validate_future_cf_statement_values_apply,
    "future_cf_statement_values_clear_report.json": validate_future_cf_statement_values,
    "baselane_weekly_file_updates_run_report.json": validate_weekly_file_updates,
    "baselane_weekly_cf_statement_sync_report.json": validate_weekly_cf_sync,
    "baselane_monthly_statements_idempotent_report.json": validate_monthly_statements_idempotent,
    "baselane_monthly_owner_email_send_guard.json": validate_owner_email_guard,
    "baselane_monthly_owner_email_packet.json": validate_owner_email_packet,
    "baselane_monthly_pipeline_candidate_coverage_audit.json": validate_monthly_pipeline_candidate_coverage,
    "baselane_financials_monthly_review_candidate_packet.json": validate_monthly_review_candidate_packet,
    "lofty_empty_updates_backfill_queue.json": validate_empty_updates_backfill_queue,
    "lofty_listing_update_cleanup_queue.json": validate_listing_cleanup_queue,
    "lofty_listing_cleanup_dry_run_verify.json": validate_listing_cleanup_dry_run_verify,
    "lofty_listing_update_cleanup_queue.live-apply-preflight.json": validate_listing_cleanup_apply_preflight,
    "lofty_listing_update_cleanup_queue.local-live-verify.json": validate_listing_cleanup_local_live_verify,
    "baselane_daily_run_report.json": validate_daily_run,
    "baselane_daily_sync_report.json": validate_daily_sync,
    "lofty_cdp_preflight_report.json": validate_lofty_cdp_preflight,
}


def validate_reports(report_dir: Path, *, max_local_model_age_hours: float) -> dict:
    reports = {}
    payloads = {}
    all_issues = []
    for name, validator in REPORT_VALIDATORS.items():
        path = report_dir / name
        payload, read_issue = read_json(path)
        if read_issue is None:
            payloads[name] = payload or {}
        issues = [issue("report_read_error", read_issue)] if read_issue else validator(payload or {})
        reports[name] = {
            "status": "ok" if not issues else "review",
            "path": str(path),
            "issue_count": len(issues),
            "issues": issues,
        }
        all_issues.extend({"report": name, **item} for item in issues)

    eod_preview_name = "baselane_eod_telegram_preview_report.json"
    eod_preview_path = report_dir / eod_preview_name
    if eod_preview_path.exists():
        eod_preview_payload, eod_preview_read_issue = read_json(eod_preview_path)
        if eod_preview_read_issue is None:
            payloads[eod_preview_name] = eod_preview_payload or {}
        eod_preview_issues = (
            [issue("report_read_error", eod_preview_read_issue)]
            if eod_preview_read_issue
            else validate_eod_telegram_report(eod_preview_payload or {})
        )
        reports[eod_preview_name] = {
            "status": "ok" if not eod_preview_issues else "review",
            "path": str(eod_preview_path),
            "issue_count": len(eod_preview_issues),
            "issues": eod_preview_issues,
        }
        all_issues.extend({"report": eod_preview_name, **item} for item in eod_preview_issues)

    canonical_eod_name = "baselane_eod_telegram_report.json"
    if canonical_eod_name in reports and eod_preview_name in payloads:
        canonical_eod = payloads.get(canonical_eod_name) or {}
        preview_eod = payloads.get(eod_preview_name) or {}
        canonical_time = iso_timestamp(canonical_eod.get("generated_at"))
        preview_time = iso_timestamp(preview_eod.get("generated_at"))
        preview_quality = preview_eod.get("message_quality") if isinstance(preview_eod.get("message_quality"), dict) else {}
        preview_daily = preview_eod.get("daily_sync_summary") if isinstance(preview_eod.get("daily_sync_summary"), dict) else {}
        preview_is_current_health = (
            preview_time
            and (not canonical_time or preview_time > canonical_time)
            and preview_quality.get("ok") is True
            and preview_daily.get("effective_status") == "ok"
            and preview_daily.get("sync_report_status") == "ok"
        )
        if preview_is_current_health:
            stale_eod_codes = {
                "eod_report_owner_exclusion_active_totals_mismatch",
                "eod_report_owner_exclusion_active_totals_match_false",
                "eod_report_daily_sync_summary_not_ok",
                "eod_report_daily_sync_local_model_not_ready",
                "eod_report_daily_sync_issue_count_nonzero",
                "eod_report_goal_audit_refresh_not_attempted",
                "eod_report_goal_audit_refresh_not_ok",
                "eod_report_empty_updates_queue_refresh_not_attempted",
                "eod_report_empty_updates_queue_refresh_not_ok",
            }
            kept = [
                item
                for item in reports[canonical_eod_name]["issues"]
                if item.get("code") not in stale_eod_codes
            ]
            reports[canonical_eod_name]["issues"] = kept
            reports[canonical_eod_name]["issue_count"] = len(kept)
            reports[canonical_eod_name]["status"] = "ok" if not kept else "review"
            all_issues = [
                item
                for item in all_issues
                if item.get("report") != canonical_eod_name or item.get("code") not in stale_eod_codes
            ]

    quarantine_name = "lofty_unreviewed_financial_approval_quarantine.json"
    quarantine_path = report_dir / quarantine_name
    if quarantine_path.exists():
        quarantine_payload, quarantine_read_issue = read_json(quarantine_path)
        if quarantine_read_issue is None:
            payloads[quarantine_name] = quarantine_payload or {}
        quarantine_issues = (
            [issue("report_read_error", quarantine_read_issue)]
            if quarantine_read_issue
            else validate_unreviewed_financial_approval_quarantine(quarantine_payload or {})
        )
        reports[quarantine_name] = {
            "status": "ok" if not quarantine_issues else "review",
            "path": str(quarantine_path),
            "issue_count": len(quarantine_issues),
            "issues": quarantine_issues,
        }
        all_issues.extend({"report": quarantine_name, **item} for item in quarantine_issues)

    local_model_name = "baselane_local_model_preflight_report.json"
    local_model_path = report_dir / local_model_name
    local_model_payload, local_model_read_issue = read_json(local_model_path)
    if local_model_read_issue is None:
        payloads[local_model_name] = local_model_payload or {}
    local_model_issues = (
        [issue("report_read_error", local_model_read_issue)]
        if local_model_read_issue
        else validate_local_model_preflight(local_model_payload or {}, max_age_hours=max_local_model_age_hours)
    )
    reports[local_model_name] = {
        "status": "ok" if not local_model_issues else "review",
        "path": str(local_model_path),
        "issue_count": len(local_model_issues),
        "issues": local_model_issues,
    }
    all_issues.extend({"report": local_model_name, **item} for item in local_model_issues)

    eod_send_state_name = "baselane_eod_telegram_send_state.json"
    eod_send_state_path = report_dir / eod_send_state_name
    if eod_send_state_path.exists():
        eod_send_state_payload, eod_send_state_read_issue = read_json(eod_send_state_path)
        eod_send_state_issues = (
            [issue("report_read_error", eod_send_state_read_issue)]
            if eod_send_state_read_issue
            else validate_eod_send_state(eod_send_state_payload or {}, report_dir=report_dir)
        )
        reports[eod_send_state_name] = {
            "status": "ok" if not eod_send_state_issues else "review",
            "path": str(eod_send_state_path),
            "issue_count": len(eod_send_state_issues),
            "issues": eod_send_state_issues,
        }
        all_issues.extend({"report": eod_send_state_name, **item} for item in eod_send_state_issues)

    statement_hygiene_name = "baselane_statement_folder_hygiene"
    statement_hygiene_issues: list[dict] = []
    statement_hygiene_roots: list[Path] = []
    for candidate in real_estate_root_candidates(report_dir):
        if candidate.exists():
            statement_hygiene_roots.append(candidate)
            statement_hygiene_issues.extend(validate_statement_folder_hygiene(candidate))
    reports[statement_hygiene_name] = {
        "status": "ok" if not statement_hygiene_issues else "review",
        "path": os.pathsep.join(str(root) for root in statement_hygiene_roots) if statement_hygiene_roots else None,
        "checked_roots": [str(root) for root in statement_hygiene_roots],
        "issue_count": len(statement_hygiene_issues),
        "issues": statement_hygiene_issues,
    }
    all_issues.extend({"report": statement_hygiene_name, **item} for item in statement_hygiene_issues)

    stale_financial_artifacts_name = "baselane_stale_financial_artifact_guard"
    stale_financial_artifact_issues: list[dict] = []
    stale_financial_artifact_roots: list[Path] = []
    for candidate in real_estate_root_candidates(report_dir):
        if candidate.exists():
            stale_financial_artifact_roots.append(candidate)
            stale_financial_artifact_issues.extend(validate_stale_financial_artifacts(candidate))
    reports[stale_financial_artifacts_name] = {
        "status": "ok" if not stale_financial_artifact_issues else "review",
        "path": os.pathsep.join(str(root) for root in stale_financial_artifact_roots) if stale_financial_artifact_roots else None,
        "checked_roots": [str(root) for root in stale_financial_artifact_roots],
        "issue_count": len(stale_financial_artifact_issues),
        "issues": stale_financial_artifact_issues,
        "policy": "Do not retain legacy reconciliation markdown or 2026-01/2026-02 P&L exports in Dropbox property trees.",
    }
    all_issues.extend({"report": stale_financial_artifacts_name, **item} for item in stale_financial_artifact_issues)

    stale_action_text_name = "baselane_stale_action_text_guard"
    stale_action_text_issues = validate_stale_action_text(report_dir)
    reports[stale_action_text_name] = {
        "status": "ok" if not stale_action_text_issues else "review",
        "path": str(report_dir),
        "issue_count": len(stale_action_text_issues),
        "issues": stale_action_text_issues,
    }
    all_issues.extend({"report": stale_action_text_name, **item} for item in stale_action_text_issues)

    operator_topology_name = "baselane_operator_topology_scope_guard"
    operator_topology_issues = validate_operator_topology_scope(report_dir)
    reports[operator_topology_name] = {
        "status": "ok" if not operator_topology_issues else "review",
        "path": str(report_dir),
        "checked_reports": [name for name in OPERATOR_TOPOLOGY_REPORTS if (report_dir / name).exists()],
        "forbidden_tokens": [code for _token, code in OPERATOR_TOPOLOGY_FORBIDDEN_TOKENS],
        "issue_count": len(operator_topology_issues),
        "issues": operator_topology_issues,
    }
    all_issues.extend({"report": operator_topology_name, **item} for item in operator_topology_issues)

    yhome_target_name = "yhome_operating_cash_target_columns_guard"
    yhome_target_issues = validate_yhome_operating_cash_targets(report_dir)
    reports[yhome_target_name] = {
        "status": "ok" if not yhome_target_issues else "review",
        "path": str(report_dir),
        "expected_target_columns": list(YHOME_OPERATING_CASH_TARGET_COLUMNS),
        "checked_reports": [name for name in YHOME_OPERATING_CASH_REPORTS if (report_dir / name).exists()],
        "issue_count": len(yhome_target_issues),
        "issues": yhome_target_issues,
    }
    all_issues.extend({"report": yhome_target_name, **item} for item in yhome_target_issues)

    monthly_summary_name = "monthly_update_summary_exclusion_guard"
    monthly_summary_issues = validate_monthly_update_summary_exclusions(report_dir, payloads)
    reports[monthly_summary_name] = {
        "status": "ok" if not monthly_summary_issues else "review",
        "path": str(
            Path(os.environ.get("COMMS_WORKSPACE") or report_dir.parent.parent / "workspace-lofty-vp")
            / "updates"
        ),
        "issue_count": len(monthly_summary_issues),
        "issues": monthly_summary_issues,
    }
    all_issues.extend({"report": monthly_summary_name, **item} for item in monthly_summary_issues)

    lofty_payload_contract_name = "lofty_financial_payload_contract_guard"
    lofty_payload_contract_issues = validate_lofty_financial_payload_contracts(report_dir)
    reports[lofty_payload_contract_name] = {
        "status": "ok" if not lofty_payload_contract_issues else "review",
        "path": str(report_dir),
        "issue_count": len(lofty_payload_contract_issues),
        "issues": lofty_payload_contract_issues,
        "policy": "Lofty property-owners API cash_flow is annualized; UI Current Month Distribution is cash_flow / 12.",
    }
    all_issues.extend({"report": lofty_payload_contract_name, **item} for item in lofty_payload_contract_issues)

    transfer_reconciliation_name = "baselane_lofty_transfer_requirements.json"
    transfer_reconciliation_path = report_dir / transfer_reconciliation_name
    if transfer_reconciliation_path.exists() and transfer_reconciliation_name not in payloads:
        transfer_reconciliation_payload, transfer_reconciliation_read_issue = read_json(transfer_reconciliation_path)
        if transfer_reconciliation_read_issue is None:
            payloads[transfer_reconciliation_name] = transfer_reconciliation_payload or {}
        else:
            reports[transfer_reconciliation_name] = {
                "status": "review",
                "path": str(transfer_reconciliation_path),
                "issue_count": 1,
                "issues": [issue("report_read_error", transfer_reconciliation_read_issue)],
            }
            all_issues.append(
                {
                    "report": transfer_reconciliation_name,
                    **issue("report_read_error", transfer_reconciliation_read_issue),
                }
            )
    monthly_transfer_alias_name = "baselane_monthly_transfer_reconciliation_report.json"
    monthly_transfer_alias_path = report_dir / monthly_transfer_alias_name
    if monthly_transfer_alias_path.exists() and monthly_transfer_alias_name not in payloads:
        monthly_transfer_alias_payload, monthly_transfer_alias_read_issue = read_json(monthly_transfer_alias_path)
        if monthly_transfer_alias_read_issue is None:
            payloads[monthly_transfer_alias_name] = monthly_transfer_alias_payload or {}
        else:
            reports[monthly_transfer_alias_name] = {
                "status": "review",
                "path": str(monthly_transfer_alias_path),
                "issue_count": 1,
                "issues": [issue("report_read_error", monthly_transfer_alias_read_issue)],
            }
            all_issues.append(
                {
                    "report": monthly_transfer_alias_name,
                    **issue("report_read_error", monthly_transfer_alias_read_issue),
                }
            )

    legacy_monthly_transfer_alias_name = "baselane_monthly_transfer_reconciliation.json"
    legacy_monthly_transfer_alias_path = report_dir / legacy_monthly_transfer_alias_name
    if legacy_monthly_transfer_alias_path.exists() and legacy_monthly_transfer_alias_name not in payloads:
        legacy_monthly_transfer_alias_payload, legacy_monthly_transfer_alias_read_issue = read_json(
            legacy_monthly_transfer_alias_path
        )
        if legacy_monthly_transfer_alias_read_issue is None:
            payloads[legacy_monthly_transfer_alias_name] = legacy_monthly_transfer_alias_payload or {}
        else:
            reports[legacy_monthly_transfer_alias_name] = {
                "status": "review",
                "path": str(legacy_monthly_transfer_alias_path),
                "issue_count": 1,
                "issues": [issue("report_read_error", legacy_monthly_transfer_alias_read_issue)],
            }
            all_issues.append(
                {
                    "report": legacy_monthly_transfer_alias_name,
                    **issue("report_read_error", legacy_monthly_transfer_alias_read_issue),
                }
            )

    obsolete_transfer_telegram_path = report_dir / "baselane_lofty_transfer_requirements_telegram.md"
    if obsolete_transfer_telegram_path.exists():
        stale_issue = issue(
            "monthly_transfer_reconciliation_obsolete_telegram_markdown_path",
            "Use reports/baselane_lofty_transfer_requirements.telegram.md; remove stale underscore-named generated artifact.",
        )
        reports["monthly_transfer_reconciliation_obsolete_telegram_markdown_path"] = {
            "status": "review",
            "path": str(obsolete_transfer_telegram_path),
            "issue_count": 1,
            "issues": [stale_issue],
        }
        all_issues.append(
            {
                "report": "monthly_transfer_reconciliation_obsolete_telegram_markdown_path",
                **stale_issue,
            }
        )

    cross_issues = validate_cross_report_consistency(payloads)
    if cross_issues:
        reports["cross_report_consistency"] = {
            "status": "review",
            "path": str(report_dir),
            "issue_count": len(cross_issues),
            "issues": cross_issues,
        }
        all_issues.extend({"report": "cross_report_consistency", **item} for item in cross_issues)
    return {
        "status": "ok" if not all_issues else "review",
        "generated_at": iso_z(),
        "report_dir": str(report_dir),
        "issue_count": len(all_issues),
        "issues": all_issues,
        "reports": reports,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=default_root())
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--local-model-max-age-hours", type=float, default=30.0)
    args = parser.parse_args()

    root = args.root.resolve()
    report_dir = (args.report_dir or root / "reports").resolve()
    out_report = args.report or report_dir / "baselane_report_integrity_guard.json"
    payload = validate_reports(report_dir, max_local_model_age_hours=args.local_model_max_age_hours)
    out_report.parent.mkdir(parents=True, exist_ok=True)
    out_report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
