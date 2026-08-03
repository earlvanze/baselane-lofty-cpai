from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from baselane_mcp.pipeline import (
    PipelineValidationError,
    inspect_pipeline_artifact,
    rebuild_monthly_review_artifacts,
    validate_run_month,
)


def test_artifact_inspection_is_allowlisted_and_supports_property_filter(tmp_path: Path):
    report = tmp_path / "reports" / "baselane_live_dao_cash_reconciliation.json"
    report.parent.mkdir()
    report.write_text(
        json.dumps(
            {
                "status": "ok",
                "properties": [
                    {"property": "88 Madison Ave", "total_dao_spendable_cash": "1.00"},
                    {"property": "90 Madison Ave", "total_dao_spendable_cash": "2.00"},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = inspect_pipeline_artifact(
        workspace_root=tmp_path,
        artifact="dao_cash_reconciliation",
        property_name="90 Madison",
    )

    assert result["status"] == "success"
    assert result["match_count"] == 1
    assert result["matches"][0]["record"]["property"] == "90 Madison Ave"

    with pytest.raises(PipelineValidationError, match="unsupported artifact"):
        inspect_pipeline_artifact(
            workspace_root=tmp_path,
            artifact="../../private",
        )


def test_monthly_review_forces_every_live_and_send_switch_off(tmp_path: Path):
    completed = type(
        "Completed",
        (),
        {"returncode": 0, "stdout": "ok", "stderr": ""},
    )()
    with patch("baselane_mcp.pipeline.subprocess.run", return_value=completed) as run:
        result = rebuild_monthly_review_artifacts(
            workspace_root=tmp_path,
            run_month="2026-07",
            reporting_cutoff_date="2026-07-31",
        )

    env = run.call_args.kwargs["env"]
    assert result["mode"] == "forced_dry_run_no_external_writes"
    assert env["DRY_RUN"] == "1"
    assert env["BASELANE_MONTHLY_LIVE_ACTIONS_APPROVED"] == "0"
    assert env["APPLY_BASELANE_MONTHLY_ACCRUALS_LIVE"] == "0"
    assert env["APPLY_MONTHLY_DAO_INTEREST_SWEEP_LIVE"] == "0"
    assert env["APPLY_LOFTY_GUARDED_UPDATES"] == "0"
    assert env["APPLY_LOFTY_LIVE_FINANCIAL_CORRECTIONS"] == "0"
    assert env["RUN_LOFTY_GUARDED_APPLY"] == "0"
    assert env["BOOTSTRAP_LOFTY_PUBLIC_DOCS"] == "0"
    assert env["RUN_FUTURE_CF_VALUES_CLEANUP"] == "0"
    assert env["RUN_DAO_VENDOR_UPSTREAM_NORMALIZATION"] == "0"
    assert env["RUN_NONPROPERTY_CATEGORY_NORMALIZATION"] == "0"
    assert env["SEND_OWNER_EMAILS"] == "0"
    assert env["SEND_MONTHLY_DISCORD_REVIEW_DRAFTS"] == "0"
    assert env["YHOME_GSHEET_APPLY"] == "0"


@pytest.mark.parametrize("value", ["2026-00", "2026-13", "2026-7", "../../07"])
def test_run_month_rejects_noncanonical_values(value: str):
    with pytest.raises(PipelineValidationError):
        validate_run_month(value)
