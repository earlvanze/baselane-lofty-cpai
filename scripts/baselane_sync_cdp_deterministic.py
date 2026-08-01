#!/usr/bin/env python3
import argparse
import csv
import fcntl
import hashlib
import json
import os
import re
import shlex
import signal
import shutil
import subprocess
import sys
import urllib.request
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, TextIO
from contextlib import contextmanager

from baselane_brave_utils import brave_port_candidates, cdp_diagnostics, resolve_brave_cdp_version

SCRIPT_PATH = Path(__file__).absolute()
ROOT = Path(os.environ.get('WORKSPACE_ROOT') or os.environ.get('OPENCLAW_WORKSPACE') or SCRIPT_PATH.parents[1])
REPORT_DIR = Path(os.environ.get('BASELANE_REPORT_DIR') or ROOT / 'reports')
REPORT = REPORT_DIR / 'baselane_sync_cdp_report.json'
EXPORT_SCRIPT = ROOT / 'scripts' / 'baselane_export_human_paced.js'
AUTH_PREFLIGHT = ROOT / 'scripts' / 'baselane_cdp_auth_recovery.py'
SPLIT_SCRIPT = ROOT / 'scripts' / 'split_ledger_public_financials.py'
FIRST_DAY_PM_FEE_CLEANUP_SCRIPT = ROOT / 'scripts' / 'baselane_first_day_pm_fee_source_cleanup_plan.py'
NATIVE_SPLIT_PLAN_SCRIPT = ROOT / 'scripts' / 'baselane_native_split_plan.py'
NATIVE_SPLIT_APPLY_SCRIPT = ROOT / 'scripts' / 'baselane_apply_native_splits.py'
NATIVE_SPLIT_LEDGER_OVERLAY_SCRIPT = ROOT / 'scripts' / 'baselane_apply_native_split_ledger_overlay.py'
PENDING_TRANSACTION_AUDIT_SCRIPT = ROOT / 'scripts' / 'baselane_audit_recent_dao_eco_transactions.py'
MONTHLY_ACCRUALS_SCRIPT = ROOT / 'scripts' / 'baselane_monthly_accruals_idempotent.py'
ISSUE_CLASS = 'baselane-sync-cdp-deterministic'
DEFAULT_EXPORT_TIMEOUT_SECONDS = 900
PIPELINE_LOCK_PATH = Path(os.environ.get('BASELANE_SOURCE_PIPELINE_LOCK', '/tmp/baselane-source-pipeline.lock'))
PIPELINE_LOCK_HELD_ENV = 'BASELANE_SOURCE_PIPELINE_LOCK_HELD'
LOCAL_RETAINED_NORMALIZATION_MONTH = '2026-06'
LOCAL_RETAINED_NORMALIZATION_PROPERTIES = (
    '84 Madison Ave', '86 Madison Ave', '88 Madison Ave',
)
ECO_ACCRUAL_NOTE = re.compile(
    r'^AOPS-(?:(?:MONTHLY|OHIL|PAU|PNL)-ACCRUAL|PM-FEE)'
    r'\|(dao_eco|pm_eco)\|([^|]+)\|\d{4}-\d{2}'
    r'\|(-?\d+(?:\.\d{1,2})?)(?:\s|\||$)'
)


def verified_eco_accrual_row(row: dict[str, Any]) -> tuple[str, str] | None:
    marker = ECO_ACCRUAL_NOTE.match(str(row.get('Notes') or '').strip())
    if not marker:
        return None
    kind, target, marker_amount = marker.groups()
    try:
        amount_matches = abs(
            float(str(row.get('Amount') or '').replace('$', '').replace(',', '').strip())
            - float(marker_amount)
        ) <= 0.001
    except ValueError:
        return None
    expected_prefix = (
        'ECO Systems LLC DAO Registration Fee Revenue | '
        if kind == 'dao_eco'
        else 'ECO Systems LLC PM Fee Revenue | '
    )
    if (
        not amount_matches
        or str(row.get('Type') or '').strip() != 'Revenue'
        or str(row.get('Category') or '').strip() != 'Fees & Other Revenue'
        or not str(row.get('Merchant') or '').strip().startswith(expected_prefix)
        or (
            str(row.get('Description') or '').strip()
            and not str(row.get('Description') or '').strip().startswith(expected_prefix)
        )
    ):
        return None
    return kind, target.strip()


