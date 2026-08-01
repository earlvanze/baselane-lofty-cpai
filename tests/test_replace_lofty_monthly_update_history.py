from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "replace_lofty_monthly_update_history.py"
SPEC = importlib.util.spec_from_file_location("replace_lofty_monthly_update_history", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_replace_dates_removes_only_scoped_entries() -> None:
    current = """# Property Updates

## 2026-07-31

- stale close

## 2026-07-30

- stale preview

## 2026-06-30

- preserved exactly
"""
    candidate = """## 2026-07-31

- corrected close"""

    rendered, removed = MODULE.replace_dates(
        current,
        candidate,
        entry_date="2026-07-31",
        remove_dates={"2026-07-30", "2026-07-31"},
    )

    assert removed == ["2026-07-31", "2026-07-30"]
    assert rendered.count("## 2026-07-31") == 1
    assert "## 2026-07-30" not in rendered
    assert "- corrected close" in rendered
    assert "## 2026-06-30\n\n- preserved exactly" in rendered


def test_normalized_candidate_rejects_multiple_sections() -> None:
    candidate = "## 2026-07-31\n\n- one\n\n## 2026-06-30\n\n- two\n"
    try:
        MODULE.normalized_candidate(candidate, "2026-07-31")
    except ValueError as exc:
        assert "exactly one" in str(exc)
    else:
        raise AssertionError("candidate with multiple sections should fail")
