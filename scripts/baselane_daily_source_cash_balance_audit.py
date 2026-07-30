#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from canonical_property_ledger import DivergentCanonicalLedgerError, resolve_equivalent_ledgers
from lofty_monthly_exclusions import DEFAULT_MANUAL_EXCLUDED_PROPERTIES, match_exclusion_guard, monthly_exclusion_guards


ROOT = Path(__file__).absolute().parents[1]
LOCAL_WORKSPACE_ROOT = Path.home() / ".openclaw" / "workspace"
CF_SCRIPT = next(
    (
        candidate
        for candidate in (
            LOCAL_WORKSPACE_ROOT / "skills" / "baselane-financials" / "scripts" / "update_cf_statements.py",
            ROOT / "skills" / "baselane-financials" / "scripts" / "update_cf_statements.py",
        )
        if candidate.is_file()
    ),
    ROOT / "skills" / "baselane-financials" / "scripts" / "update_cf_statements.py",
)
DEFAULT_REPORT = ROOT / "reports" / "baselane_daily_source_cash_balance_report.json"
DEFAULT_DATA_QUALITY_REPORT = ROOT / "reports" / "baselane_ecogl_data_quality_autonomy.json"
DEFAULT_SOURCE_FIX_PLAN = ROOT / "reports" / "baselane_ecogl_source_fix_plan.json"
DEFAULT_SPLIT_REPORT = ROOT / "reports" / "split_ledger_public_financials_last.json"
DEFAULT_GL_CANDIDATES = [
    Path("/mnt/c/Users/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv"),
    Path("/data/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv"),
    Path("/home/digit/Dropbox/Projects/assetrail/ECO Systems General Ledger.csv"),
]
DEFAULT_REAL_ESTATE_CANDIDATES = [
    Path("/mnt/c/Users/digit/Dropbox/Real Estate"),
    Path("/data/Dropbox/Real Estate"),
    Path("/home/digit/Dropbox/Real Estate"),
]
DEFAULT_YHOME_TRANSITION_CANDIDATES = [
    ROOT / "reports" / "yhome_transition_reconciliation.csv",
    ROOT / "reports" / "yhome_transition_reconciliation.live.20260702.refreshed.csv",
    ROOT / "reports" / "yhome_transition_reconciliation.live.20260702.csv",
]
SOURCE_CASH_NON_PROPERTY_ENTITIES = ("EARLDAO",)
COMPOSITE_SOURCE_PROPERTIES = {
    "ohio 3 property package": (
        "1518 Dille Rd",
        "1258 Lily St",
        "1321 Allendale Ave",
    ),
    "ohio 3property package": (
        "1518 Dille Rd",
        "1258 Lily St",
        "1321 Allendale Ave",
    ),
}
US_STATE_DIRS = {
    "AL", "AR", "AZ", "CA", "CO", "CT", "FL", "GA", "HI", "IA",
    "IL", "IN", "KY", "MA", "MD", "MI", "MO", "NC", "NJ", "NY",
    "OH", "OR", "PA", "SC", "TN", "TX", "UT", "VA", "WA", "WI",
}


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def unresolved_source_fix_plan(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {"status": "missing", "action_count": 0, "properties": {}, "path": str(path or "")}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "unreadable",
            "action_count": 0,
            "properties": {},
            "path": str(path),
            "error": f"{type(exc).__name__}: {exc}",
        }
    resolved_statuses = {"already applied", "cleared", "not applicable", "resolved", "verified fixed"}
    properties: dict[str, list[dict[str, Any]]] = {}
    for action in payload.get("actions") or []:
        if not isinstance(action, dict):
            continue
        automation_status = re.sub(r"[^a-z0-9]+", " ", str(action.get("automation_status") or "").lower()).strip()
        action_status = re.sub(r"[^a-z0-9]+", " ", str(action.get("status") or "").lower()).strip()
        if automation_status in resolved_statuses or action_status in resolved_statuses:
            continue
        property_name = str(action.get("property") or action.get("property_name") or "").strip()
        if property_name:
            properties.setdefault(property_name, []).append(action)
    return {
        "status": payload.get("status") or "missing",
        "generated_at": payload.get("generated_at"),
        "action_count": sum(len(actions) for actions in properties.values()),
        "property_count": len(properties),
        "properties": properties,
        "path": str(path),
    }


def default_existing(candidates: list[Path]) -> Path:
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def resolve_source_property(cf: Any, property_name: str, gl_properties: set[str]) -> tuple[str | None, tuple[str, ...]]:
    """Resolve a CF workbook to one GL property or an explicit package source."""
    matched_gl = cf.match_gl_property(property_name, gl_properties)
    if matched_gl:
        return matched_gl, ()
    normalized_property = cf.normalize_property_name(property_name)
    components = COMPOSITE_SOURCE_PROPERTIES.get(normalized_property)
    if not components:
        compact_property = re.sub(r"[^a-z0-9]", "", normalized_property.lower())
        components = next(
            (
                candidate_components
                for candidate_name, candidate_components in COMPOSITE_SOURCE_PROPERTIES.items()
                if re.sub(r"[^a-z0-9]", "", candidate_name.lower()) == compact_property
            ),
            None,
        )
    if not components:
        return None, ()
    resolved_components = tuple(
        component_match
        for component in components
        if (component_match := cf.match_gl_property(component, gl_properties))
    )
    if len(resolved_components) != len(components):
        return None, ()
    return property_name, resolved_components