@contextmanager
def exclusive_pipeline_lock(path: Path = PIPELINE_LOCK_PATH) -> Iterator[bool]:
    """Acquire the shared Baselane source-pipeline lock without waiting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+', encoding='utf-8') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def diagnostic_command() -> str:
    return f'python3 {shlex.quote(str(SCRIPT_PATH))} --json'


DIAGNOSTIC_COMMAND = diagnostic_command()


def auth_preflight_command(cdp_url: str, report_path: Path) -> list[str]:
    return [
        sys.executable,
        str(AUTH_PREFLIGHT),
        '--cdp-url',
        cdp_url,
        '--graphql-auth-smoke',
        '--report',
        str(report_path),
    ]


def ensure_cdp_running():
    # In Umbrel container, Brave runs on Windows host with CDP port forwarded.
    # Resolve the Windows host gateway and connect to port 19222.
    try:
        resolve_brave_cdp_version(timeout=5)
        return True
    except Exception:
        pass
    # In this environment, we cannot launch a browser - it must be running on the host.
    return False


def redact_output(text: str, max_chars: int) -> str:
    text = text or ''
    patterns = (
        (r'--session\s+\S+', '--session <redacted>'),
        (r'(BW_SESSION=)[^\s]+', r'\1<redacted>'),
        (r'([?&](?:token|session|key|code)=)[^&\s]+', r'\1<redacted>'),
    )
    import re

    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text[-max_chars:]


def read_json_file(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def resolve_pipeline_storage_paths(
    environment: dict[str, str] | None = None,
    default_dropbox_candidates: tuple[Path, ...] | None = None,
) -> dict[str, Path]:
    """Resolve one canonical storage lane for export, cleanup, and splitting."""
    env = os.environ if environment is None else environment
    candidates = default_dropbox_candidates or (
        Path('/mnt/c/Users/digit/Dropbox'),
        Path('/data/Dropbox'),
        Path.home() / 'Dropbox',
        Path('/home/digit/Dropbox'),
        ROOT / 'Dropbox',
    )
    explicit_ledger_path = str(env.get('BASELANE_LEDGER_PATH') or '').strip()
    explicit_ledger_dir = str(env.get('BASELANE_LEDGER_DIR') or '').strip()
    explicit_dropbox = str(env.get('DROPBOX_ROOT') or '').strip()
    if explicit_dropbox:
        dropbox_root = Path(explicit_dropbox)
    else:
        dropbox_root = next((path for path in candidates if path.exists()), candidates[0])
    if explicit_ledger_dir:
        ledger_dir = Path(explicit_ledger_dir)
    elif explicit_ledger_path:
        ledger_dir = Path(explicit_ledger_path).parent
    else:
        ledger_candidates = (
            dropbox_root / 'Projects/assetrail',
            dropbox_root / 'Projects/transaction_tracker',
        )
        ledger_dir = next((path for path in ledger_candidates if path.exists()), ledger_candidates[0])
    ledger_path = (
        Path(explicit_ledger_path)
        if explicit_ledger_path
        else ledger_dir / 'ECO Systems General Ledger.csv'
    )
    return {
        'dropbox_root': dropbox_root,
        'ledger_dir': ledger_dir,
        'ledger_path': ledger_path,
    }


def configure_pipeline_storage_paths(report: dict) -> dict[str, Path]:
    paths = resolve_pipeline_storage_paths()
    os.environ['DROPBOX_ROOT'] = str(paths['dropbox_root'])
    os.environ['BASELANE_LEDGER_DIR'] = str(paths['ledger_dir'])
    os.environ['BASELANE_LEDGER_PATH'] = str(paths['ledger_path'])
    report['pipeline_storage_paths'] = {key: str(value) for key, value in paths.items()}
    return paths


def file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def reconcile_canonical_ledger_from_login_report(root: Path, report: dict, settle_seconds: float | None = None) -> bool:
    export_report = read_json_file(root / 'reports' / 'baselane_export_report.json')
    export_output_raw = str(export_report.get('output') or '').strip()
    export_checked_at_raw = str(export_report.get('checked_at') or '').strip()
    try:
        export_checked_at = datetime.fromisoformat(export_checked_at_raw.replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        export_checked_at = 0.0
    run_started_at = float(report.get('started_at') or 0)
    configured_path_raw = str(
        ((report.get('pipeline_storage_paths') or {}).get('ledger_path'))
        or os.environ.get('BASELANE_LEDGER_PATH')
        or ''
    ).strip()
    current_export = {
        'ok': export_report.get('ok') is True,
        'output': export_output_raw or None,
        'checked_at': export_checked_at_raw or None,
        'run_started_at': run_started_at or None,
    }
    if (
        export_report.get('ok') is True
        and export_output_raw
        and export_checked_at >= run_started_at - 5
    ):
        export_output = Path(export_output_raw)
        configured_path = Path(configured_path_raw) if configured_path_raw else export_output
        current_export['output_matches_configured_ledger'] = export_output == configured_path
        current_export['canonical_sha256'] = file_sha256(export_output)
        if export_output == configured_path and current_export['canonical_sha256']:
            current_export['status'] = 'ok_current_run_export_authoritative'
            report['canonical_ledger_reconcile'] = {
                'attempted': False,
                'status': 'ok_current_run_export_authoritative',
                'current_export': current_export,
                'reason': 'the active CDP exporter wrote the configured canonical ledger directly',
            }
            return True

    login_report = read_json_file(root / 'reports' / 'baselane_login_export_report.json')
    canonical_path_raw = str(login_report.get('canonical_path') or '').strip()
    snapshot_path_raw = str(login_report.get('filtered_snapshot') or '').strip()
    expected_sha = str(login_report.get('canonical_sha256') or '').strip()
    reconcile = {
        'attempted': False,
        'current_export': current_export,
        'login_report_ok': login_report.get('ok') is True,
        'canonical_path': canonical_path_raw or None,
        'filtered_snapshot': snapshot_path_raw or None,
        'expected_sha256': expected_sha or None,
    }
    report['canonical_ledger_reconcile'] = reconcile
    if login_report.get('ok') is not True or not canonical_path_raw or not snapshot_path_raw:
        reconcile['status'] = 'skipped_missing_login_export_paths'
        return True
    canonical_path = Path(canonical_path_raw)
    snapshot_path = Path(snapshot_path_raw)
    if not canonical_path.is_absolute():
        canonical_path = root / canonical_path
    if not snapshot_path.is_absolute():
        snapshot_path = root / snapshot_path
    snapshot_sha = file_sha256(snapshot_path)
    canonical_sha_before = file_sha256(canonical_path)
    reconcile.update(
        {
            'canonical_path': str(canonical_path),
            'filtered_snapshot': str(snapshot_path),
            'snapshot_sha256': snapshot_sha,
            'canonical_sha256_before': canonical_sha_before,
        }
    )
    if not snapshot_sha:
        reconcile['status'] = 'failed_missing_filtered_snapshot'
        return False
    if expected_sha and expected_sha != snapshot_sha:
        reconcile['status'] = 'failed_snapshot_sha_mismatch'
        return False
    if canonical_sha_before == snapshot_sha:
        reconcile['status'] = 'ok_already_current'
        reconcile['canonical_sha256_after'] = canonical_sha_before
        return True
    reconcile['attempted'] = True
    copied = subprocess.run(['cp', '-f', str(snapshot_path), str(canonical_path)], text=True, capture_output=True)
    reconcile['copy_return_code'] = copied.returncode
    reconcile['copy_stderr_tail'] = redact_output(copied.stderr or '', 1000)
    if copied.returncode != 0:
        reconcile['status'] = 'failed_copy'
        return False
    try:
        os.sync()
    except AttributeError:
        pass
    sleep_seconds = float(
        os.environ.get('BASELANE_CANONICAL_RECONCILE_SETTLE_SECONDS')
        or (settle_seconds if settle_seconds is not None else 15)
    )
    reconcile['settle_seconds'] = sleep_seconds
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)
    canonical_sha_after = file_sha256(canonical_path)
    reconcile['canonical_sha256_after'] = canonical_sha_after
    if canonical_sha_after != snapshot_sha:
        reconcile['status'] = 'failed_post_copy_sha_mismatch'
        return False
    reconcile['status'] = 'ok_reconciled'
    return True


def remap_verified_dao_eco_rows(canonical_path: Path) -> dict[str, Any]:
    """Map exact excluded ECO DAO accrual rows back to their marker property."""
    with canonical_path.open('r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    remapped = []
    for row in rows:
        source_property = str(row.get('Property') or '').strip().lower().replace('&', 'and')
        verified = verified_eco_accrual_row(row)
        if not (
            verified
            and source_property == 'mining, sales, consulting, and pm'
        ):
            continue
        target = verified[1]
        row['Property'] = target
        remapped.append(target)
    if remapped:
        with canonical_path.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    return {'remapped_row_count': len(remapped), 'properties': sorted(remapped)}


def rehydrate_indexed_dao_eco_rows(canonical_path: Path, source_index_path: Path) -> dict[str, Any]:
    """Restore exact live ECO-side accrual rows omitted by the canonical export filter."""
    result = {
        'canonical_path': str(canonical_path),
        'source_index_path': str(source_index_path),
        'appended_row_count': 0,
        'already_present_row_count': 0,
        'status': 'skipped_missing_source_index',
    }
    if not canonical_path.is_file() or not source_index_path.is_file():
        return result
    with canonical_path.open('r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    existing_notes = {str(row.get('Notes') or '').strip() for row in rows}
    with source_index_path.open('r', encoding='utf-8-sig', newline='') as handle:
        source_rows = list(csv.DictReader(handle))
    additions = []
    for row in source_rows:
        note = str(row.get('Notes') or '').strip()
        verified = verified_eco_accrual_row(row)
        if not verified:
            continue
        if note in existing_notes:
            result['already_present_row_count'] += 1
            continue
        if str(row.get('Property') or '').strip() != verified[1]:
            continue
        additions.append({field: str(row.get(field) or '') for field in fieldnames})
        existing_notes.add(note)
    if additions:
        with canonical_path.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows + additions)
    result['appended_row_count'] = len(additions)
    result['status'] = 'ok'
    return result


def run_local_retained_capital_normalization(root: Path, report: dict) -> int:
    """Optionally replay explicitly reviewed June local-ledger corrections.

    Raw Baselane exports remain authoritative for source transactions, but they
    can reintroduce stale pre-governance values into the reporting ledger.  This
    replay is therefore opt-in: it mutates a local ledger and must not run as an
    implicit side effect of a raw sync while retained-capital evidence is under
    reconciliation.
    """
    normalization = {
        'enabled': os.environ.get('BASELANE_REAPPLY_JUNE_RETAINED_NORMALIZATIONS', '0') == '1',
        'month': LOCAL_RETAINED_NORMALIZATION_MONTH,
        'properties': list(LOCAL_RETAINED_NORMALIZATION_PROPERTIES),
        'mutation_scope': 'local_reporting_ledger_only',
    }
    report['local_retained_capital_normalization'] = normalization
    if not normalization['enabled']:
        normalization['status'] = 'skipped_disabled'
        return 0
    login_report = read_json_file(root / 'reports' / 'baselane_login_export_report.json')
    canonical_path_raw = str(login_report.get('canonical_path') or '').strip()
    script = root / 'scripts' / 'baselane_monthly_accruals_idempotent.py'
    if not canonical_path_raw or not script.is_file():
        normalization['status'] = 'failed_missing_inputs'
        normalization['canonical_path'] = canonical_path_raw or None
        normalization['script'] = str(script)
        return 2
    canonical_path = Path(canonical_path_raw)
    if not canonical_path.is_absolute():
        canonical_path = root / canonical_path
    dao_eco_remap = remap_verified_dao_eco_rows(canonical_path)
    normalization['dao_eco_remap'] = dao_eco_remap
    month_key = LOCAL_RETAINED_NORMALIZATION_MONTH.replace('-', '')
    normalization_report = root / 'reports' / f'baselane_local_retained_normalization_{month_key}.json'
    normalization_review = root / 'reports' / f'baselane_local_retained_normalization_{month_key}.md'
    command = [
        'python3', str(script), '--gl-csv', str(canonical_path),
        '--month', LOCAL_RETAINED_NORMALIZATION_MONTH,
        '--kind', 'retained_capital', '--update-amount-mismatches', '--apply',
        '--report', str(normalization_report), '--review-markdown', str(normalization_review),
    ]
    for property_name in LOCAL_RETAINED_NORMALIZATION_PROPERTIES:
        command.extend(['--property', property_name])
    completed = subprocess.run(command, text=True, capture_output=True, timeout=300)
    result = read_json_file(normalization_report)
    normalization.update(
        {
            'attempted': True,
            'canonical_path': str(canonical_path),
            'report': str(normalization_report),
            'review_markdown': str(normalization_review),
            'return_code': completed.returncode,
            'stdout_tail': redact_output(completed.stdout or '', 2000),
            'stderr_tail': redact_output(completed.stderr or '', 2000),
            'normalizer_status': result.get('status'),
            'updated_amount_mismatch_count': result.get('updated_amount_mismatch_count'),
            'updated_amount_mismatches': result.get('updated_amount_mismatches') or [],
        }
    )
    normalization['status'] = 'ok' if completed.returncode == 0 and result.get('status') == 'ok' else 'review'
    return 0 if normalization['status'] == 'ok' else 2


def publish_post_cleanup_canonical_baseline(root: Path, report: dict) -> bool:
    login_report_path = root / 'reports' / 'baselane_login_export_report.json'
    login_report = read_json_file(login_report_path)
    canonical_path_raw = str(login_report.get('canonical_path') or '').strip()
    baseline = {
        'attempted': False,
        'login_report_ok': login_report.get('ok') is True,
        'canonical_path': canonical_path_raw or None,
    }
    report['post_cleanup_canonical_baseline'] = baseline
    if login_report.get('ok') is not True or not canonical_path_raw:
        baseline['status'] = 'skipped_missing_login_export_paths'
        return True
    canonical_path = Path(canonical_path_raw)
    if not canonical_path.is_absolute():
        canonical_path = root / canonical_path
    canonical_sha = file_sha256(canonical_path)
    baseline.update(
        {
            'attempted': True,
            'canonical_path': str(canonical_path),
            'canonical_sha256': canonical_sha,
            'raw_filtered_snapshot': login_report.get('filtered_snapshot'),
            'raw_filtered_snapshot_sha256': login_report.get('canonical_sha256'),
        }
    )
    if not canonical_sha:
        baseline['status'] = 'failed_canonical_unreadable'
        return False
    login_report['post_cleanup_baseline'] = True
    login_report['post_cleanup_canonical_sha256'] = canonical_sha
    login_report['post_cleanup_canonical_size_bytes'] = canonical_path.stat().st_size
    login_report['raw_filtered_snapshot'] = login_report.get('filtered_snapshot')
    login_report['raw_filtered_snapshot_sha256'] = login_report.get('canonical_sha256')
    login_report['canonical_sha256'] = canonical_sha
    tmp = login_report_path.with_suffix(login_report_path.suffix + '.tmp')
    tmp.write_text(json.dumps(login_report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    tmp.replace(login_report_path)
    baseline['status'] = 'ok'
    baseline['login_export_report_updated'] = True
    return True


def verify_canonical_ledger_unchanged_after_split(root: Path, report: dict) -> bool:
    baseline = report.get('post_cleanup_canonical_baseline') or {}
    canonical_path_raw = str(baseline.get('canonical_path') or '').strip()
    expected_sha = str(baseline.get('canonical_sha256') or '').strip()
    verification = {
        'attempted': False,
        'canonical_path': canonical_path_raw or None,
        'expected_sha256': expected_sha or None,
    }
    report['canonical_ledger_post_split_verification'] = verification
    if not canonical_path_raw or not expected_sha:
        verification['status'] = 'skipped_missing_post_cleanup_baseline'
        return True
    canonical_path = Path(canonical_path_raw)
    if not canonical_path.is_absolute():
        canonical_path = root / canonical_path
    actual_sha = file_sha256(canonical_path)
    verification.update({'attempted': True, 'canonical_path': str(canonical_path), 'actual_sha256': actual_sha})
    if actual_sha != expected_sha:
        verification['status'] = 'failed_post_split_canonical_drift'
        return False
    verification['status'] = 'ok'
    return True


def publish_native_split_overlay_baseline(report: dict, overlay_report: dict) -> None:
    ledger_path_raw = str(overlay_report.get('ledger') or '').strip()
    ledger_sha = str(overlay_report.get('ledger_sha256') or '').strip()
    if overlay_report.get('status') != 'ok' or not ledger_path_raw or not ledger_sha:
        return
    baseline = report.get('post_cleanup_canonical_baseline')
    if not isinstance(baseline, dict):
        return
    baseline_path_raw = str(baseline.get('canonical_path') or '').strip()
    if not baseline_path_raw:
        return
    ledger_path = Path(ledger_path_raw)
    baseline_path = Path(baseline_path_raw)
    if not ledger_path.is_absolute():
        ledger_path = ROOT / ledger_path
    if not baseline_path.is_absolute():
        baseline_path = ROOT / baseline_path
    if ledger_path != baseline_path:
        return
    baseline['pre_native_split_overlay_sha256'] = baseline.get('canonical_sha256')
    baseline['canonical_sha256'] = ledger_sha
    baseline['status'] = 'ok_native_split_overlay'
    baseline['native_split_overlay_report'] = str(REPORT_DIR / 'baselane_native_split_ledger_overlay_report.json')


def write_run_report(report: dict) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2), encoding='utf-8')


def finalize_running_report(reason: str, failure_class: str = 'baselane_sync_interrupted') -> None:
    report = read_json_file(REPORT)
    if report.get('status') != 'running':
        report = {'started_at': time.time()}
    report['status'] = 'failed'
    report['reason'] = reason
    report['export_failure_class'] = failure_class
    report['interrupted'] = True
    report['finished_at'] = time.time()
    write_run_report(report)


def handle_interrupt_signal(signum, _frame=None) -> None:
    finalize_running_report(f'interrupted_by_signal_{signum}', 'baselane_sync_interrupted_by_signal')
    raise SystemExit(128 + int(signum))


def install_interrupt_signal_handlers() -> None:
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, handle_interrupt_signal)


def timeout_seconds(env_name: str, default: int) -> int:
    raw = str(os.environ.get(env_name, '')).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def cdp_target_churn_error(text: str) -> bool:
    lowered = (text or '').lower()
    return (
        'inspected target navigated or closed' in lowered
        or 'target closed' in lowered
        or 'session closed' in lowered
    )


def timeout_output(value: object) -> str:
    if value is None:
        return ''
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return str(value)


def run_first_day_pm_fee_cleanup(report: dict) -> int:
    if not FIRST_DAY_PM_FEE_CLEANUP_SCRIPT.is_file():
        report['first_day_pm_fee_cleanup'] = {
            'status': 'skipped_missing_script',
            'script': str(FIRST_DAY_PM_FEE_CLEANUP_SCRIPT),
        }
        return 0
    apply_enabled = os.environ.get('BASELANE_FIRST_DAY_PM_FEE_SOURCE_CLEANUP_APPLY', '0') == '1'
    cleanup_env = {
        **os.environ.copy(),
        'BASELANE_FIRST_DAY_PM_FEE_SOURCE_CLEANUP_APPLY': '1' if apply_enabled else '0',
        'WORKSPACE_ROOT': str(ROOT),
        'OPENCLAW_ROOT': str(ROOT.parent),
    }
    ledger_path = os.environ.get('BASELANE_LEDGER_PATH')
    if not ledger_path and os.environ.get('BASELANE_LEDGER_DIR'):
        ledger_path = str(Path(os.environ['BASELANE_LEDGER_DIR']) / 'ECO Systems General Ledger.csv')
    command = [
        'python3',
        str(FIRST_DAY_PM_FEE_CLEANUP_SCRIPT),
        '--all-months',
    ]
    if apply_enabled:
        command.append('--apply')
    if ledger_path:
        command.extend(['--gl-csv', ledger_path])
    cleanup = subprocess.run(
        command,
        text=True,
        capture_output=True,
        env=cleanup_env,
        timeout=300,
    )
    cleanup_report = read_json_file(ROOT / 'reports' / 'baselane_first_day_pm_fee_source_cleanup_plan.json')
    report['first_day_pm_fee_cleanup'] = {
        'status': cleanup_report.get('status') or ('ok' if cleanup.returncode == 0 else 'failed'),
        'return_code': cleanup.returncode,
        'mutation_mode': 'apply' if apply_enabled else 'dry_run',
        'apply_enabled': apply_enabled,
        'action_count': cleanup_report.get('action_count'),
        'pre_cleanup_action_count': cleanup_report.get('pre_cleanup_action_count'),
        'deleted_row_count': (cleanup_report.get('cleanup_apply') or {}).get('deleted_row_count'),
        'mutated': (cleanup_report.get('cleanup_apply') or {}).get('mutated'),
        'backup_file': (cleanup_report.get('cleanup_apply') or {}).get('backup_file'),
        'stdout_tail': redact_output(cleanup.stdout or '', 2000),
        'stderr_tail': redact_output(cleanup.stderr or '', 2000),
    }
    return cleanup.returncode


def run_native_split_plan_and_apply(report: dict) -> int:
    if not NATIVE_SPLIT_PLAN_SCRIPT.is_file():
        report['native_split_plan'] = {
            'status': 'skipped_missing_script',
            'script': str(NATIVE_SPLIT_PLAN_SCRIPT),
        }
        return 0
    source_index = Path(
        os.environ.get('BASELANE_SOURCE_TRANSACTION_INDEX')
        or REPORT_DIR / 'baselane_source_transaction_index.csv'
    )
    pending_audit_report = REPORT_DIR / 'baselane_recent_dao_eco_transaction_audit.current.json'
    if PENDING_TRANSACTION_AUDIT_SCRIPT.is_file():
        audit_command = [
            'python3',
            str(PENDING_TRANSACTION_AUDIT_SCRIPT),
            '--report',
            str(pending_audit_report),
        ]
        run_month = str(os.environ.get('RUN_MONTH') or '').strip()
        if len(run_month) == 7:
            audit_command.extend(['--start-date', f'{run_month}-01'])
        audit = subprocess.run(
            audit_command,
            text=True,
            capture_output=True,
            env={**os.environ.copy(), PIPELINE_LOCK_HELD_ENV: '1'},
            timeout=300,
        )
        pending_audit = read_json_file(pending_audit_report)
        report['pending_transaction_audit'] = {
            'status': pending_audit.get('status') or ('ok' if audit.returncode == 0 else 'failed'),
            'return_code': audit.returncode,
            'pending_count': pending_audit.get('pending_count'),
            'report': str(pending_audit_report),
            'stdout_tail': redact_output(audit.stdout or '', 2000),
            'stderr_tail': redact_output(audit.stderr or '', 2000),
        }
        if audit.returncode != 0:
            return audit.returncode
    plan_path = REPORT_DIR / 'baselane_native_split_plan.json'
    plan_command = [
        'python3',
        str(NATIVE_SPLIT_PLAN_SCRIPT),
        '--source-index',
        str(source_index),
        '--report',
        str(plan_path),
        '--csv',
        str(REPORT_DIR / 'baselane_native_split_plan.csv'),
        '--markdown',
        str(REPORT_DIR / 'baselane_native_split_plan.md'),
    ]
    if pending_audit_report.is_file():
        plan_command.extend([
            '--pending-transactions-report',
            str(pending_audit_report),
        ])
    native_split_env = {
        **os.environ.copy(),
        'WORKSPACE_ROOT': str(ROOT),
        'OPENCLAW_ROOT': str(ROOT.parent),
        PIPELINE_LOCK_HELD_ENV: '1',
    }
    if report.get('cdp_version_url'):
        native_split_env['BASELANE_CDP_VERSION_URL'] = str(report['cdp_version_url'])
    plan = subprocess.run(
        plan_command,
        text=True,
        capture_output=True,
        env=native_split_env,
        timeout=300,
    )
    plan_report = read_json_file(plan_path)
    report['native_split_plan'] = {
        'status': plan_report.get('status') or ('ok' if plan.returncode == 0 else 'review'),
        'return_code': plan.returncode,
        'ready_native_split_count': plan_report.get('ready_native_split_count'),
        'blocked_count': plan_report.get('blocked_count'),
        'rule_counts': plan_report.get('rule_counts'),
        'mutation_mode': plan_report.get('mutation_mode'),
        'report': str(plan_path),
        'stdout_tail': redact_output(plan.stdout or '', 2000),
        'stderr_tail': redact_output(plan.stderr or '', 2000),
    }
    if plan.returncode not in (0, 2):
        return plan.returncode
    if not NATIVE_SPLIT_APPLY_SCRIPT.is_file():
        report['native_split_apply'] = {
            'status': 'skipped_missing_script',
            'script': str(NATIVE_SPLIT_APPLY_SCRIPT),
        }
        return 0
    apply_enabled = os.environ.get('BASELANE_NATIVE_SPLIT_APPLY', '0') == '1'
    apply_command = [
        'python3',
        str(NATIVE_SPLIT_APPLY_SCRIPT),
        '--plan',
        str(plan_path),
        '--report',
        str(REPORT_DIR / 'baselane_native_split_apply_report.json'),
    ]
    if apply_enabled:
        apply_command.append('--apply')
    applied = subprocess.run(
        apply_command,
        text=True,
        capture_output=True,
        env=native_split_env,
        timeout=900,
    )
    apply_report = read_json_file(REPORT_DIR / 'baselane_native_split_apply_report.json')
    report['native_split_apply'] = {
        'status': apply_report.get('status') or ('ok' if applied.returncode == 0 else 'review'),
        'return_code': applied.returncode,
        'mutation_mode': apply_report.get('mutation_mode'),
        'apply_enabled': apply_report.get('apply_enabled'),
        'ready_count': apply_report.get('ready_count'),
        'applied_count': apply_report.get('applied_count'),
        'blocked_count': apply_report.get('blocked_count'),
        'already_applied_count': apply_report.get('already_applied_count'),
        'dry_run_count': apply_report.get('dry_run_count'),
        'report': str(REPORT_DIR / 'baselane_native_split_apply_report.json'),
        'stdout_tail': redact_output(applied.stdout or '', 2000),
        'stderr_tail': redact_output(applied.stderr or '', 2000),
    }
    if applied.returncode not in (0, 2):
        return applied.returncode
    reconcile_command = [
        *plan_command,
        '--apply-report',
        str(REPORT_DIR / 'baselane_native_split_apply_report.json'),
    ]
    reconciled = subprocess.run(
        reconcile_command,
        text=True,
        capture_output=True,
        env=native_split_env,
        timeout=300,
    )
    reconciled_plan_report = read_json_file(plan_path)
    report['native_split_plan'].update(
        {
            'status': reconciled_plan_report.get('status') or report['native_split_plan'].get('status'),
            'reconcile_return_code': reconciled.returncode,
            'ready_native_split_count': reconciled_plan_report.get('ready_native_split_count'),
            'handled_native_split_count': reconciled_plan_report.get('handled_native_split_count'),
            'already_applied_count': reconciled_plan_report.get('already_applied_count'),
            'applied_count': reconciled_plan_report.get('applied_count'),
            'blocked_count': reconciled_plan_report.get('blocked_count'),
            'mutation_mode': reconciled_plan_report.get('mutation_mode'),
            'reconcile_stdout_tail': redact_output(reconciled.stdout or '', 2000),
            'reconcile_stderr_tail': redact_output(reconciled.stderr or '', 2000),
        }
    )
    if reconciled.returncode not in (0, 2):
        return reconciled.returncode
    if NATIVE_SPLIT_LEDGER_OVERLAY_SCRIPT.is_file():
        overlay_command = [
            'python3',
            str(NATIVE_SPLIT_LEDGER_OVERLAY_SCRIPT),
            '--plan',
            str(plan_path),
            '--report',
            str(REPORT_DIR / 'baselane_native_split_ledger_overlay_report.json'),
        ]
        if apply_enabled:
            overlay_command.append('--apply')
        overlay = subprocess.run(
            overlay_command,
            text=True,
            capture_output=True,
            env=native_split_env,
            timeout=300,
        )
        overlay_report = read_json_file(REPORT_DIR / 'baselane_native_split_ledger_overlay_report.json')
        report['native_split_ledger_overlay'] = {
            'status': overlay_report.get('status') or ('ok' if overlay.returncode == 0 else 'review'),
            'return_code': overlay.returncode,
            'mutation_mode': overlay_report.get('mutation_mode'),
            'record_count': overlay_report.get('record_count'),
            'applied_count': overlay_report.get('applied_count'),
            'already_overlayed_count': overlay_report.get('already_overlayed_count'),
            'blocked_count': overlay_report.get('blocked_count'),
            'output_written': overlay_report.get('output_written'),
            'ledger': overlay_report.get('ledger'),
            'ledger_sha256': overlay_report.get('ledger_sha256'),
            'report': str(REPORT_DIR / 'baselane_native_split_ledger_overlay_report.json'),
            'stdout_tail': redact_output(overlay.stdout or '', 2000),
            'stderr_tail': redact_output(overlay.stderr or '', 2000),
        }
        if overlay.returncode not in (0, 2) or overlay_report.get('status') != 'ok':
            return overlay.returncode or 2
        publish_native_split_overlay_baseline(report, overlay_report)
    else:
        report['native_split_ledger_overlay'] = {
            'status': 'skipped_missing_script',
            'script': str(NATIVE_SPLIT_LEDGER_OVERLAY_SCRIPT),
        }
    if apply_enabled and int(apply_report.get('applied_count') or 0) > 0:
        report['native_split_reexport_required'] = True
        if (report.get('native_split_ledger_overlay') or {}).get('status') != 'ok':
            return 2
    return 0


def run():
    report = {'started_at': time.time(), 'status': 'running', 'steps': []}
    configure_pipeline_storage_paths(report)
    write_run_report(report)

    if not ensure_cdp_running():
        report['status'] = 'failed'
        report['reason'] = 'cdp_not_running'
        report['finished_at'] = time.time()
        write_run_report(report)
        print('FAILED: CDP not running')
        return 4

    resolved_cdp = resolve_brave_cdp_version(timeout=5)
    report['cdp_version_url'] = resolved_cdp.get('version_url')
    report['cdp_browser'] = resolved_cdp.get('browser')
    write_run_report(report)

    skip_login_wait = os.environ.get('BASELANE_SKIP_LOGIN_WAIT', '1') == '1'
    report['skip_login_wait'] = skip_login_wait
    if not skip_login_wait:
        report['steps'].append('manual_session_handoff_required')
        report['login_warning'] = 'automatic_login_disabled; use an already authenticated visible browser session'
        write_run_report(report)

    auth_report = ROOT / 'reports' / 'baselane_cdp_auth_recovery_report.json'
    preflight = subprocess.run(
        auth_preflight_command(resolved_cdp['version_url'], auth_report),
        text=True, capture_output=True, timeout=60,
    )
    report['steps'].append('human_session_preflight')
    report['auth_preflight_exit'] = preflight.returncode
    if preflight.returncode != 0:
        report['status'] = 'review'
        report['reason'] = 'manual_session_required'
        report['finished_at'] = time.time()
        write_run_report(report)
        print('REVIEW: sign in through an authorized visible Baselane browser session, then rerun')
        return 2

    export_timeout = timeout_seconds('BASELANE_EXPORT_TIMEOUT_SECONDS', DEFAULT_EXPORT_TIMEOUT_SECONDS)
    report['steps'].append('raw_cdp_export_existing_session')
    report['current_step'] = 'raw_cdp_export_existing_session'
    report['export_timeout_seconds'] = export_timeout
    write_run_report(report)
    export_command = ['node', str(EXPORT_SCRIPT)]
    export_env = {
        **os.environ.copy(),
        'BASELANE_CDP_VERSION_URL': resolved_cdp['version_url'],
        'WORKSPACE_ROOT': str(ROOT),
        'OPENCLAW_ROOT': str(ROOT.parent),
    }
    try:
        exported = subprocess.run(
            export_command,
            text=True,
            capture_output=True,
            env=export_env,
            timeout=export_timeout,
        )
        first_export_text = f"{exported.stdout or ''}\n{exported.stderr or ''}"
        if exported.returncode != 0 and cdp_target_churn_error(first_export_text):
            report['export_retry_reason'] = 'cdp_target_navigated_or_closed'
            report['export_first_exit'] = exported.returncode
            report['export_first_stdout_tail'] = redact_output(exported.stdout or '', 2000)
            report['export_first_stderr_tail'] = redact_output(exported.stderr or '', 2000)
            write_run_report(report)
            time.sleep(3)
            exported = subprocess.run(
                export_command,
                text=True,
                capture_output=True,
                env=export_env,
                timeout=export_timeout,
            )
            report['export_retry_attempted'] = True
            report['export_retry_exit'] = exported.returncode
    except subprocess.TimeoutExpired as exc:
        report['status'] = 'failed'
        report['reason'] = 'raw_cdp_export_timeout'
        report['export_failure_class'] = 'baselane_raw_export_timeout'
        report['export_exit'] = None
        report['export_timed_out'] = True
        report['export_timeout_seconds'] = export_timeout
        report['export_stdout_tail'] = redact_output(timeout_output(exc.stdout), 4000)
        report['export_stderr_tail'] = redact_output(timeout_output(exc.stderr), 4000)
        report['finished_at'] = time.time()
        report.pop('current_step', None)
        write_run_report(report)
        print('FAILED: raw CDP export timed out')
        return 4
    report.pop('current_step', None)
    report['export_exit'] = exported.returncode
    report['export_stdout_tail'] = redact_output(exported.stdout or '', 4000)
    report['export_stderr_tail'] = redact_output(exported.stderr or '', 4000)
    export_text = f"{exported.stdout or ''}\n{exported.stderr or ''}"

    if exported.returncode != 0:
        report['status'] = 'failed'
        report['reason'] = 'raw_cdp_export_failed'
        if 'APP_CHECK_REQUIRED' in export_text or 'App Check token is required' in export_text:
            report['export_failure_class'] = 'baselane_app_check_required'
        elif 'Temporarily down for maintenance' in export_text:
            report['export_failure_class'] = 'baselane_maintenance'
        elif 'timeout waiting for initial dom ready' in export_text:
            report['export_failure_class'] = 'baselane_cdp_dom_not_ready'
        elif 'timeout waiting for x-firebase-appcheck' in export_text:
            report['export_failure_class'] = 'baselane_app_check_not_captured'
        elif 'CDP Runtime.evaluate timed out' in export_text:
            report['export_failure_class'] = 'baselane_cdp_runtime_evaluate_timeout'
        elif cdp_target_churn_error(export_text):
            report['export_failure_class'] = 'baselane_cdp_target_navigated_or_closed'
        elif 'missing input for input[name="email"]' in export_text:
            report['export_failure_class'] = 'baselane_login_form_unavailable'
        elif 'accounts:signInWithPassword' in export_text and 'status=401' in export_text:
            report['export_failure_class'] = 'baselane_login_auth_401'
        elif 'loginAuthFailure' in export_text and 'accounts:signInWithPassword' in export_text and '"status":401' in export_text:
            report['export_failure_class'] = 'baselane_login_auth_401'
        elif (
            ('Apollo GraphQL' in export_text or 'GraphQL Transactions direct fetch failed' in export_text)
            and 'Failed to fetch' in export_text
        ):
            report['export_failure_class'] = 'baselane_apollo_graphql_fetch_failed'
        elif 'Apollo GraphQL' in export_text and ('status code 401' in export_text or 'unauthorized' in export_text.lower()):
            report['export_failure_class'] = 'baselane_auth_401'
        elif 'apollo_401_reauth' in export_text and ('login inputs' in export_text or '/login' in export_text):
            report['export_failure_class'] = 'baselane_reauth_challenge_or_login_blocked'
        elif 'Guard failed:' in export_text:
            guard = read_json_file(ROOT / 'reports' / 'baselane_export_guard_last.json')
            cleanup_rc = run_first_day_pm_fee_cleanup(report)
            report['status'] = 'review'
            report['reason'] = 'export_guard_review'
            report['export_failure_class'] = 'baselane_export_guard_review'
            report['export_guard_ok'] = guard.get('ok')
            report['export_guard_violations'] = guard.get('violations') or []
            report['export_guard_report'] = str(ROOT / 'reports' / 'baselane_export_guard_last.json')
            report['source_transaction_index'] = guard.get('source_transaction_index')
            report['filtered_preview_snapshot'] = guard.get('filtered_preview_snapshot')
            report['canonical_path'] = guard.get('canonical_path')
            report['canonical_overwrite_blocked'] = True
            report['local_first_day_pm_fee_cleanup_rc'] = cleanup_rc
            native_split_rc = run_native_split_plan_and_apply(report)
            report['native_split_plan_apply_rc'] = native_split_rc
        else:
            report['export_failure_class'] = 'baselane_raw_export_failed'
        report['finished_at'] = time.time()
        write_run_report(report)
        if report['status'] == 'review':
            print('REVIEW: raw CDP export guard blocked canonical overwrite')
            return 2
        print('FAILED: raw CDP export failed')
        return 4

    if not reconcile_canonical_ledger_from_login_report(ROOT, report):
        report['status'] = 'review'
        report['reason'] = 'canonical_ledger_reconcile_failed'
        report['export_failure_class'] = 'baselane_canonical_ledger_reconcile_failed'
        report['finished_at'] = time.time()
        write_run_report(report)
        print('REVIEW: canonical Baselane ledger did not match filtered export snapshot')
        return 2
    write_run_report(report)

    cleanup_rc = run_first_day_pm_fee_cleanup(report)
    report['local_first_day_pm_fee_cleanup_rc'] = cleanup_rc
    if cleanup_rc != 0:
        report['status'] = 'review'
        report['reason'] = 'first_day_pm_fee_cleanup_review'
        report['finished_at'] = time.time()
        write_run_report(report)
        print('REVIEW: first-day PM fee local cleanup failed before split')
        return 2
    normalization_rc = run_local_retained_capital_normalization(ROOT, report)
    report['local_retained_capital_normalization_rc'] = normalization_rc
    if normalization_rc != 0:
        report['status'] = 'review'
        report['reason'] = 'local_retained_capital_normalization_review'
        report['finished_at'] = time.time()
        write_run_report(report)
        print('REVIEW: scoped local retained-capital normalization failed before split')
        return 2
    if not publish_post_cleanup_canonical_baseline(ROOT, report):
        report['status'] = 'review'
        report['reason'] = 'post_cleanup_canonical_baseline_failed'
        report['export_failure_class'] = 'baselane_post_cleanup_canonical_baseline_failed'
        report['finished_at'] = time.time()
        write_run_report(report)
        print('REVIEW: post-cleanup canonical ledger baseline could not be published')
        return 2

    native_split_rc = run_native_split_plan_and_apply(report)
    report['native_split_plan_apply_rc'] = native_split_rc
    if native_split_rc != 0:
        report['status'] = 'review'
        report['reason'] = 'native_split_apply_review'
        report['finished_at'] = time.time()
        write_run_report(report)
        print('REVIEW: Baselane native split workflow needs review before downstream split')
        return 2

    report['steps'].append('split_public_financials')
    report['current_step'] = 'split_public_financials'
    write_run_report(report)
    rs = subprocess.run(
        ['python3', str(SPLIT_SCRIPT)],
        text=True,
        capture_output=True,
        env=os.environ.copy(),
        timeout=1800,
    )
    report.pop('current_step', None)
    report['split_exit'] = rs.returncode
    report['split_stdout_tail'] = redact_output(rs.stdout or '', 2000)
    report['split_stderr_tail'] = redact_output(rs.stderr or '', 2000)
    report['canonical_ledger_reconcile_before_split'] = report.get('canonical_ledger_reconcile')
    if not verify_canonical_ledger_unchanged_after_split(ROOT, report):
        report['status'] = 'review'
        report['reason'] = 'canonical_ledger_drifted_after_split'
        report['export_failure_class'] = 'baselane_canonical_ledger_drifted_after_split'
        report['finished_at'] = time.time()
        write_run_report(report)
        print('REVIEW: canonical Baselane ledger drifted after split')
        return 2
    report['canonical_ledger_reconcile_after_split'] = {
        'attempted': False,
        'status': 'skipped_post_cleanup_baseline_preserved',
        'reason': 'post-cleanup canonical ledger may intentionally differ from raw filtered Baselane export',
    }
    report['status'] = 'ok' if rs.returncode == 0 else 'failed'
    report['finished_at'] = time.time()
    write_run_report(report)

    if rs.returncode == 0:
        print('OK: baselane sync via raw CDP login/export complete')
        return 0
    print('FAILED: split script failed')
    return 5


def _paths_for_root(root: Path):
    return {
        'root': root,
        'report': root / 'reports' / 'baselane_sync_cdp_report.json',
        'export_script': root / 'scripts' / 'baselane_export_human_paced.js',
        'auth_preflight': root / 'scripts' / 'baselane_cdp_auth_recovery.py',
        'split_script': root / 'scripts' / 'split_ledger_public_financials.py',
    }


def remediation_fields(classification: str):
    has_issues = classification != 'ok'
    return {
        'remediation_class': 'operator-reviewed-baselane-sync-cdp-deterministic' if has_issues else 'no-remediation-needed',
        'requires_operator_approval': has_issues,
        'requires_interactive_sudo': False,
        'requires_interactive_oauth': False,
        'safe_to_run_automatically': not has_issues,
        'review_command': diagnostic_command(),
        'review_command_safe_to_run_automatically': True,
        'cleanup_command_after_review': None,
        'restart_command_after_review': None,
        'oauth_command_after_review': None,
        'helper_command_after_review': None,
    }


def review_command_validation(command: object | None = None):
    command = diagnostic_command() if command is None else command
    expected_script = str(SCRIPT_PATH)
    issues = []
    parts = []
    if not isinstance(command, str) or not command.strip():
        issues.append('review command is empty or not a string')
    else:
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            issues.append(f'review command is not shell-parseable: {exc}')
    script_exists = SCRIPT_PATH.exists()
    script_is_file = SCRIPT_PATH.is_file()
    python3_present = bool(parts) and parts[0] == 'python3'
    script_path_present = expected_script in parts
    json_flag_present = '--json' in parts
    if not python3_present:
        issues.append('review command must start with python3')
    if not script_path_present:
        issues.append(f'review command must include script path: {expected_script}')
    if not json_flag_present:
        issues.append('review command must include --json')
    if not script_exists:
        issues.append(f'review command script path does not exist: {expected_script}')
    elif not script_is_file:
        issues.append(f'review command script path is not a file: {expected_script}')
    return {
        'command': command,
        'expected_script_path': expected_script,
        'script_exists': script_exists,
        'script_is_file': script_is_file,
        'path': expected_script,
        'path_exists': script_exists,
        'python3_present': python3_present,
        'script_path_present': script_path_present,
        'json_flag_present': json_flag_present,
        'requires_executable': False,
        'valid': not issues,
        'issues': issues,
        'issue': issues[0] if issues else None,
    }


def _path_state(path: Path):
    return {
        'path': str(path),
        'exists': path.exists(),
        'readable': path.is_file() and os.access(path, os.R_OK),
        'size_bytes': path.stat().st_size if path.exists() and path.is_file() else None,
    }


def classified_issue_records(issues, evidence, classification):
    fields = remediation_fields(classification)
    validation = review_command_validation(fields.get('review_command'))
    return [
        {
            'issue': issue,
            'issue_class': ISSUE_CLASS,
            'classification': classification,
            'area': 'baselane-cdp-sync',
            'node_available': evidence.get('node_available'),
            'python3_available': evidence.get('python3_available'),
            'export_script_readable': evidence.get('export_script', {}).get('readable'),
            'login_wait_readable': evidence.get('login_wait', {}).get('readable'),
            'split_script_readable': evidence.get('split_script', {}).get('readable'),
            'report_parent_exists': evidence.get('report_parent_exists'),
            'cdp_port_candidates': evidence.get('cdp_port_candidates'),
            'cdp_probe_attempted': evidence.get('cdp_probe_attempted'),
            'browser_launch_attempted': evidence.get('browser_launch_attempted'),
            'report_write_attempted': evidence.get('report_write_attempted'),
            'review_command_valid': validation['valid'],
            'review_command_validation': validation,
            **fields,
        }
        for issue in issues
    ]


def classified_issue_summary(report):
    classified = report.get('classified_issues') or []
    class_counts = {}
    route_counts = {}
    validation_issues = []
    for issue in classified:
        issue_class = issue.get('issue_class')
        route = issue.get('classification', report.get('classification'))
        if issue_class:
            class_counts[issue_class] = class_counts.get(issue_class, 0) + 1
        if route:
            route_counts[route] = route_counts.get(route, 0) + 1
        validation_issues.extend(issue.get('review_command_validation', {}).get('issues') or [])
    safe_review_command_count = sum(1 for issue in classified if issue.get('review_command_safe_to_run_automatically'))
    valid_review_command_count = sum(1 for issue in classified if issue.get('review_command_valid') is True)
    return {
        'total': len(classified),
        'total_count': len(classified),
        'ok_count': int(report.get('ok_count') or 0),
        'issue_count': int(report.get('issue_count') or 0),
        'visible_ok_count': len(report.get('visible_ok') or []),
        'class_counts': class_counts,
        'issue_class_counts': class_counts,
        'route_classification': report.get('classification'),
        'route_classification_counts': route_counts,
        'approval_required_count': sum(1 for issue in classified if issue.get('requires_operator_approval')),
        'review_required_count': int(report.get('review_required_count') or 0),
        'interactive_sudo_count': sum(1 for issue in classified if issue.get('requires_interactive_sudo')),
        'interactive_oauth_count': sum(1 for issue in classified if issue.get('requires_interactive_oauth')),
        'safe_review_command_count': safe_review_command_count,
        'valid_review_command_count': valid_review_command_count,
        'invalid_review_command_count': safe_review_command_count - valid_review_command_count,
        'review_command_validation_issues': sorted(set(validation_issues)),
        'safe_to_run_automatically': report.get('safe_to_run_automatically') is True,
        'node_available': report.get('node_available') is True,
        'python3_available': report.get('python3_available') is True,
        'export_script_readable': report.get('export_script', {}).get('readable') is True,
        'login_wait_readable': report.get('login_wait', {}).get('readable') is True,
        'split_script_readable': report.get('split_script', {}).get('readable') is True,
        'report_parent_exists': report.get('report_parent_exists') is True,
        'report_parent_writable': report.get('report_parent_writable') is True,
        'secrets_file_exists': report.get('secrets_file_exists') is True,
        'secrets_read_attempted': report.get('secrets_read_attempted') is True,
        'cdp_probe_attempted': report.get('cdp_probe_attempted') is True,
        'browser_launch_attempted': report.get('browser_launch_attempted') is True,
        'login_wait_subprocess_attempted': report.get('login_wait_subprocess_attempted') is True,
        'export_subprocess_attempted': report.get('export_subprocess_attempted') is True,
        'split_subprocess_attempted': report.get('split_subprocess_attempted') is True,
        'report_write_attempted': report.get('report_write_attempted') is True,
        'remediation_class': report.get('remediation_class'),
        'cleanup_command_available_after_review': bool(report.get('cleanup_command_after_review')),
        'restart_command_available_after_review': bool(report.get('restart_command_after_review')),
        'oauth_command_available_after_review': bool(report.get('oauth_command_after_review')),
        'helper_command_available_after_review': bool(report.get('helper_command_after_review')),
    }


def build_report(root: Path = ROOT, env=None):
    env = os.environ if env is None else env
    root = Path(root)
    paths = _paths_for_root(root)
    issues = []
    visible_ok = []
    ports, port_issues = brave_port_candidates(env)
    issues.extend(port_issues)

    node_path = shutil.which('node')
    python_path = shutil.which('python3')
    report_parent = paths['report'].parent
    evidence = {
        'root': str(root),
        'credentials_inspected': False,
        'report_path': str(paths['report']),
        'report_parent_exists': report_parent.exists(),
        'report_parent_writable': report_parent.exists() and os.access(report_parent, os.W_OK),
        'export_script': _path_state(paths['export_script']),
        'auth_preflight': _path_state(paths['auth_preflight']),
        'split_script': _path_state(paths['split_script']),
        'node_available': node_path is not None,
        'python3_available': python_path is not None,
        'browser_binary_available': any(
            shutil.which(name)
            for name in ('brave-browser', 'chromium-browser', 'chromium', 'google-chrome')
        ),
        'skip_login_wait': str(env.get('BASELANE_SKIP_LOGIN_WAIT', '1')) == '1',
        'force_login_default': str(env.get('BASELANE_FORCE_LOGIN', '0')),
        'base_cdp_url_present': bool(str(env.get('BASELANE_CDP_URL', '')).strip()),
        'base_cdp_version_url_present': bool(str(env.get('BASELANE_CDP_VERSION_URL', '')).strip()),
        'base_cdp_host_present': bool(str(env.get('BASELANE_CDP_HOST', '')).strip()),
        'cdp_port_candidates': ports,
        'cdp_probe_attempted': False,
        'cdp_diagnostics_attempted': False,
        'browser_launch_attempted': False,
        'browser_user_data_dir_created': False,
        'auth_preflight_subprocess_attempted': False,
        'export_subprocess_attempted': False,
        'split_subprocess_attempted': False,
        'report_write_attempted': False,
    }

    if not evidence['node_available']:
        issues.append('Node.js binary is not available for Baselane CDP export scripts')
    if not evidence['python3_available']:
        issues.append('python3 binary is not available for the Baselane split step')
    if not evidence['report_parent_exists']:
        issues.append(f'Baselane CDP sync report parent is missing: {report_parent}')
    elif not evidence['report_parent_writable']:
        issues.append(f'Baselane CDP sync report parent is not writable: {report_parent}')
    for label in ('export_script', 'auth_preflight', 'split_script'):
        state = evidence[label]
        if not state['exists']:
            issues.append(f'Required Baselane sync dependency is missing: {state["path"]}')
        elif not state['readable']:
            issues.append(f'Required Baselane sync dependency is not readable: {state["path"]}')

    if not issues:
        visible_ok.append(
            'OK Baselane deterministic CDP sync config: '
            f'node={evidence["node_available"]} export={evidence["export_script"]["readable"]} '
            f'split={evidence["split_script"]["readable"]}'
        )
        visible_ok.append(
            'OK Baselane deterministic CDP sync diagnostic: '
            'no secrets read, CDP probe, browser launch, subprocess, report write, restart, sudo, OAuth, or helper command'
        )

    classification = 'baselane-sync-cdp-deterministic-review' if issues else 'ok'
    classified_issues = classified_issue_records(issues, evidence, classification)
    fields = remediation_fields(classification)
    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'status': 'BASELANE_SYNC_CDP_DETERMINISTIC_REVIEW' if issues else 'NO_REPLY',
        'classification': classification,
        'ok': visible_ok,
        'ok_state': not issues,
        'visible_ok': visible_ok,
        'ok_count': len(visible_ok),
        'issues': issues,
        'issue_count': len(issues),
        'issue_classes': [ISSUE_CLASS] if issues else [],
        'classified_issues': classified_issues,
        'advisory_count': 0,
        'review_required_count': len(classified_issues),
        **evidence,
        'remediation': {'classification': fields['remediation_class'], **fields},
        **fields,
    }
    summary = classified_issue_summary(report)
    report['classified_issue_summary'] = summary
    report['safe_review_command_count'] = summary['safe_review_command_count']
    report['valid_review_command_count'] = summary['valid_review_command_count']
    report['invalid_review_command_count'] = summary['invalid_review_command_count']
    report['review_command_validation_issues'] = summary['review_command_validation_issues']
    return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Run or inspect the deterministic Baselane CDP sync')
    parser.add_argument('--json', action='store_true', help='Emit a read-only diagnostic report and do not run sync')
    parser.add_argument('--root', default=str(ROOT), help='Workspace root to inspect for --json')
    return parser.parse_args(argv)


def main(argv=None, stdout: TextIO | None = None):
    args = parse_args(argv)
    if args.json:
        report = build_report(root=Path(args.root))
        print(json.dumps(report, indent=2, sort_keys=True), file=stdout or sys.stdout)
        return 0 if report['status'] == 'NO_REPLY' else 1

    install_interrupt_signal_handlers()
    with exclusive_pipeline_lock() as acquired:
        if not acquired:
            print(
                f'DEFERRED: another Baselane source pipeline run holds {PIPELINE_LOCK_PATH}',
                file=stdout or sys.stdout,
            )
            return 2
        try:
            code = run()
        except KeyboardInterrupt:
            finalize_running_report('interrupted_by_operator', 'baselane_sync_interrupted_by_operator')
            print('FAILED: interrupted by operator', file=stdout or sys.stdout)
            return 130
        except Exception as e:
            report = read_json_file(REPORT)
            if report.get('status') != 'running':
                report = {}
            report.update({
                'status': 'failed',
                'reason': str(e),
                'finished_at': time.time(),
                'cdp_diagnostics': cdp_diagnostics(),
            })
            write_run_report(report)
            print(f'FAILED: {e}', file=stdout or sys.stdout)
            return 1
        return code


if __name__ == '__main__':
    raise SystemExit(main())
