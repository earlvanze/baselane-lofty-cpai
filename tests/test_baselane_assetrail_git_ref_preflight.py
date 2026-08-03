from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import baselane_assetrail_git_ref_preflight as preflight


def initialize_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "ledger.csv").write_text("value\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "ledger.csv"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "initial"], check=True)


def test_apply_quarantines_recognized_dropbox_conflict_ref(tmp_path):
    repo = tmp_path / "assetrail"
    repo.mkdir()
    initialize_repo(repo)
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    bad_ref = repo / ".git/refs/heads/main (Cyber's conflicted copy 2026-07-31)"
    bad_ref.write_text(head + "\n", encoding="utf-8")
    report_path = tmp_path / "report.json"
    quarantine = tmp_path / "quarantine"

    report = preflight.run_preflight(
        repo,
        report_path=report_path,
        quarantine_root=quarantine,
        apply=True,
    )

    assert report["status"] == "quarantined"
    assert report["invalid_ref_count"] == 1
    assert report["quarantined_count"] == 1
    assert report["unresolved_count"] == 0
    assert not bad_ref.exists()
    destination = Path(report["records"][0]["quarantine_path"])
    assert destination.read_text(encoding="utf-8").strip() == head
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "quarantined"
    subprocess.run(["git", "-C", str(repo), "status", "--short"], check=True)


def test_unrecognized_invalid_ref_fails_closed(tmp_path):
    repo = tmp_path / "assetrail"
    repo.mkdir()
    initialize_repo(repo)
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    bad_ref = repo / ".git/refs/heads/not valid"
    bad_ref.write_text(head + "\n", encoding="utf-8")

    report = preflight.run_preflight(
        repo,
        report_path=tmp_path / "report.json",
        quarantine_root=tmp_path / "quarantine",
        apply=True,
    )

    assert report["status"] == "blocked"
    assert report["quarantined_count"] == 0
    assert report["unresolved_count"] == 1
    assert bad_ref.exists()


def test_dry_run_preserves_recognized_ref(tmp_path):
    repo = tmp_path / "assetrail"
    repo.mkdir()
    initialize_repo(repo)
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    bad_ref = repo / ".git/refs/heads/main (umbrel's conflicted copy 2026-07-16)"
    bad_ref.write_text(head + "\n", encoding="utf-8")

    report = preflight.run_preflight(
        repo,
        report_path=tmp_path / "report.json",
        quarantine_root=tmp_path / "quarantine",
        apply=False,
    )

    assert report["status"] == "review"
    assert report["records"][0]["action"] == "would_quarantine"
    assert bad_ref.exists()
