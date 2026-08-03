from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from repair_monthly_discord_review_fragment_boundaries import (  # noqa: E402
    safe_fragment_split,
    semantic_tokens,
    transport_equivalent,
)


def test_safe_fragment_split_preserves_nested_obligation_lines() -> None:
    value = (
        "Actual July 31 position:\n\n"
        "- Baselane bank cash: $547.61\n"
        "- Recorded unpaid obligations: $43,431.06\n"
        "  - Taxes: $33,345.88\n"
        "  - Insurance: $6,494.73\n"
        "- Accessible DAO funds for operations: $3,417.57\n\n"
        "Accessible DAO funds is available for operations."
    )

    parts = safe_fragment_split(value, 3)

    assert "\n".join(parts) == value
    assert all(part == part.strip() for part in parts)
    assert all(not part[:1].isspace() for part in parts)
    assert not any(part.startswith("- Insurance") for part in parts)


def test_semantic_tokens_allow_only_transport_whitespace_changes() -> None:
    expected = "- Obligations:\n  - Insurance: $100.00\n\n- Cash: $50.00"
    transport_value = "- Obligations:\n- Insurance: $100.00\n- Cash: $50.00"
    changed_value = "- Obligations:\n- Insurance: $101.00\n- Cash: $50.00"

    assert semantic_tokens(expected) == semantic_tokens(transport_value)
    assert semantic_tokens(expected) != semantic_tokens(changed_value)


def test_transport_equivalent_handles_mixed_fragment_boundaries() -> None:
    expected = "Revenue: $100.00\n\n- Obligations:\n  - Insurance: $25.00"
    parts = ["Revenue: $100", ".00", "- Obligations:\n- Insurance: $25.00"]

    assert transport_equivalent(parts, expected)
    assert not transport_equivalent(
        ["Revenue: $100", ".00", "- Obligations:\n- Insurance: $26.00"],
        expected,
    )
