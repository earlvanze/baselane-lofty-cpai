from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lofty_live_updates_history_containment_report.py"
SPEC = importlib.util.spec_from_file_location("lofty_history_containment", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ExitingParser:
    @staticmethod
    def parse_entries(_text: str) -> list[dict[str, str]]:
        raise SystemExit("No dated update entries found in UPDATES.md")


class MarkdownNormalizer:
    @staticmethod
    def fix_mojibake(text: str) -> str:
        return text

    @staticmethod
    def flatten_markdown_tables(text: str) -> str:
        return text


def test_parser_system_exit_becomes_record_level_parse_error(tmp_path: Path) -> None:
    updates_md = tmp_path / "UPDATES.md"
    updates_md.write_text("# Property Updates\n", encoding="utf-8")

    result = MODULE.containment_for_updates(
        ExitingParser,
        MarkdownNormalizer,
        updates_md,
        "- **Property Update (07/31/2026):**\n    - Existing live update",
    )

    assert result["containment_ok"] is False
    assert result["parse_error"] == "SystemExit: No dated update entries found in UPDATES.md"
    assert result["missing_entry_count"] == 0
