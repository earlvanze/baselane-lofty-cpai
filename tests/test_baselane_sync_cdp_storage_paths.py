from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import baselane_sync_cdp_deterministic as sync


def test_storage_paths_use_one_existing_dropbox_lane(tmp_path):
    missing = tmp_path / "missing"
    mounted = tmp_path / "mounted-dropbox"
    assetrail = mounted / "Projects" / "assetrail"
    assetrail.mkdir(parents=True)

    paths = sync.resolve_pipeline_storage_paths(
        {},
        default_dropbox_candidates=(missing, mounted),
    )

    assert paths["dropbox_root"] == mounted
    assert paths["ledger_dir"] == assetrail
    assert paths["ledger_path"] == assetrail / "ECO Systems General Ledger.csv"


def test_auth_preflight_accepts_graphql_proof_for_visible_session(tmp_path):
    command = sync.auth_preflight_command(
        "http://127.0.0.1:19222/json/version",
        tmp_path / "auth-report.json",
    )

    assert "--graphql-auth-smoke" in command
    assert command[command.index("--cdp-url") + 1] == "http://127.0.0.1:19222/json/version"


def test_explicit_ledger_path_keeps_export_and_split_on_same_file(tmp_path):
    ledger = tmp_path / "custom" / "ledger.csv"

    paths = sync.resolve_pipeline_storage_paths(
        {"BASELANE_LEDGER_PATH": str(ledger)},
        default_dropbox_candidates=(tmp_path / "dropbox",),
    )

    assert paths["ledger_dir"] == ledger.parent
    assert paths["ledger_path"] == ledger


def test_current_run_export_is_not_replaced_by_stale_login_snapshot(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    ledger = tmp_path / "canonical.csv"
    stale_snapshot = tmp_path / "stale.csv"
    ledger.write_text("current\n", encoding="utf-8")
    stale_snapshot.write_text("stale\n", encoding="utf-8")
    started_at = dt.datetime.now(dt.UTC).timestamp()
    checked_at = dt.datetime.fromtimestamp(started_at + 1, dt.UTC).isoformat()
    (reports / "baselane_export_report.json").write_text(
        json.dumps(
            {
                "ok": True,
                "output": str(ledger),
                "checked_at": checked_at,
            }
        ),
        encoding="utf-8",
    )
    (reports / "baselane_login_export_report.json").write_text(
        json.dumps(
            {
                "ok": True,
                "canonical_path": str(ledger),
                "filtered_snapshot": str(stale_snapshot),
                "canonical_sha256": sync.file_sha256(stale_snapshot),
            }
        ),
        encoding="utf-8",
    )
    report = {
        "started_at": started_at,
        "pipeline_storage_paths": {"ledger_path": str(ledger)},
    }

    assert sync.reconcile_canonical_ledger_from_login_report(tmp_path, report)
    assert ledger.read_text(encoding="utf-8") == "current\n"
    assert report["canonical_ledger_reconcile"]["status"] == "ok_current_run_export_authoritative"
