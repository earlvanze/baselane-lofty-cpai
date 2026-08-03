import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "baselane_daily_source_cash_balance_audit.py"


def load_module():
    scripts_path = str(SCRIPT.parent)
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)
    spec = importlib.util.spec_from_file_location("baselane_daily_source_cash_manifest", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fake_cf():
    return SimpleNamespace(
        OWNER_STATEMENTS_DIR="07 - P&L & Owner Statements",
        normalize_property_name=lambda value: value.lower(),
        cf_candidate_priority_for_property=lambda path, _property: (len(path.parts), path.name),
    )


def write_manifest(path: Path, workbook: Path, source: Path, property_name: str = "Property"):
    path.write_text(
        json.dumps(
            {
                "status": "ok",
                "checked_workbook_count": 1,
                "checked_workbooks": [
                    {
                        "file": str(workbook),
                        "property": property_name,
                        "source_cash_sources": [str(source)],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_manifest_resolves_package_folder_move(tmp_path: Path):
    module = load_module()
    old_statement = tmp_path / "Old Package" / "Property Address" / fake_cf().OWNER_STATEMENTS_DIR
    new_statement = tmp_path / "New Package" / "Property Address" / fake_cf().OWNER_STATEMENTS_DIR
    workbook = new_statement / "Cash Flow Statement - Property.xlsx"
    source = new_statement / "ECO Systems General Ledger - Property.csv"
    new_statement.mkdir(parents=True)
    workbook.touch()
    source.touch()
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, old_statement / workbook.name, old_statement / source.name)

    files, metadata = module.cf_files_from_manifest(fake_cf(), manifest)

    assert files == {"property": workbook}
    assert metadata["missing_manifest_file_count"] == 0


def test_manifest_does_not_cross_property_boundaries_for_public_layout(tmp_path: Path):
    module = load_module()
    statement_name = fake_cf().OWNER_STATEMENTS_DIR
    target_statement = tmp_path / "OH" / "Target" / "Public" / statement_name
    target_workbook = target_statement / "Statements" / "Cash Flow Statement - Target.xlsx"
    target_source = target_statement / "ECO Systems General Ledger - Target.csv"
    decoy = tmp_path / "OH" / "Decoy" / "Public" / statement_name / "Cash Flow Statement - Decoy.xlsx"
    target_workbook.parent.mkdir(parents=True)
    decoy.parent.mkdir(parents=True)
    target_workbook.touch()
    target_source.touch()
    decoy.touch()
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, target_statement / target_workbook.name, target_source, "Target")

    files, _metadata = module.cf_files_from_manifest(fake_cf(), manifest)

    assert files == {"target": target_workbook}


def test_package_source_excludes_dropbox_conflicted_ledger_copies(tmp_path: Path):
    module = load_module()
    statement_dir = tmp_path / fake_cf().OWNER_STATEMENTS_DIR
    statement_dir.mkdir(parents=True)
    workbook = statement_dir / "Cash Flow Statement - Ohio 3-Property Package.xlsx"
    canonical = statement_dir / "ECO Systems General Ledger - 1518 Dille Rd.csv"
    conflicted = (
        statement_dir
        / "ECO Systems General Ledger - 1518 Dille Rd (Earl Co's conflicted copy 2026-08-01).csv"
    )
    workbook.touch()
    canonical.write_text("canonical\n", encoding="utf-8")
    conflicted.write_text("duplicate\n", encoding="utf-8")

    sources = module.canonical_property_split_gls(
        workbook,
        "Ohio 3-Property Package",
    )

    assert sources == [canonical]
