from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import baselane_scheduler_audit as audit


CANONICAL = audit.OPENCLAW_CANONICAL_EXT4_REPO


def test_eod_telegram_schedule_is_not_required_by_default():
    assert all(spec.get("name") != "eod_telegram" for spec in audit.JOB_SPECS)


def daily_job(*, cwd: str = CANONICAL, script_root: str = CANONICAL) -> dict:
    return {
        "id": "baselane-daily-sync",
        "name": "Baselane Daily Sync",
        "enabled": True,
        "agentId": "cron-network",
        "sessionTarget": "isolated",
        "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "Europe/Paris"},
        "payload": {
            "kind": "command",
            "argv": [
                "bash",
                "-lc",
                f"flock -n /tmp/test.lock timeout 90m {script_root}/scripts/baselane_cron_run.sh",
            ],
            "cwd": cwd,
            "env": {
                "BASELANE_CRON_HUMAN_PACED_FALLBACK": "1",
                "WORKSPACE_ROOT": cwd,
            },
        },
        "delivery": {"mode": "none"},
    }


def test_resolve_openclaw_root_from_nested_ext4_repo(monkeypatch):
    monkeypatch.delenv("OPENCLAW_ROOT", raising=False)

    resolved = audit.resolve_openclaw_root(
        Path("/home/digit/.openclaw/workspace/repos/baselane-lofty-cpai")
    )

    assert resolved == Path("/home/digit/.openclaw")


def test_daily_command_job_accepts_structured_env_and_canonical_ext4_execution():
    assert audit.openclaw_baselane_backup_job_allowed(daily_job())


def test_daily_command_job_rejects_stale_workspace_copy():
    stale = "/home/digit/.openclaw/workspace"

    assert not audit.openclaw_baselane_backup_job_allowed(
        daily_job(cwd=stale, script_root=stale)
    )


def test_normalized_daily_job_preserves_owner_evidence_and_matches_schedule_guard(tmp_path):
    job = daily_job()
    text, issues, ignored = audit.normalize_openclaw_cron_jobs(
        json.dumps({"jobs": [job]}), tmp_path / "openclaw.sqlite"
    )

    assert issues == []
    assert ignored == []
    assert '"agentId": "cron-network"' in text
    assert '"sessionTarget": "isolated"' in text
    mentions = audit.scheduler_mentions(
        [{"name": "openclaw", "path": "test", "text": text}],
        ["baselane_cron_run.sh"],
    )
    assert audit.scheduler_line_guard_matches(
        mentions,
        audit.JOB_SPECS[0]["required_scheduler_line_fragment_sets"],
    )