def load_cf_module():
    spec = importlib.util.spec_from_file_location("baselane_update_cf_statements", CF_SCRIPT)
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot load CF script: {CF_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bounded(items: list[dict[str, Any]], limit: int = 25) -> list[dict[str, Any]]:
    return items[:limit]


def write_progress(report_path: Path, payload: dict[str, Any]) -> None:
    progress_path = report_path.with_suffix(report_path.suffix + ".progress")
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = progress_path.with_suffix(progress_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        tmp.replace(progress_path)
    except PermissionError:
        # Dropbox/Windows can briefly hold the atomic sidecar open. Progress is
        # advisory; never let that transient lock abort the financial audit.
        try:
            progress_path.write_text(tmp.read_text(encoding="utf-8"), encoding="utf-8")
        except (OSError, PermissionError):
            return


def clear_progress(report_path: Path) -> None:
    progress_path = report_path.with_suffix(report_path.suffix + ".progress")
    try:
        progress_path.unlink()
    except (FileNotFoundError, PermissionError):
        pass


def raw_no_dao_mortgage_guard(report_path: Path | None = None) -> dict[str, Any]:
    path = report_path or DEFAULT_DATA_QUALITY_REPORT
    if not path or not Path(path).is_file():
        return {"active": False, "reason": "missing_report", "report": str(path) if path else None, "count": 0}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {
            "active": True,
            "reason": "unreadable_report",
            "report": str(path),
            "error": f"{type(exc).__name__}: {exc}",
            "count": None,
        }
    count = int(data.get("raw_no_dao_mortgage_exception_count") or 0)
    active = count > 0
    return {
        "active": active,
        "reason": "raw_no_dao_mortgage_exceptions" if active else "clear",
        "report": str(path),
        "count": count,
        "status": data.get("status"),
        # This guard controls only raw no-DAO-mortgage exceptions. The ECO
        # report can remain held for unrelated gates, such as weekly CF review.
        "downstream_hold": active,
        "data_quality_status": data.get("status"),
        "data_quality_downstream_hold": data.get("downstream_hold"),
    }


def source_ledger_quality_guard(report_path: Path | None = None) -> dict[str, Any]:
    """Block workbook writes for actual unresolved ECO source-ledger exceptions.

    ``downstream_hold`` also covers non-source review signals, so writes are held
    only when the data-quality report has source exceptions (or is unreadable).
    """
    path = report_path or DEFAULT_DATA_QUALITY_REPORT
    if not path or not Path(path).is_file():
        return {"active": False, "reason": "missing_report", "report": str(path) if path else None, "count": 0}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {
            "active": True,
            "reason": "unreadable_report",
            "report": str(path),
            "error": f"{type(exc).__name__}: {exc}",
            "count": None,
        }
    count = int(data.get("exception_count") or 0)
    return {
        "active": count > 0,
        "reason": "source_ledger_exceptions" if count > 0 else "clear",
        "report": str(path),
        "count": count,
        "status": data.get("status"),
        "downstream_hold": bool(data.get("downstream_hold")),
    }


def duplicate_workbook_resolution(
    property_name: str,
    files: list[str],
    schema_priorities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    public_files = [value for value in files if " public/" in value.lower()]
    candidate_files = public_files or files
    schema_priorities = schema_priorities or {}
    ranked_candidates = sorted(
        candidate_files,
        key=lambda value: (
            schema_priorities.get(value, (99, "unknown_template"))[0]
            if isinstance(schema_priorities.get(value), (list, tuple))
            else 99,
            str(value).lower(),
        ),
    )
    selected = ranked_candidates[0] if ranked_candidates else None
    ignored = [value for value in files if value != selected]
    schema_ranks = {
        value: schema_priorities.get(value, (99, "unknown_template"))[0]
        for value in candidate_files
        if isinstance(schema_priorities.get(value), (list, tuple))
    }
    schema_selected = bool(schema_ranks) and len(set(schema_ranks.values())) > 1
    if schema_selected and ignored:
        action = "Use the most complete DAO/ECO Cash Flow Statement schema as canonical and archive or exclude the less complete duplicate from monthly source-cash audit scope."
        reason = "most_complete_schema_preferred"
    elif public_files and ignored:
        action = "Use the Public Cash Flow Statement as canonical and archive or exclude the non-Public duplicate from monthly source-cash audit scope."
        reason = "public_workbook_preferred"
    elif selected and ignored:
        action = "Choose one canonical Cash Flow Statement workbook for this DAO and archive or exclude the remaining duplicates from monthly source-cash audit scope."
        reason = "manual_canonical_selection_required"
    else:
        action = "No duplicate workbook remediation required."
        reason = "single_workbook"
    return {
        "property": property_name,
        "selected": selected,
        "ignored": ignored,
        "reason": reason,
        "action": action,
    }


def canonical_property_split_gls(
    cf_path: Path,
    property_name: str,
    gl_properties: set[str] | None = None,
    cf: Any | None = None,
) -> list[Path]:
    search_dirs = [cf_path.parent]
    property_root: Path | None = None
    for parent in cf_path.parents:
        if parent.name == "07 - P&L & Owner Statements":
            search_dirs.append(parent)
            break
    if cf is not None:
        state_dirs = getattr(cf, "US_STATE_DIRS", US_STATE_DIRS)
        state_dir = next((parent for parent in cf_path.parents if parent.name.upper() in state_dirs), None)
        property_root = next(
            (parent for parent in cf_path.parents if state_dir is not None and parent.parent == state_dir),
            None,
        )
        if property_root is not None:
            search_dirs.extend(
                directory
                for directory in property_root.glob(f"*/{cf.OWNER_STATEMENTS_DIR}")
                if directory.is_dir()
            )

    def line_count(path: Path) -> int:
        with path.open(encoding="utf-8-sig", errors="ignore") as handle:
            return sum(1 for _ in handle)

    normalized_name = "".join(character for character in property_name.lower() if character.isalnum())
    source_properties = (
        source_cash_property_aliases(cf, property_name, gl_properties or set())
        if cf is not None
        else [property_name]
    )

    def ledger_candidates_in(paths: list[Path]) -> list[Path]:
        return [
            path
            for directory in dict.fromkeys(paths)
            for path in directory.glob("*General Ledger*.csv")
            if path.is_file() and path.name.lower() != "gl rows.csv"
        ]

    def exact_candidates_in(paths: list[Path]) -> list[Path]:
        candidates = ledger_candidates_in(paths)
        if "package" in normalized_name:
            return candidates
        exact: list[Path] = []
        for source_property in source_properties:
            expected_name = "ecosystemsgeneralledger" + "".join(
                character for character in source_property.lower() if character.isalnum()
            ) + "csv"
            exact.extend(
                path
                for path in candidates
                if "".join(character for character in path.name.lower() if character.isalnum()) == expected_name
            )
        return exact

    candidates = ledger_candidates_in(search_dirs)
    if not candidates:
        candidates = []

    if "package" not in normalized_name:
        selected: list[Path] = []
        for source_property in source_properties:
            expected_name = "ecosystemsgeneralledger" + "".join(
                character for character in source_property.lower() if character.isalnum()
            ) + "csv"
            exact_candidates = [
                path
                for path in candidates
                if "".join(character for character in path.name.lower() if character.isalnum()) == expected_name
            ]
            if exact_candidates:
                selected.append(max(exact_candidates, key=lambda path: (line_count(path), path.stat().st_mtime)))
        if selected:
            # Address aliases are alternate exports of one property-wide ECO
            # ledger, not separate bank-account ledgers. Adding both copies
            # double-counts overlapping accrual rows. Divergent aliases are an
            # authoritative-source collision and must not be selected by mtime.
            return [resolve_equivalent_ledgers(selected)]

        # A selected Public CF can live in a legacy folder while its canonical
        # Public owner-statement export lives in the address-named sibling.
        # Search only direct state-level Public statement directories and only
        # when the established GL matcher maps that folder to this source.
        if cf is not None:
            state_dirs = getattr(cf, "US_STATE_DIRS", US_STATE_DIRS)
            state_dir = next(
                (parent for parent in cf_path.parents if parent.name.upper() in state_dirs),
                None,
            )
            sibling_dirs: list[Path] = []
            if state_dir is not None:
                for property_dir in sorted(state_dir.iterdir()):
                    if not property_dir.is_dir() or property_dir.name.startswith("_"):
                        continue
                    public_statement_dir = property_dir / "Public" / cf.OWNER_STATEMENTS_DIR
                    if not public_statement_dir.is_dir():
                        continue
                    folder_match = cf.match_gl_property(property_dir.name, set(source_properties))
                    if folder_match in source_properties:
                        sibling_dirs.append(public_statement_dir)
            sibling_exact = exact_candidates_in(sibling_dirs)
            if sibling_exact:
                sibling_selected: list[Path] = []
                for source_property in source_properties:
                    expected_name = "ecosystemsgeneralledger" + "".join(
                        character for character in source_property.lower() if character.isalnum()
                    ) + "csv"
                    matches = [
                        path
                        for path in sibling_exact
                        if "".join(character for character in path.name.lower() if character.isalnum()) == expected_name
                    ]
                    if matches:
                        sibling_selected.append(
                            max(matches, key=lambda path: (line_count(path), path.stat().st_mtime))
                        )
                if sibling_selected:
                    return [resolve_equivalent_ledgers(sibling_selected)]

        # Some legacy property roots keep the CF in Public while the canonical
        # ledger lives below a deeper nested unit folder. Traverse that mounted
        # subtree only after direct, shallow nested, and sibling candidates
        # fail; eager rglob calls make the daily audit spend minutes repeatedly
        # walking Dropbox.
        if property_root is not None and cf is not None:
            nested_dirs = [
                directory
                for directory in property_root.rglob(cf.OWNER_STATEMENTS_DIR)
                if directory.is_dir() and directory not in search_dirs
            ]
            nested_exact = exact_candidates_in(nested_dirs)
            if nested_exact:
                nested_selected: list[Path] = []
                for source_property in source_properties:
                    expected_name = "ecosystemsgeneralledger" + "".join(
                        character for character in source_property.lower() if character.isalnum()
                    ) + "csv"
                    matches = [
                        path
                        for path in nested_exact
                        if "".join(character for character in path.name.lower() if character.isalnum()) == expected_name
                    ]
                    if matches:
                        nested_selected.append(
                            max(matches, key=lambda path: (line_count(path), path.stat().st_mtime))
                        )
                if nested_selected:
                    return [resolve_equivalent_ledgers(nested_selected)]

        # Dropbox's canonicalization may append the property-folder label
        # after the ledger identity (for example,
        # ``... - 326 South Alcott Street - 326-332 S Alcott St, ...``).
        # Use that local fallback only after exact Public exports, including
        # the canonical sibling directory, have been considered.
        for source_property in source_properties:
            expected_name = "ecosystemsgeneralledger" + "".join(
                character for character in source_property.lower() if character.isalnum()
            ) + "csv"
            expected_prefix = expected_name.removesuffix("csv")
            prefixed_candidates = [
                path
                for path in candidates
                if "".join(character for character in path.stem.lower() if character.isalnum()).startswith(expected_prefix)
            ]
            if prefixed_candidates:
                selected.append(
                    sorted(
                        prefixed_candidates,
                        key=lambda path: (
                            "recovered" not in path.stem.lower(),
                            line_count(path),
                            path.stat().st_mtime,
                        ),
                        reverse=True,
                    )[0]
                )
        if selected:
            return [resolve_equivalent_ledgers(selected)]

        return []

    if not candidates and property_root is not None and cf is not None:
        nested_dirs = [
            directory
            for directory in property_root.rglob(cf.OWNER_STATEMENTS_DIR)
            if directory.is_dir() and directory not in search_dirs
        ]
        candidates = ledger_candidates_in(nested_dirs)

    grouped: dict[str, list[Path]] = {}
    prefix = "ECO Systems General Ledger - "
    for path in candidates:
        component_text = path.stem[len(prefix):] if path.stem.startswith(prefix) else path.stem
        component_text = re.split(
            r"\s+-\s+.*\bproperty\s+package\b.*$",
            component_text,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()
        component_key = "".join(character for character in component_text.lower() if character.isalnum())
        grouped.setdefault(component_key, []).append(path)

    selected: list[Path] = []

    for component_candidates in grouped.values():
        component_text = next(
            path.stem[len(prefix):]
            for path in component_candidates
            if path.stem.startswith(prefix)
        )
        component_text = re.split(
            r"\s+-\s+.*\bproperty\s+package\b.*$",
            component_text,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()
        selected.append(
            sorted(
                component_candidates,
                key=lambda path: (
                    path.stem == f"{prefix}{component_text}",
                    line_count(path),
                    path.stat().st_mtime,
                ),
                reverse=True,
            )[0]
        )
    return sorted(selected, key=lambda path: str(path).lower())


def canonical_property_split_gl(cf_path: Path, property_name: str) -> Path | None:
    candidates = canonical_property_split_gls(cf_path, property_name)
    if not candidates:
        return None
    return candidates[0]


def source_cash_match_key(cf: Any, value: str) -> str:
    key = cf.normalize_property_name(value)
    for long_form, short_form in {
        "mount": "mt",
        "circle": "cir",
        "south": "s",
        "north": "n",
        "east": "e",
        "west": "w",
    }.items():
        key = re.sub(rf"\b{long_form}\b", short_form, key)
    key = re.sub(r"\b(\d+)(st|nd|rd|th)\b", r"\1", key)
    return re.sub(r"\s+", " ", key).strip()


def source_cash_tokens(cf: Any, value: str) -> set[str]:
    return {
        token
        for token in source_cash_match_key(cf, value).split()
        if token.isdigit() or len(token) > 1
    }


def source_cash_name_matches(cf: Any, left: str, right: str) -> bool:
    left_key = source_cash_match_key(cf, left)
    right_key = source_cash_match_key(cf, right)
    if not left_key or not right_key:
        return False
    if left_key == right_key or left_key in right_key or right_key in left_key:
        return True
    left_tokens = source_cash_tokens(cf, left)
    right_tokens = source_cash_tokens(cf, right)
    return bool(left_tokens and left_tokens.issubset(right_tokens))


def source_cash_alias_matches(cf: Any, left: str, right: str) -> bool:
    """Match address variants without treating an address fragment as a separate DAO."""
    return source_cash_name_matches(cf, left, right) or source_cash_name_matches(cf, right, left)


def source_cash_property_aliases(cf: Any, property_name: str, gl_properties: set[str]) -> list[str]:
    if not gl_properties:
        return [property_name]
    matched_property, components = resolve_source_property(cf, property_name, gl_properties)
    if components:
        return list(components)
    canonical = matched_property or property_name
    aliases = [
        candidate
        for candidate in gl_properties
        if source_cash_alias_matches(cf, canonical, candidate)
    ]
    if matched_property and matched_property not in aliases:
        aliases.append(matched_property)
    return sorted(set(aliases), key=lambda value: value.lower())


def source_cash_canonical_identity(cf: Any, property_name: str, gl_properties: set[str]) -> str:
    """Use one stable identity for a DAO's base and address-suffixed workbooks."""
    matched_property, components = resolve_source_property(cf, property_name, gl_properties)
    if components:
        return matched_property or property_name
    aliases = source_cash_property_aliases(cf, matched_property or property_name, gl_properties)
    if not aliases:
        return matched_property or property_name
    return min(
        aliases,
        key=lambda value: (
            len(source_cash_tokens(cf, value)),
            len(source_cash_match_key(cf, value)),
            value.lower(),
        ),
    )


def source_cash_scope_matches(cf: Any, left: str, right: str, gl_properties: set[str]) -> bool:
    """Match split-report aliases through their canonical central-ledger identity."""
    if source_cash_name_matches(cf, left, right):
        return True
    left_property, left_components = resolve_source_property(cf, left, gl_properties)
    right_property, right_components = resolve_source_property(cf, right, gl_properties)
    left_identities = set(left_components or ((left_property,) if left_property else ()))
    right_identities = set(right_components or ((right_property,) if right_property else ()))
    return bool(left_identities and right_identities and left_identities.intersection(right_identities))


def create_missing_split_scope_cf_workbooks(
    cf: Any,
    cf_files: dict[str, Path],
    discovery_metadata: dict[str, Any],
    split_current_properties: list[str],
    year: int,
    apply: bool,
    exclusion_guards: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not split_current_properties:
        return []
    template = cf.select_cf_template(cf_files) if hasattr(cf, "select_cf_template") else None
    owner_statement_dirs = [
        Path(value)
        for value in (discovery_metadata.get("canonical_owner_statement_dirs") or {}).values()
        if str(value or "").strip()
    ]
    created: list[dict[str, Any]] = []
    for property_name in split_current_properties:
        if match_exclusion_guard(Path(property_name), exclusion_guards):
            continue
        if any(source_cash_name_matches(cf, property_name, key) for key in cf_files):
            continue
        ledger_candidates: list[Path] = []
        for owner_statement_dir in owner_statement_dirs:
            if not owner_statement_dir.is_dir():
                continue
            ledger_candidates.extend(
                path
                for path in owner_statement_dir.glob("*General Ledger*.csv")
                if path.is_file()
                and path.name.lower() != "gl rows.csv"
                and source_cash_name_matches(cf, property_name, path.stem)
            )
        result = {
            "property": property_name,
            "status": "blocked",
            "dry_run": not apply,
            "template_path": str(template) if template else None,
        }
        if not ledger_candidates:
            result["reason"] = "property_split_gl_not_found"
            created.append(result)
            continue
        if template is None:
            result["reason"] = "template_missing"
            result["property_split_gl_candidates"] = [str(path) for path in ledger_candidates[:5]]
            created.append(result)
            continue
        split_gl = sorted(ledger_candidates, key=lambda path: (len(path.name), str(path).lower()))[0]
        target_dir = split_gl.parent / "Statements" if (split_gl.parent / "Statements").is_dir() else split_gl.parent
        target_path = target_dir / cf.safe_cf_filename(property_name)
        result.update(
            {
                "status": "would_create" if not apply else "created",
                "property_split_gl": str(split_gl),
                "target_path": str(target_path),
            }
        )
        if target_path.exists():
            result["status"] = "exists"
            cf_files[cf.normalize_property_name(property_name)] = target_path
            created.append(result)
            continue
        if not apply:
            created.append(result)
            continue
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(template, target_path)
            wb = cf.openpyxl.load_workbook(target_path)
            result["cleared_template_cell_count"] = cf.clear_template_workbook_data(wb, year)
            no_mortgage_clear = cf.clear_no_mortgage_debt_rows(wb, target_path)
            result["no_mortgage_debt_state"] = no_mortgage_clear.get("state")
            result["no_mortgage_debt_checked"] = no_mortgage_clear.get("checked")
            result["no_mortgage_debt_cleared_cell_count"] = no_mortgage_clear.get("cleared_cell_count")
            wb.save(target_path)
            wb.close()
        except Exception as exc:  # noqa: BLE001
            result["status"] = "failed"
            result["reason"] = "create_failed"
            result["error"] = f"{type(exc).__name__}: {exc}"
            created.append(result)
            continue
        cf_files[cf.normalize_property_name(property_name)] = target_path
        created.append(result)
    return created


def load_split_current_properties(report_path: Path | None) -> tuple[list[str], dict[str, Any]]:
    if not report_path:
        return [], {"status": "not_configured", "path": None}
    path = Path(report_path)
    if not path.is_file():
        return [], {"status": "missing", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [], {"status": "unreadable", "path": str(path), "error": f"{type(exc).__name__}: {exc}"}
    properties = [
        str(value)
        for value in (data.get("output_current_properties") or [])
        if str(value or "").strip()
    ]
    return properties, {
        "status": data.get("status"),
        "path": str(path),
        "output_current_count": data.get("output_current_count"),
        "output_current_property_count": len(properties),
    }


def discover_cf_files_fast(cf: Any, real_estate_root: Path) -> tuple[dict[str, Path], dict[str, Any]]:
    """Discover canonical CF workbooks without recursively walking Dropbox."""
    cf_files: dict[str, Path] = {}
    candidates: dict[str, list[Path]] = {}
    owner_statement_dirs: dict[str, str] = {}
    property_dir_names: dict[str, str] = {}
    skipped: dict[str, list[dict[str, str]]] = {}
    duplicate_candidates: dict[str, dict[str, Any]] = {}
    real_estate = Path(real_estate_root)
    if not real_estate.exists():
        return {}, {
            "discovery_mode": "fast_direct_owner_statement_dirs",
            "skipped": {},
            "canonical_property_count": 0,
            "canonical_owner_statement_dirs": {},
            "duplicate_candidates": {},
            "duplicate_candidate_property_count": 0,
            "missing_root": str(real_estate),
        }

    def has_direct_statement_dir(path: Path) -> bool:
        return any(
            statement_dir.is_dir()
            and cf.is_direct_property_owner_statement_dir(statement_dir, path)
            for statement_dir in (
                path / cf.OWNER_STATEMENTS_DIR,
                path / "Public" / cf.OWNER_STATEMENTS_DIR,
            )
        )

    def scan_property_dirs(prop_dir: Path, *, max_depth: int = 3) -> list[Path]:
        property_dirs: list[Path] = []
        queue: list[tuple[Path, int]] = [(prop_dir, 0)]
        while queue:
            current, depth = queue.pop(0)
            if current.name in {getattr(cf, "LEGACY_PUBLIC_DIR_PART", "Public"), "reports"}:
                continue
            if has_direct_statement_dir(current):
                property_dirs.append(current)
                continue
            if depth >= max_depth:
                continue
            for child_dir in sorted(current.iterdir()):
                if not child_dir.is_dir() or child_dir.name.startswith("_"):
                    continue
                queue.append((child_dir, depth + 1))
        return property_dirs or [prop_dir]

    for state_dir in sorted(real_estate.iterdir()):
        if not state_dir.is_dir():
            continue
        if state_dir.name.upper() not in getattr(cf, "US_STATE_DIRS", US_STATE_DIRS):
            continue
        if state_dir.name.startswith("_"):
            continue
        for prop_dir in sorted(state_dir.iterdir()):
            if not prop_dir.is_dir() or prop_dir.name.startswith("_"):
                continue
            if prop_dir.name in {getattr(cf, "LEGACY_PUBLIC_DIR_PART", "Public"), "reports"}:
                continue
            for scan_dir in scan_property_dirs(prop_dir):
                key = cf.normalize_property_name(scan_dir.name)
                property_dir_names[key] = scan_dir.name
                statement_dirs = [
                    scan_dir / cf.OWNER_STATEMENTS_DIR,
                    scan_dir / "Public" / cf.OWNER_STATEMENTS_DIR,
                ]
                for statement_dir in statement_dirs:
                    if not statement_dir.is_dir():
                        continue
                    if not cf.is_direct_property_owner_statement_dir(statement_dir, scan_dir):
                        continue
                    owner_statement_dirs[key] = str(statement_dir)
                    workbook_dirs = [
                        statement_dir,
                        statement_dir / "P&L Statements",
                        statement_dir / "Statements",
                    ]
                    for workbook_dir in workbook_dirs:
                        if not workbook_dir.is_dir():
                            continue
                        for path in sorted(workbook_dir.glob("Cash Flow Statement*.xlsx")):
                            workbook_property = cf.property_name_from_cf_file(path) or scan_dir.name
                            workbook_key = cf.normalize_property_name(workbook_property)
                            property_dir_names.setdefault(workbook_key, workbook_property)
                            filename = path.name.lower()
                            if (
                                "conflicted copy" in filename
                                or "conflict" in filename
                                or ".before-" in filename
                            ):
                                continue
                            if cf.is_legacy_public_finance_path(path):
                                skipped.setdefault(workbook_key, []).append(
                                    {"path": str(path), "reason": "legacy_public_finance_dir_ignored"}
                                )
                                continue
                            candidates.setdefault(workbook_key, []).append(path)

    for key, paths in candidates.items():
        prop_dir_name = property_dir_names.get(key) or key
        schema_priorities = {}
        if len(paths) > 1:
            schema_priorities = {path: cf.cf_workbook_schema_priority(path) for path in paths}
        ranked_paths = sorted(
            paths,
            key=lambda path: (
                schema_priorities.get(path, (0, "not_needed"))[0],
                cf.cf_candidate_priority_for_property(path, prop_dir_name),
            ),
        )
        cf_files[key] = ranked_paths[0]
        if len(ranked_paths) > 1:
            duplicate_candidates[key] = {
                "selected": str(ranked_paths[0]),
                "ignored": [str(path) for path in ranked_paths[1:]],
                "candidate_count": len(ranked_paths),
                "schema_priorities": {str(path): schema_priorities.get(path) for path in ranked_paths},
            }

    return cf_files, {
        "discovery_mode": "fast_direct_owner_statement_dirs",
        "skipped": skipped,
        "canonical_property_count": len(cf_files),
        "canonical_owner_statement_dirs": owner_statement_dirs,
        "property_dir_names": property_dir_names,
        "duplicate_candidates": duplicate_candidates,
        "duplicate_candidate_property_count": len(duplicate_candidates),
    }


def build_report(
    *,
    month: str,
    gl_csv: Path,
    real_estate_root: Path,
    report_path: Path,
    apply: bool,
    apply_requires_property_split_source: bool = False,
    source_cash_mode: str = "full_column_e",
    yhome_transition_csv: Path | None = None,
    data_quality_report: Path | None = None,
    source_fix_plan: Path | None = None,
    split_report: Path | None = None,
) -> dict[str, Any]:
    year, month_number = map(int, month.split("-"))
    cf = load_cf_module()
    cf.GL_PATH = gl_csv
    cf.REAL_ESTATE_BASE = real_estate_root
    cf.OUTPUT_DIR = report_path.parent

    transactions = cf.load_gl_data(gl_csv)
    source_digest_cache: dict[Path, str] = {}

    def source_digest(path: Path) -> str:
        if path not in source_digest_cache:
            source_digest_cache[path] = sha256_file(path)
        return source_digest_cache[path]

    gl_properties = {transaction["_property"] for transaction in transactions if transaction.get("_property")}
    cf_files, discovery_metadata = discover_cf_files_fast(cf, real_estate_root)
    if not cf_files:
        cf_files, discovery_metadata = cf.discover_cf_files(include_metadata=True)
        discovery_metadata = {**discovery_metadata, "discovery_mode": "recursive_fallback"}
    exclusion_guards, yhome_guard, manual_exclusions = monthly_exclusion_guards(
        yhome_transition_csv,
        DEFAULT_MANUAL_EXCLUDED_PROPERTIES,
    )
    exclusion_guards = [
        *exclusion_guards,
        *[
            {
                "source": "source_cash_non_property_entity",
                "property_name": name,
                "normalized_property": cf.normalize_property_name(name),
                "exclude_reason": "non-property entity has no property Cash Flow Statement target",
            }
            for name in SOURCE_CASH_NON_PROPERTY_ENTITIES
        ],
    ]
    raw_mortgage_guard = (
        raw_no_dao_mortgage_guard(data_quality_report)
        if data_quality_report is not None
        else {"active": False, "reason": "not_configured", "report": None, "count": 0}
    )
    source_ledger_guard = (
        source_ledger_quality_guard(data_quality_report)
        if data_quality_report is not None
        else {"active": False, "reason": "not_configured", "report": None, "count": 0}
    )
    source_fix_manifest = unresolved_source_fix_plan(source_fix_plan)
    source_fix_plan_covers_quality_guard = bool(
        source_ledger_guard.get("active")
        and source_fix_manifest.get("status") in {"ok", "review"}
        and int(source_fix_manifest.get("action_count") or 0)
        == int(source_ledger_guard.get("count") or 0)
        and int(source_fix_manifest.get("property_count") or 0) > 0
    )
    split_current_properties, split_scope = load_split_current_properties(split_report)
    # Some property accounting sources are intentionally excluded from the
    # consolidated Baselane export (for example, Coolwood's Citadel ledger).
    # The verified split inventory still identifies their canonical property
    # GLs, so include those identities when resolving workbooks.
    gl_properties.update(split_current_properties)

    def split_scope_contains(value: str) -> bool:
        return not split_current_properties or any(
            source_cash_scope_matches(cf, value, split_property, gl_properties)
            for split_property in split_current_properties
        )

    def matching_exclusion(value: str) -> dict[str, Any] | None:
        direct_match = match_exclusion_guard(Path(value), exclusion_guards)
        if direct_match:
            return direct_match
        return next(
            (
                guard
                for guard in exclusion_guards
                if source_cash_name_matches(cf, value, str(guard.get("property_name") or ""))
            ),
            None,
        )

    apply_blocked_by_raw_no_dao_mortgage_guard = bool(apply and raw_mortgage_guard.get("active"))
    apply_blocked_by_source_ledger_quality_guard = bool(
        apply
        and source_ledger_guard.get("active")
        and not source_fix_plan_covers_quality_guard
    )
    effective_apply = bool(
        apply
        and not apply_blocked_by_raw_no_dao_mortgage_guard
        and not apply_blocked_by_source_ledger_quality_guard
    )
    missing_cf_workbook_actions = create_missing_split_scope_cf_workbooks(
        cf,
        cf_files,
        discovery_metadata,
        split_current_properties,
        year,
        effective_apply,
        exclusion_guards,
    )

    checked_count = 0
    no_match_count = 0
    blocking_no_match_count = 0
    missing_row_count = 0
    missing_month_column_count = 0
    unreadable_count = 0
    update_count = 0
    violation_count = 0
    violation_properties: list[str] = []
    violations: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    skipped_excluded_properties: list[dict[str, Any]] = []
    composite_reporting_exclusion_audits: list[dict[str, Any]] = []
    skipped_exclusion_keys: set[str] = set()
    unreadable_files: list[dict[str, Any]] = []
    checked_properties: list[str] = []
    source_quality_held_properties: list[dict[str, Any]] = []
    no_match_properties: list[dict[str, Any]] = []
    blocking_no_match_properties: list[dict[str, Any]] = []
    checked_workbooks: list[dict[str, Any]] = []
    source_cash_evidence: set[tuple[str, str]] = set()
    matched_candidates: dict[str, list[dict[str, Any]]] = {}

    for cf_name, cf_path in sorted(cf_files.items()):
        if cf_name in cf.SKIP_PROPERTIES and not split_scope_contains(cf_name):
            _package_match, package_components = resolve_source_property(cf, cf_name, gl_properties)
            if not package_components or not any(split_scope_contains(component) for component in package_components):
                continue
        file_label = cf.property_name_from_cf_file(cf_path)
        exclusion = matching_exclusion(cf_name) or matching_exclusion(file_label)
        matched_gl, source_components = resolve_source_property(cf, cf_name, gl_properties)
        if not matched_gl:
            matched_gl, source_components = resolve_source_property(cf, file_label, gl_properties)
        if not matched_gl:
            if exclusion:
                exclusion_key = str(exclusion.get("normalized_property") or cf.normalize_property_name(file_label or cf_name))
                if exclusion_key not in skipped_exclusion_keys:
                    skipped_exclusion_keys.add(exclusion_key)
                    skipped_excluded_properties.append(
                        {
                            "property": str(exclusion.get("property_name") or cf_name),
                            "file": str(cf_path),
                            "source": exclusion.get("source"),
                            "exclude_reason": exclusion.get("exclude_reason"),
                            "yhome_column_b": exclusion.get("yhome_column_b"),
                        }
                    )
                continue
            file_label = cf.property_name_from_cf_file(cf_path)
            record = {"property": cf_name, "file": str(cf_path)}
            no_match_count += 1
            no_match_properties.append(record)
            if split_scope_contains(cf_name) or split_scope_contains(file_label):
                blocking_no_match_count += 1
                blocking_no_match_properties.append(record)
            continue

        canonical_identity = source_cash_canonical_identity(cf, matched_gl, gl_properties)
        matched_candidates.setdefault(canonical_identity, []).append(
            {"cf_name": cf_name, "file": str(cf_path), "source_components": source_components}
        )

    duplicate_checked_properties = [
        {
            "property": property_name,
            "workbook_count": len(records),
            "files": [record["file"] for record in records][:10],
            "resolution": duplicate_workbook_resolution(
                property_name,
                [record["file"] for record in records][:10],
                {
                    record["file"]: cf.cf_workbook_schema_priority(Path(record["file"]))
                    for record in records
                },
            ),
        }
        for property_name, records in sorted(matched_candidates.items())
        if len(records) > 1 and not matching_exclusion(property_name)
    ]
    duplicate_properties_seen = {record["property"] for record in duplicate_checked_properties}
    for duplicate_key, duplicate_record in sorted((discovery_metadata.get("duplicate_candidates") or {}).items()):
        selected = str(duplicate_record.get("selected") or "")
        ignored = [str(value) for value in (duplicate_record.get("ignored") or [])]
        if not selected or not ignored:
            continue
        matched_gl = cf.match_gl_property(str(duplicate_key), gl_properties)
        if not matched_gl:
            matched_gl = cf.match_gl_property(cf.property_name_from_cf_file(Path(selected)), gl_properties)
        if not matched_gl or matched_gl in duplicate_properties_seen or matching_exclusion(matched_gl):
            continue
        files = [selected, *ignored]
        duplicate_checked_properties.append(
            {
                "property": matched_gl,
                "workbook_count": len(files),
                "files": files[:10],
                "resolution": duplicate_workbook_resolution(
                    matched_gl,
                    files[:10],
                    duplicate_record.get("schema_priorities") or {},
                ),
            }
        )
        duplicate_properties_seen.add(matched_gl)
    canonical_candidates: list[tuple[str, str, Path, tuple[str, ...]]] = []
    for matched_gl, records in sorted(matched_candidates.items()):
        files = [record["file"] for record in records]
        schema_priorities = {
            path: cf.cf_workbook_schema_priority(Path(path))
            for path in files
        }
        resolution = duplicate_workbook_resolution(matched_gl, files, schema_priorities)
        selected = resolution.get("selected") or files[0]
        selected_record = next((record for record in records if record["file"] == selected), records[0])
        canonical_candidates.append(
            (
                str(selected_record["cf_name"]),
                matched_gl,
                Path(str(selected_record["file"])),
                tuple(selected_record.get("source_components") or ()),
            )
        )

    for cf_name, matched_gl, cf_path, source_components in canonical_candidates:
        exclusion = matching_exclusion(matched_gl) or matching_exclusion(cf_name)
        component_reporting_scope = tuple(
            scope_property
            for scope_property in split_current_properties
            if any(source_cash_name_matches(cf, scope_property, component) for component in source_components)
        )
        exclusion_reason = str((exclusion or {}).get("exclude_reason") or "").lower()
        continues_canonical_cf_reporting = "continue canonical dropbox and cash flow statement reporting" in exclusion_reason
        if exclusion and not (continues_canonical_cf_reporting and component_reporting_scope):
            exclusion_key = str(exclusion.get("normalized_property") or cf.normalize_property_name(matched_gl))
            if exclusion_key not in skipped_exclusion_keys:
                skipped_exclusion_keys.add(exclusion_key)
                skipped_excluded_properties.append(
                    {
                        "property": matched_gl,
                        "file": str(cf_path),
                        "source": exclusion.get("source"),
                        "exclude_reason": exclusion.get("exclude_reason"),
                        "yhome_column_b": exclusion.get("yhome_column_b"),
                    }
                )
            continue
        if exclusion:
            # A consolidated package may be excluded from publication while
            # still owning the canonical CF workbook for active component DAOs.
            # Keep its source-cash audit in scope without making it a Lofty
            # listing, Discord, or owner-email candidate.
            composite_reporting_exclusion_audits.append(
                {
                    "property": matched_gl,
                    "source_components": list(source_components),
                    "covered_split_scope_components": list(component_reporting_scope),
                    "exclude_source": exclusion.get("source"),
                    "exclude_reason": exclusion.get("exclude_reason"),
                }
            )

        held_source_actions: list[dict[str, Any]] = []
        for held_property, actions in source_fix_manifest.get("properties", {}).items():
            if source_cash_name_matches(cf, matched_gl, held_property) or source_cash_name_matches(
                cf, cf_name, held_property
            ):
                held_source_actions.extend(actions)
        if held_source_actions:
            source_quality_held_properties.append(
                {
                    "property": matched_gl,
                    "file": str(cf_path),
                    "action_count": len(held_source_actions),
                    "action_types": sorted(
                        {str(action.get("action_type") or "unknown") for action in held_source_actions}
                    ),
                }
            )

        try:
            property_split_gls = canonical_property_split_gls(cf_path, matched_gl, gl_properties, cf)
        except DivergentCanonicalLedgerError as exc:
            unreadable_count += 1
            unreadable_files.append(
                {
                    "property": matched_gl,
                    "file": str(cf_path),
                    "stage": "canonical_source_resolution",
                    "status": "ambiguous_canonical_source",
                    "sources": [str(path) for path in exc.paths],
                    "error": str(exc),
                }
            )
            continue
        if property_split_gls:
            source_cash_data = []
            for property_split_gl in property_split_gls:
                source_cash_data.extend(cf.load_gl_data(property_split_gl))
            source_cash_source_mode = (
                "canonical_aggregate_property_split_gl"
                if len(property_split_gls) > 1
                else "canonical_property_split_gl"
            )
            source_cash_source = ", ".join(str(path) for path in property_split_gls)
            source_cash_evidence.update((str(path), source_digest(path)) for path in property_split_gls)
        else:
            source_cash_data = []
            for component in source_components or (matched_gl,):
                source_cash_data.extend(cf.filter_by_property(transactions, component))
            source_cash_source_mode = (
                "central_ledger_composite_property_fallback"
                if source_components
                else "central_ledger_property_fallback"
            )
            source_cash_source = str(gl_csv)
            source_cash_evidence.add((str(gl_csv), source_digest(gl_csv)))
        write_progress(
            report_path,
            {
                "generated_at": iso_z(),
                "status": "running",
                "stage": "update" if effective_apply else "audit",
                "property": matched_gl,
                "file": str(cf_path),
                "mode": "apply" if effective_apply else "audit",
                "checked_property_count": checked_count,
                "update_count": update_count,
                "violation_count": violation_count,
                "unreadable_count": unreadable_count,
            },
        )
        # A guarded production refresh may leave central-ledger fallback gaps
        # review-only, while the normal repair mode preserves legacy behavior.
        can_apply_source_cash = (
            not apply_requires_property_split_source
            or not source_cash_source_mode.startswith("central_ledger")
        ) and not held_source_actions
        if effective_apply and can_apply_source_cash:
            try:
                changes = cf.update_xlsx(
                    cf_path,
                    matched_gl,
                    [],
                    year,
                    month_number,
                    dry_run=False,
                    source_cash_data=source_cash_data,
                    source_cash_only=True,
                    source_cash_mode=source_cash_mode,
                )
            except Exception as exc:  # noqa: BLE001
                unreadable_count += 1
                unreadable_files.append(
                    {
                        "property": matched_gl,
                        "file": str(cf_path),
                        "stage": "update",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            for change in changes:
                if change.get("action") == "set_source_cash_balance":
                    update_count += 1
                    updates.append(
                        {
                            "property": matched_gl,
                            "file": str(cf_path),
                            "old_value": change.get("old_value"),
                            "new_value": change.get("new_value"),
                            "source_included_transaction_count": change.get("source_included_transaction_count"),
                            "source_excluded_earldao_interest_count": change.get("source_excluded_earldao_interest_count"),
                            "source_excluded_earldao_interest_total": change.get("source_excluded_earldao_interest_total"),
                            "source_cash_source": source_cash_source,
                            "source_cash_source_mode": source_cash_source_mode,
                        }
                    )

        write_progress(
            report_path,
            {
                "generated_at": iso_z(),
                "status": "running",
                "stage": "audit",
                "property": matched_gl,
                "file": str(cf_path),
                "mode": "apply" if effective_apply else "audit",
                "checked_property_count": checked_count,
                "update_count": update_count,
                "violation_count": violation_count,
                "unreadable_count": unreadable_count,
            },
        )
        try:
            wb = cf.openpyxl.load_workbook(cf_path, data_only=False)
        except Exception as exc:  # noqa: BLE001
            unreadable_count += 1
            unreadable_files.append(
                {
                    "property": matched_gl,
                    "file": str(cf_path),
                    "stage": "audit",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        try:
            sheet = cf.get_year_sheet(wb, year)
            if not sheet:
                no_match_count += 1
                continue
            audit = cf.audit_source_cash_balance_row(
                sheet,
                source_cash_data,
                year,
                month_number,
                source_cash_mode=source_cash_mode,
            )
        finally:
            wb.close()
        checked_count += 1
        checked_properties.append(matched_gl)
        checked_workbooks.append(
            {
                "property": matched_gl,
                "file": str(cf_path),
                "source_cash_source": source_cash_source,
                "source_cash_sources": [str(path) for path in property_split_gls] if property_split_gls else [str(gl_csv)],
                "source_cash_source_mode": source_cash_source_mode,
                "source_components": list(source_components),
            }
        )
        if audit.get("row") is None:
            missing_row_count += 1
        if audit.get("checked") is False:
            missing_month_column_count += 1
        prop_violation_count = int(audit.get("violation_count") or 0)
        if prop_violation_count:
            violation_count += prop_violation_count
            violation_properties.append(matched_gl)
            for violation in audit.get("violations_bounded") or []:
                violations.append(
                    {
                        "property": matched_gl,
                        "file": str(cf_path),
                        "expected": audit.get("expected"),
                        "included_transaction_count": audit.get("included_count"),
                        "excluded_earldao_interest_count": audit.get("excluded_earldao_interest_count"),
                        "source_cash_source": source_cash_source,
                        "source_cash_source_mode": source_cash_source_mode,
                        **violation,
                    }
                )

        checked_properties.extend(component_reporting_scope or source_components or (matched_gl,))

    unique_checked_properties = sorted(set(checked_properties))
    checked_property_keys = {cf.normalize_property_name(value) for value in unique_checked_properties}
    split_scope_missing_properties: list[str] = []
    split_scope_excluded_properties: list[dict[str, Any]] = []
    for value in split_current_properties:
        if any(
            source_cash_scope_matches(cf, value, checked_property, gl_properties)
            for checked_property in unique_checked_properties
        ):
            continue
        exclusion = matching_exclusion(value)
        if exclusion:
            split_scope_excluded_properties.append(
                {
                    "property": value,
                    "source": exclusion.get("source"),
                    "exclude_reason": exclusion.get("exclude_reason"),
                    "yhome_column_b": exclusion.get("yhome_column_b"),
                }
            )
            continue
        split_scope_missing_properties.append(value)

    status = (
        "ok"
        if violation_count == 0
        and missing_row_count == 0
        and missing_month_column_count == 0
        and unreadable_count == 0
        and blocking_no_match_count == 0
        and not split_scope_missing_properties
        and not apply_blocked_by_raw_no_dao_mortgage_guard
        and not apply_blocked_by_source_ledger_quality_guard
        else "review"
    )
    source_cash_evidence_records = [
        {"path": path, "sha256": digest}
        for path, digest in sorted(source_cash_evidence)
    ]
    source_cash_evidence_fingerprint = hashlib.sha256(
        "".join(f"{record['path']}\\0{record['sha256']}\\n" for record in source_cash_evidence_records).encode("utf-8")
    ).hexdigest()
    return {
        "generated_at": iso_z(),
        "job": "baselane-daily-source-cash-balance",
        "status": status,
        "mode": "apply" if apply else "audit",
        "effective_mode": "apply" if effective_apply else "audit",
        "apply_requested": apply,
        "apply_blocked_by_raw_no_dao_mortgage_guard": apply_blocked_by_raw_no_dao_mortgage_guard,
        "raw_no_dao_mortgage_guard": raw_mortgage_guard,
        "apply_blocked_by_source_ledger_quality_guard": apply_blocked_by_source_ledger_quality_guard,
        "source_ledger_quality_guard": source_ledger_guard,
        "source_fix_plan": {
            key: value for key, value in source_fix_manifest.items() if key != "properties"
        },
        "source_fix_plan_covers_quality_guard": source_fix_plan_covers_quality_guard,
        "source_quality_held_property_count": len(source_quality_held_properties),
        "source_quality_held_properties_bounded": bounded(source_quality_held_properties),
        "month": month,
        "source_cash_balance_policy": (
            "ECO Net DAO Funds equals the full property-split ECO GL Column E balance, including "
            "manual accruals and actual bank-transfer rows. Physical bank cash remains a separate row; "
            "closed historical CF columns use the GL balance through month-end."
        ),
        "source_cash_balance_mode": source_cash_mode,
        "gl_csv": str(gl_csv),
        "gl_csv_sha256": source_digest(gl_csv),
        "source_cash_ledger_evidence_count": len(source_cash_evidence_records),
        "source_cash_ledger_evidence_fingerprint": source_cash_evidence_fingerprint,
        "source_cash_ledger_evidence_bounded": bounded(source_cash_evidence_records, limit=50),
        "real_estate_root": str(real_estate_root),
        "checked_workbook_count": checked_count,
        "checked_workbooks_bounded": bounded(checked_workbooks, limit=50),
        "checked_property_count": len(unique_checked_properties),
        "checked_properties": unique_checked_properties,
        "checked_properties_bounded": unique_checked_properties[:50],
        "duplicate_checked_property_count": len(duplicate_checked_properties),
        "duplicate_checked_properties_bounded": bounded(duplicate_checked_properties),
        "split_scope": split_scope,
        "split_scope_expected_property_count": len(split_current_properties),
        "split_scope_missing_property_count": len(split_scope_missing_properties),
        "split_scope_missing_properties_bounded": split_scope_missing_properties[:25],
        "split_scope_excluded_property_count": len(split_scope_excluded_properties),
        "split_scope_excluded_properties_bounded": bounded(split_scope_excluded_properties),
        "cf_file_count": len(cf_files),
        "missing_cf_workbook_action_count": len(missing_cf_workbook_actions),
        "created_missing_cf_workbook_count": sum(1 for action in missing_cf_workbook_actions if action.get("status") == "created"),
        "blocked_missing_cf_workbook_count": sum(1 for action in missing_cf_workbook_actions if action.get("status") in {"blocked", "failed"}),
        "missing_cf_workbook_actions_bounded": bounded(missing_cf_workbook_actions),
        "no_match_count": no_match_count,
        "no_match_properties_bounded": bounded(no_match_properties),
        "blocking_no_match_count": blocking_no_match_count,
        "blocking_no_match_properties_bounded": bounded(blocking_no_match_properties),
        "missing_row_count": missing_row_count,
        "missing_month_column_count": missing_month_column_count,
        "unreadable_count": unreadable_count,
        "update_count": update_count,
        "violation_count": violation_count,
        "violation_property_count": len(set(violation_properties)),
        "violation_properties": sorted(set(violation_properties))[:25],
        "violations_bounded": bounded(violations),
        "updates_bounded": bounded(updates),
        "skipped_excluded_property_count": len(skipped_excluded_properties),
        "skipped_excluded_properties_bounded": bounded(skipped_excluded_properties),
        "composite_reporting_exclusion_audit_count": len(composite_reporting_exclusion_audits),
        "composite_reporting_exclusion_audits_bounded": bounded(composite_reporting_exclusion_audits),
        "manual_excluded_property_names": list(DEFAULT_MANUAL_EXCLUDED_PROPERTIES),
        "manual_excluded_property_count": len(manual_exclusions),
        "yhome_transition_guard": yhome_guard,
        "yhome_transition_csv": str(yhome_transition_csv) if yhome_transition_csv else None,
        "unreadable_files_bounded": bounded(unreadable_files),
        "discovery_mode": discovery_metadata.get("discovery_mode"),
        "ignored_cf_candidate_count": sum(len(paths) for paths in (discovery_metadata.get("skipped") or {}).values()),
        "report": str(report_path),
    }


def write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit or refresh current-month ECO GL source-cash balances in CF statements.")
    parser.add_argument("--month", default=datetime.now().strftime("%Y-%m"), help="Target month in YYYY-MM format; defaults to current month.")
    parser.add_argument("--gl-csv", type=Path, default=default_existing(DEFAULT_GL_CANDIDATES))
    parser.add_argument("--real-estate-root", type=Path, default=default_existing(DEFAULT_REAL_ESTATE_CANDIDATES))
    parser.add_argument("--yhome-transition-csv", type=Path, default=default_existing(DEFAULT_YHOME_TRANSITION_CANDIDATES))
    parser.add_argument("--data-quality-report", type=Path, default=DEFAULT_DATA_QUALITY_REPORT)
    parser.add_argument("--source-fix-plan", type=Path, default=DEFAULT_SOURCE_FIX_PLAN)
    parser.add_argument("--split-report", type=Path, default=DEFAULT_SPLIT_REPORT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--apply", action="store_true", help="Update local CF workbook source-cash cells before auditing.")
    parser.add_argument(
        "--apply-requires-property-split-source",
        action="store_true",
        help="When applying, leave central-ledger fallback balances review-only.",
    )
    parser.add_argument(
        "--source-cash-mode",
        choices=("full_column_e", "as_of_month_end"),
        default="full_column_e",
        help="Use the current full Column E balance or the closed-month balance through month-end.",
    )
    args = parser.parse_args()

    report = build_report(
        month=args.month,
        gl_csv=args.gl_csv,
        real_estate_root=args.real_estate_root,
        report_path=args.report,
        apply=args.apply,
        apply_requires_property_split_source=args.apply_requires_property_split_source,
        source_cash_mode=args.source_cash_mode,
        yhome_transition_csv=args.yhome_transition_csv,
        data_quality_report=args.data_quality_report,
        source_fix_plan=args.source_fix_plan,
        split_report=args.split_report,
    )
    write_json(args.report, report)
    clear_progress(args.report)
    print(json.dumps({key: report[key] for key in ("status", "mode", "month", "checked_property_count", "update_count", "violation_count")}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
