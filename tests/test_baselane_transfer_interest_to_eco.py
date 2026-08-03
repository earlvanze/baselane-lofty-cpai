import csv
import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "baselane_transfer_interest_to_eco",
    ROOT / "scripts" / "baselane_transfer_interest_to_eco.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_source_property_ids_keeps_only_unambiguous_ids(tmp_path):
    source = tmp_path / "source.csv"
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Property", "PropertyId"])
        writer.writeheader()
        writer.writerows(
            [
                {"Property": "88 Madison Ave", "PropertyId": "65338"},
                {"Property": "88 Madison Ave", "PropertyId": "65338"},
                {"Property": "Conflict", "PropertyId": "1"},
                {"Property": "Conflict", "PropertyId": "2"},
            ]
        )

    assert MODULE.source_property_ids(source) == {"88 Madison Ave": "65338"}


def test_public_plan_uses_requested_month():
    report = {"as_of": "2026-07-31"}
    live = {
        MODULE.ECO_TRANSFER_ACCOUNT_ID: {"available_balance": "100.00"},
    }
    plan = MODULE.public_plan([], live, [], report, "2026-07")

    assert plan["policy"]["period"] == "through 2026-07"
    assert plan["transfer_count"] == 0
    assert plan["total"] == "0.00"
