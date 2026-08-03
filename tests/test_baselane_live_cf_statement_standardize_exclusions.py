import importlib.util
import json
import sys
from datetime import date
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "baselane_live_cf_statement_standardize.py"


def load_module():
    spec = importlib.util.spec_from_file_location("baselane_live_cf_statement_standardize_exclusions", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_runtime_map_terminal_exclusion_removes_live_record(tmp_path: Path):
    module = load_module()
    runtime_map = tmp_path / "runtime-map.json"
    runtime_map.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "lofty_property_id": "closed-property",
                        "status": "excluded_no_live_update_or_email",
                        "index_status": "skipped_closed",
                        "exclude_reason": "property is closed",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    included, excluded = module.filter_excluded_live_capture_records(
        [
            {
                "lofty_property_id": "closed-property",
                "property_name": "1845 W 48th St",
                "input_property_path": "/tmp/1845 W 48th St",
            },
            {
                "lofty_property_id": "active-property",
                "property_name": "Active",
                "input_property_path": "/tmp/Active",
            },
        ],
        yhome_csv=None,
        runtime_map=runtime_map,
    )

    assert [row["lofty_property_id"] for row in included] == ["active-property"]
    assert excluded == [
        {
            "property": "1845 W 48th St",
            "property_path": "/tmp/1845 W 48th St",
            "source": "lofty_pm_runtime_map",
            "exclude_reason": "property is closed",
        }
    ]


def test_runtime_map_nonterminal_record_remains_in_scope(tmp_path: Path):
    module = load_module()
    runtime_map = tmp_path / "runtime-map.json"
    runtime_map.write_text(
        json.dumps({"records": [{"lofty_property_id": "active", "status": "ready"}]}),
        encoding="utf-8",
    )
    records = [
        {
            "lofty_property_id": "active",
            "property_name": "Active",
            "input_property_path": "/tmp/Active",
        }
    ]

    included, excluded = module.filter_excluded_live_capture_records(
        records, yhome_csv=None, runtime_map=runtime_map
    )

    assert included == records
    assert excluded == []


def test_closed_month_uses_month_end_source_cash_mode():
    module = load_module()

    assert module.source_cash_mode_for_month(2026, 7, today=date(2026, 8, 2)) == "as_of_month_end"
    assert module.source_cash_mode_for_month(2026, 8, today=date(2026, 8, 2)) == "full_column_e"
