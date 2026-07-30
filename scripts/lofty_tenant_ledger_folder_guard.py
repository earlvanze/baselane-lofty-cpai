#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REAL_ESTATE_ROOT = Path("/mnt/c/Users/digit/Dropbox/Real Estate")


def default_root() -> Path:
    env_root = os.environ.get("OPENCLAW_WORKSPACE") or os.environ.get("WORKSPACE_ROOT")
    if env_root:
        return Path(env_root)
    cwd = Path.cwd()
    if (cwd / "scripts").is_dir():
        return cwd
    return Path(__file__).absolute().parents[1]


DEFAULT_REPORT = default_root() / "reports" / "lofty_tenant_ledger_folder_guard_report.json"
DEFAULT_MARKDOWN = default_root() / "reports" / "lofty_tenant_ledger_folder_guard_report.md"


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize(value: object) -> str:
    text = str(value or "").lower()
    text = re.sub(r"\bpublic\b", " ", text)
    text = re.sub(r"\blfty\d+\b", " ", text)
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


ABBREVIATIONS = {
    "avenue": "ave",
    "street": "st",
    "road": "rd",
    "circle": "cir",
    "drive": "dr",
    "lane": "ln",
    "place": "pl",
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
}
NON_STREET_IDENTITY_TOKENS = {"n", "s", "e", "w", "st", "ave", "rd", "cir", "dr", "ln", "pl", "unit", "floor"}


def abbreviation_variants(value: str) -> set[str]:
    normalized = normalize(value)
    variants = {normalized}
    tokens = normalized.split()
    abbreviated = [ABBREVIATIONS.get(token, token) for token in tokens]
    variants.add(" ".join(abbreviated))
    expanded = []
    reverse = {abbreviation: word for word, abbreviation in ABBREVIATIONS.items()}
    for token in tokens:
        expanded.append(reverse.get(token, token))
    variants.add(" ".join(expanded))
    return {variant for variant in variants if variant}


def is_public_root(path: Path) -> bool:
    name = path.name.lower()
    return name == "public" or name.endswith(" public")


def property_alias_source_name(public_root: Path) -> str:
    if public_root.name.lower() == "public":
        return public_root.parent.name
    return re.sub(r"\s+Public$", "", public_root.name).strip()


def public_root_identity(public_root: Path | str | None) -> str:
    if public_root is None:
        return ""
    try:
        return normalize(property_alias_source_name(Path(public_root)))
    except Exception:
        return ""


def property_aliases(property_dir: Path) -> set[str]:
    name = property_alias_source_name(property_dir)
    aliases = set(abbreviation_variants(name))
    without_city = re.split(r",", name, maxsplit=1)[0].strip()
    aliases.update(abbreviation_variants(without_city))
    return aliases


def property_identity_aliases(*values: object) -> set[str]:
    aliases: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        aliases.update(abbreviation_variants(text))
        aliases.update(abbreviation_variants(re.split(r",", text, maxsplit=1)[0].strip()))
    return aliases


def discover_property_dirs(real_estate_root: Path) -> list[Path]:
    if not real_estate_root.is_dir():
        return []
    public_roots: set[Path] = set()
    for state_dir in real_estate_root.iterdir():
        if not state_dir.is_dir():
            continue
        for property_dir in state_dir.iterdir():
            if not property_dir.is_dir():
                continue
            if is_public_root(property_dir):
                public_roots.add(property_dir)
            public_child = property_dir / "Public"
            if public_child.is_dir():
                public_roots.add(public_child)
    return sorted(
        public_roots
    )


def ledger_subject(path: Path) -> str:
    match = re.match(
        r"Tenant Ledger - (?P<subject>.+?) - Lease \d+ - PII REDACTED - \d{4}-\d{2}-\d{2}(?: - .+)?\.csv$",
        path.name,
    )
    return match.group("subject").strip() if match else ""


def ledger_filename_property(path: Path) -> str:
    match = re.match(
        r"Tenant Ledger - .+? - Lease \d+ - PII REDACTED - \d{4}-\d{2}-\d{2}(?: - (?P<property>.+))?\.csv$",
        path.name,
    )
    return match.group("property").strip() if match and match.group("property") else ""


