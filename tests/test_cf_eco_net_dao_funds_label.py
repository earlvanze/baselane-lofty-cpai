import importlib.util
import sys
from pathlib import Path

import openpyxl


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit = load_module(
    "cf_eco_net_dao_funds_audit",
    ROOT / "scripts" / "baselane_cf_balance_sheet_consistency_audit.py",
)
cash_apply = load_module(
    "cf_eco_net_dao_funds_apply",
    ROOT / "scripts" / "baselane_cf_balance_sheet_cash_apply.py",
)


def create_workbook(path: Path, eco_cash_label: str = "ECO Operating Cash") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "2026"
    sheet["B1"] = "Jul-26"
    sheet["A2"] = "Lofty Operating Reserve (OR) Balance"
    sheet["A3"] = eco_cash_label
    sheet["A4"] = "ECO General Ledger (ECO GL Column E Total)"
    sheet["B2"] = 10
    sheet["B3"] = 20
    sheet["B4"] = 30
    workbook.save(path)
    workbook.close()


def test_audit_reads_legacy_label_under_canonical_name(tmp_path: Path):
    workbook_path = tmp_path / "Cash Flow Statement.xlsx"
    create_workbook(workbook_path)

    payload = audit.probe_workbook_payload(workbook_path, 2026, 7)

    assert audit.ECO_CASH_LABEL == "ECO Net DAO Funds"
    assert "ECO Operating Cash" in audit.ECO_CASH_LABELS
    assert payload["values"]["ECO Net DAO Funds"]["status"] == "ok"
    assert payload["values"]["ECO Net DAO Funds"]["value"] == 20


def test_apply_relabels_legacy_row_and_keeps_gl_separate(tmp_path: Path):
    property_path = tmp_path / "Test Property"
    workbook_path = (
        property_path
        / "Public"
        / "07 - P&L & Owner Statements"
        / "Cash Flow Statement - Test Property.xlsx"
    )
    create_workbook(workbook_path)
    record = {
        "property_name": "Test Property",
        "property_path": str(property_path),
        "monthly_financial_summary": {
            "lofty_curr_maintenance_reserve": 100,
            "eco_operating_cash": 200,
            "eco_operating_cash_status": "ok",
            "eco_operating_cash_source_mode": "verified_eco_cash_custody_reconciliation",
            "eco_operating_cash_as_of_date": "2026-07-31",
            "eco_general_ledger_sum": 300,
        },
    }

    changes, result = cash_apply.update_workbook(
        audit,
        record,
        year=2026,
        month=7,
        apply=True,
        create_missing_rows=False,
        apply_eco_net_dao_funds=True,
    )

    workbook = openpyxl.load_workbook(workbook_path, data_only=False)
    sheet = workbook["2026"]
    assert result["status"] == "ok"
    assert sheet["A3"].value == "ECO Net DAO Funds"
    assert sheet["B3"].value == 200
    assert sheet["A4"].value == "ECO General Ledger (ECO GL Column E Total)"
    assert sheet["B4"].value == 300
    assert any(change["action"] == "relabel_eco_net_dao_funds_row" for change in changes)
    workbook.close()
