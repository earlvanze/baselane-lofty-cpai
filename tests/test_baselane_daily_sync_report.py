from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import baselane_daily_sync_report as daily


SHA = "a" * 64


def test_post_cleanup_baseline_requires_matching_hash_and_size():
    report = {
        "post_cleanup_baseline": True,
        "post_cleanup_canonical_sha256": SHA,
        "post_cleanup_canonical_size_bytes": 123,
    }

    assert daily.valid_post_cleanup_baseline(report, SHA, 123)
    assert not daily.valid_post_cleanup_baseline(report, "b" * 64, 123)
    assert not daily.valid_post_cleanup_baseline(report, SHA, 124)


def test_post_cleanup_baseline_fails_closed_without_evidence_fields():
    assert not daily.valid_post_cleanup_baseline({"post_cleanup_baseline": True}, SHA, 123)
    assert not daily.valid_post_cleanup_baseline(
        {
            "post_cleanup_baseline": True,
            "post_cleanup_canonical_sha256": "not-a-sha",
            "post_cleanup_canonical_size_bytes": 123,
        },
        SHA,
        123,
    )