def ledger_metadata_property(path: Path) -> str:
    if os.environ.get("LOFTY_TENANT_LEDGER_CHECK_METADATA") != "1":
        return ""
    timeout_seconds = float(os.environ.get("LOFTY_TENANT_LEDGER_METADATA_TIMEOUT_SECONDS") or "1.0")
    try:
        result = subprocess.run(
            ["head", "-n", "1", str(path)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except (subprocess.TimeoutExpired, Exception):
        return ""
    first_line = result.stdout
    if not first_line:
        return ""
    try:
        row = next(csv.reader([first_line]))
    except Exception:
        return ""
    for index, value in enumerate(row[:-1]):
        if normalize(value) == "property":
            return str(row[index + 1] or "").strip()
    return ""


def owning_property_dir(path: Path) -> Path | None:
    for parent in path.parents:
        if is_public_root(parent):
            return parent
    return None


def build_alias_index(property_dirs: list[Path]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for property_dir in property_dirs:
        for alias in property_aliases(property_dir):
            index.setdefault(alias, []).append(str(property_dir))
    return index


def manifest_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("records", "items", "leases", "ledgers"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def matched_dirs_for_aliases(
    aliases: set[str],
    alias_index: dict[str, list[str]],
    property_alias_map: dict[str, set[str]],
) -> list[str]:
    matched = {
        matched_dir
        for alias in aliases
        for matched_dir in alias_index.get(alias, [])
    }
    for alias in aliases:
        for property_dir, property_aliases_ in property_alias_map.items():
            if alias_street_compatible({alias}, property_aliases_):
                matched.add(property_dir)
    return sorted(matched)


def classify_ledger(
    path: Path,
    alias_index: dict[str, list[str]],
    property_alias_map: dict[str, set[str]],
) -> dict[str, Any]:
    property_dir = owning_property_dir(path)
    subject = ledger_subject(path)
    filename_property = ledger_filename_property(path)
    metadata_property = ledger_metadata_property(path)
    subject_aliases = abbreviation_variants(subject) if subject else set()
    filename_property_aliases = property_identity_aliases(filename_property)
    metadata_aliases = property_identity_aliases(metadata_property)
    current_aliases = property_aliases(property_dir) if property_dir else set()
    matched_property_dirs = matched_dirs_for_aliases(subject_aliases, alias_index, property_alias_map)
    filename_matched_property_dirs = matched_dirs_for_aliases(filename_property_aliases, alias_index, property_alias_map)
    metadata_matched_property_dirs = matched_dirs_for_aliases(metadata_aliases, alias_index, property_alias_map)
    current_path = str(property_dir) if property_dir else ""
    current_identity = public_root_identity(property_dir)
    matched_other_dirs = [
        matched
        for matched in matched_property_dirs
        if matched != current_path and public_root_identity(matched) != current_identity
    ]
    metadata_matched_other_dirs = [
        matched
        for matched in metadata_matched_property_dirs
        if matched != current_path and public_root_identity(matched) != current_identity
    ]
    filename_matched_other_dirs = [
        matched
        for matched in filename_matched_property_dirs
        if matched != current_path and public_root_identity(matched) != current_identity
    ]
    matched_current = current_path in matched_property_dirs or bool(subject_aliases & current_aliases)
    filename_matched_current = current_path in filename_matched_property_dirs or bool(
        filename_property_aliases & current_aliases
    ) or alias_street_compatible(filename_property_aliases, current_aliases)
    metadata_matched_current = current_path in metadata_matched_property_dirs or bool(
        metadata_aliases & current_aliases
    ) or alias_street_compatible(metadata_aliases, current_aliases)
    if not subject:
        status = "review"
        issue = "unparseable_tenant_ledger_filename"
    elif metadata_matched_other_dirs and not metadata_matched_current:
        status = "review"
        issue = "cross_property_tenant_ledger_metadata"
    elif filename_matched_other_dirs and not filename_matched_current:
        status = "review"
        issue = "cross_property_tenant_ledger_filename_property"
    elif matched_other_dirs and not matched_current:
        status = "review"
        issue = "cross_property_tenant_ledger"
    elif matched_other_dirs and matched_current:
        status = "review"
        issue = "ambiguous_tenant_ledger_property_match"
    else:
        status = "ok"
        issue = None
    return {
        "status": status,
        "issue": issue,
        "path": str(path),
        "property_dir": current_path,
        "ledger_subject": subject,
        "ledger_filename_property": filename_property,
        "ledger_metadata_property": metadata_property,
        "matched_property_dirs": matched_property_dirs,
        "matched_other_property_dirs": matched_other_dirs,
        "filename_matched_property_dirs": filename_matched_property_dirs,
        "filename_matched_other_property_dirs": filename_matched_other_dirs,
        "metadata_matched_property_dirs": metadata_matched_property_dirs,
        "metadata_matched_other_property_dirs": metadata_matched_other_dirs,
    }


def destination_property_aliases(path_text: str) -> set[str]:
    path = Path(path_text)
    for part in reversed(path.parts):
        if part.lower().endswith(" public"):
            return property_aliases(Path(part))
    parts = path.parts
    for index in range(len(parts) - 1, -1, -1):
        if parts[index].lower() == "public" and index > 0:
            return property_aliases(Path(parts[index - 1]) / parts[index])
    return set()


def destination_public_root(path_text: str) -> str:
    path = Path(path_text)
    parts = path.parts
    for index in range(len(parts) - 1, -1, -1):
        part = parts[index]
        if part.lower().endswith(" public") or part.lower() == "public":
            return str(Path(*parts[: index + 1]))
    return ""


def destination_is_public_tenant_ledger_file(path_text: str) -> bool:
    path = Path(path_text)
    return path.name.startswith("Tenant Ledger - ") and path.suffix.lower() == ".csv"


def destination_uses_legacy_financials(path_text: str) -> bool:
    return any(part.lower() == "financials" for part in Path(path_text).parts)


def destination_uses_canonical_public_ledger_path(path_text: str) -> bool:
    parts = [part.lower() for part in Path(path_text).parts]
    for index, part in enumerate(parts):
        if part == "07 - p&l & owner statements":
            return index + 1 < len(parts) and parts[index + 1] == "tenant ledgers"
    return False


def alias_street_compatible(expected_aliases: set[str], destination_aliases: set[str]) -> bool:
    for expected in expected_aliases:
        expected_tokens = expected.split()
        if not expected_tokens or not expected_tokens[0].isdigit():
            continue
        expected_street_tokens = set(expected_tokens[1:]) - NON_STREET_IDENTITY_TOKENS
        if not expected_street_tokens:
            continue
        for destination in destination_aliases:
            destination_tokens = destination.split()
            if not destination_tokens or destination_tokens[0] != expected_tokens[0]:
                continue
            destination_street_tokens = set(destination_tokens[1:]) - NON_STREET_IDENTITY_TOKENS
            if expected_street_tokens & destination_street_tokens:
                return True
    return False


def classify_manifest_destination(
    manifest_path: Path,
    record_index: int,
    record: dict[str, Any],
    destination: str,
) -> dict[str, Any] | None:
    expected_aliases = property_identity_aliases(
        record.get("propertyName"),
        record.get("propertyAddress"),
        record.get("property"),
        record.get("address"),
    )
    destination_aliases = destination_property_aliases(destination)
    if not expected_aliases or not destination_aliases:
        return None
    if expected_aliases & destination_aliases or alias_street_compatible(expected_aliases, destination_aliases):
        return None
    return {
        "status": "review",
        "issue": "manifest_cross_property_public_destination",
        "manifest_path": str(manifest_path),
        "record_index": record_index,
        "property_name": record.get("propertyName") or record.get("property"),
        "property_address": record.get("propertyAddress") or record.get("address"),
        "destination_path": destination,
        "expected_aliases": sorted(expected_aliases),
        "destination_aliases": sorted(destination_aliases),
    }


def classify_manifest_destination_shape(
    manifest_path: Path,
    record_index: int,
    destination: str,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if destination_uses_legacy_financials(destination):
        issues.append(
            {
                "status": "review",
                "issue": "manifest_legacy_financials_destination",
                "manifest_path": str(manifest_path),
                "record_index": record_index,
                "destination_path": destination,
                "expected_path_segment": "07 - P&L & Owner Statements",
            }
        )
    if destination_is_public_tenant_ledger_file(destination) and not destination_uses_canonical_public_ledger_path(destination):
        issues.append(
            {
                "status": "review",
                "issue": "manifest_noncanonical_tenant_ledger_destination",
                "manifest_path": str(manifest_path),
                "record_index": record_index,
                "destination_path": destination,
                "expected_path_segment": "07 - P&L & Owner Statements/Tenant Ledgers",
            }
        )
    return issues


def classify_manifest_public_roots(
    manifest_path: Path,
    record_index: int,
    record: dict[str, Any],
    destinations: list[str],
) -> dict[str, Any] | None:
    public_roots = sorted({root for destination in destinations if (root := destination_public_root(destination))})
    public_root_identities = sorted({identity for root in public_roots if (identity := public_root_identity(root))})
    if len(public_root_identities) <= 1:
        return None
    return {
        "status": "review",
        "issue": "manifest_multiple_public_destination_roots",
        "manifest_path": str(manifest_path),
        "record_index": record_index,
        "property_name": record.get("propertyName") or record.get("property"),
        "property_address": record.get("propertyAddress") or record.get("address"),
        "public_root_count": len(public_roots),
        "public_root_identity_count": len(public_root_identities),
        "public_roots": public_roots,
    }


def classify_manifest(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [
            {
                "status": "review",
                "issue": "unreadable_tenant_ledger_manifest",
                "manifest_path": str(path),
                "error": str(exc),
            }
        ]
    issues: list[dict[str, Any]] = []
    for index, record in enumerate(manifest_items(data)):
        destinations: list[str] = []
        for key in ("publicFolders", "publicFiles"):
            value = record.get(key)
            if isinstance(value, list):
                destinations.extend(str(item) for item in value if item)
        public_roots_issue = classify_manifest_public_roots(path, index, record, destinations)
        if public_roots_issue:
            issues.append(public_roots_issue)
        for destination in sorted(set(destinations)):
            issues.extend(classify_manifest_destination_shape(path, index, destination))
            issue = classify_manifest_destination(path, index, record, destination)
            if issue:
                issues.append(issue)
    return issues


def build_report(real_estate_root: Path, manifest_paths: list[Path] | None = None) -> dict[str, Any]:
    property_dirs = discover_property_dirs(real_estate_root)
    alias_index = build_alias_index(property_dirs)
    property_alias_map = {str(property_dir): property_aliases(property_dir) for property_dir in property_dirs}
    ledger_paths = sorted(
        {
            ledger_path
            for property_dir in property_dirs
            for ledger_path in (property_dir / "07 - P&L & Owner Statements" / "Tenant Ledgers").glob("Tenant Ledger - *.csv")
        }
    )
    records = [classify_ledger(path, alias_index, property_alias_map) for path in ledger_paths]
    issues = [record for record in records if record.get("status") != "ok"]
    manifest_paths = manifest_paths or []
    manifest_issues = [
        issue
        for manifest_path in manifest_paths
        for issue in classify_manifest(manifest_path)
    ]
    missing_root = not real_estate_root.is_dir()
    status = "ok" if not issues and not manifest_issues and not missing_root else "review"
    return {
        "generated_at": iso_z(),
        "status": status,
        "policy": "Fail closed before investor-facing reporting when a tenant ledger filename or generated public manifest destination identifies a different known property than its containing folder.",
        "real_estate_root": str(real_estate_root),
        "property_dir_count": len(property_dirs),
        "checked_count": len(records),
        "manifest_checked_count": len(manifest_paths),
        "manifest_issue_count": len(manifest_issues),
        "issue_count": len(issues) + len(manifest_issues) + (1 if missing_root else 0),
        "missing_root": missing_root,
        "issues": issues + manifest_issues,
        "records": records,
    }


def write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Lofty Tenant Ledger Folder Guard",
        "",
        f"- Status: `{report['status']}`",
        f"- Checked ledgers: `{report['checked_count']}`",
        f"- Issues: `{report['issue_count']}`",
        f"- Policy: {report['policy']}",
        "",
        "## Issues",
        "",
    ]
    for issue in report.get("issues") or []:
        if issue.get("manifest_path"):
            lines.append(
                f"- `{issue.get('issue')}` — `{issue.get('manifest_path')}` — destination `{issue.get('destination_path')}`"
            )
        else:
            lines.append(
                f"- `{issue.get('issue')}` — `{issue.get('path')}` — subject `{issue.get('ledger_subject')}`"
            )
            for matched in issue.get("matched_other_property_dirs") or []:
                lines.append(f"  - Matched other property: `{matched}`")
    if not report.get("issues"):
        lines.append("- None")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect cross-property tenant ledger files before investor-facing reporting.")
    parser.add_argument("--real-estate-root", type=Path, default=DEFAULT_REAL_ESTATE_ROOT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--manifest", action="append", type=Path, default=[])
    args = parser.parse_args()
    report = build_report(args.real_estate_root, args.manifest)
    write_json(args.report, report)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"status": report["status"], "checked_count": report["checked_count"], "issue_count": report["issue_count"]}, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
