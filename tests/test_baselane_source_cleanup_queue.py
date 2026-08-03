import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "baselane_source_cleanup_queue.py"


def load_module():
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("baselane_source_cleanup_queue", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_missing_inputs_have_complete_report_schema_and_markdown(tmp_path):
    module = load_module()
    report = module.build_report(
        ledger=tmp_path / "missing-ledger.csv",
        source_index=tmp_path / "missing-source-index.csv",
        state=tmp_path / "state.json",
    )

    assert report["status"] == "review"
    assert report["action_count"] == 0
    assert report["missing_id_count"] == 0
    assert report["scope_properties"] == []
    assert report["actions"] == []

    markdown = tmp_path / "queue.md"
    module.write_markdown(markdown, report)
    text = markdown.read_text(encoding="utf-8")
    assert "- Status: `review`" in text
    assert "- Action count: `0`" in text


def test_source_index_iso_date_matches_human_formatted_ledger_date():
    module = load_module()
    ledger_row = {
        "Date": "May 23, 2025",
        "Amount": "-450",
        "Merchant": "CITADEL SERV PMT",
        "Description": "CITADEL SERV PMT | MTGE PAYMT",
        "Property": "90 Madison Ave",
        "Type": "Transfers & Other",
        "Category": "Transfers Between Accounts",
    }
    source_row = {**ledger_row, "Date": "May 23, 2025", "ISODate": "2025-05-23"}

    assert module.row_key(ledger_row) == module.row_key(source_row)


def test_approved_principal_curtailment_is_not_a_no_dao_cleanup_candidate():
    module = load_module()
    ledger_row = {
        "Account": "90 Madison Ave",
        "Date": "July 21, 2025",
        "Amount": "-750",
        "Merchant": "90 Madison | approved 2025-06 NOI principal curtailment",
        "Description": "CITADEL SERV PMT | MTGE PAYMT",
        "Property": "90 Madison Ave",
        "Type": "Manual",
        "Category": "Loan Payments & Capex",
        "Sub-category": "Mortgage Principal Payments",
        "Notes": "Approved curtailment",
    }
    source_row = {
        **ledger_row,
        "Account": "Strawberry LFTY402 DAO LLC-90 Madison Ave Operations-0172",
        "ISODate": "2025-07-21",
        "Type": "Loan Payments & Capex",
        "Category": "Mortgage Principal Payments",
        "Sub-category": "",
        "BaselaneId": "323033782",
    }

    actions = module.no_dao_mortgage_actions([ledger_row], [source_row], set())

    # Approved 90 Madison principal curtailments from June 2024 through June
    # 2025 are canonical DAO mortgage principal, not orphan rows to remove.
    assert actions == []


def test_source_identity_match_refuses_ambiguous_source_ids():
    module = load_module()
    ledger_row = {
        "Account": "90 Madison Ave",
        "Date": "July 21, 2025",
        "Amount": "-750",
        "Merchant": "90 Madison | approved 2025-06 NOI principal curtailment",
        "Description": "CITADEL SERV PMT | MTGE PAYMT",
        "Property": "90 Madison Ave",
        "Type": "Manual",
        "Category": "Loan Payments & Capex",
        "Sub-category": "Mortgage Principal Payments",
        "Notes": "Approved curtailment",
    }
    source_rows = [
        {**ledger_row, "Account": "DAO checking", "BaselaneId": "1"},
        {**ledger_row, "Account": "DAO savings", "BaselaneId": "2"},
    ]

    assert module.unique_source_identity_match(ledger_row, source_rows) == []
