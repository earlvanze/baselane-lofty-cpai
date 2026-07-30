#!/usr/bin/env python3
import argparse
import calendar
import datetime as dt
import json
import os
import re
import shutil
import shlex
import sys
from pathlib import Path
from typing import Any, TextIO

OPENCLAW_ROOT = Path(os.environ.get('OPENCLAW_ROOT', str(Path.home() / '.openclaw')))
if not OPENCLAW_ROOT.exists() and (Path.home() / 'umbrel/app-data/openclaw/home/umbrel/.openclaw').exists():
    OPENCLAW_ROOT = Path.home() / 'umbrel/app-data/openclaw/home/umbrel/.openclaw'
REPO = Path(os.environ.get('WORKSPACE_ROOT', str(OPENCLAW_ROOT / 'workspace')))
DEFAULT_DOWNLOADS = Path(os.environ.get('DOWNLOADS_DIR', str(Path.home() / 'Downloads')))
DEFAULT_PERSONAL = Path(os.environ.get('PERSONAL_ROOT', str(REPO / 'pdf-extracts/personal/07 - P&L & Owner Statements/Bank Statements')))
DEFAULT_HOLDINGS = Path(os.environ.get('HOLDINGS_ROOT', str(REPO / 'pdf-extracts/business-holdings/07 - P&L & Owner Statements/Bank Statements')))
SCRIPT_PATH = Path(__file__).resolve()
ISSUE_CLASS = "baselane-statements-operator"
LFTY_PREFIX_RE = re.compile(r"^LFTY\d+\s+", re.IGNORECASE)
LFTY_SEGMENT_RE = re.compile(r"\bLFTY\d{4}\b", re.IGNORECASE)
STATEMENT_PERIOD_RE = re.compile(r"_([A-Z]{3,4})_(20\d{2})_STATEMENT", re.IGNORECASE)
STATEMENT_ARTIFACT_SUFFIXES = (".pdf", ".md")
LEGACY_FINANCIALS_DIR = "Financials"
BANK_STATEMENTS_DIR = "Bank Statements"

MONTH_TOKENS = {
    1: 'JAN', 2: 'FEB', 3: 'MAR', 4: 'APR', 5: 'MAY', 6: 'JUN',
    7: 'JUL', 8: 'AUG', 9: 'SEP', 10: 'OCT', 11: 'NOV', 12: 'DEC'
}
MONTH_TOKEN_TO_NUMBER = {token: month for month, token in MONTH_TOKENS.items()}
MONTH_TOKEN_TO_NUMBER["SEPT"] = 9


def default_real_estate_root() -> Path:
    env_root = os.environ.get("REAL_ESTATE_ROOT")
    if env_root:
        return Path(env_root)
    candidates = [
        Path("/mnt/c/Users/digit/Dropbox/Real Estate"),
        Path("/mnt/c/users/digit/Dropbox/Real Estate"),
        Path("/data/Dropbox/Real Estate"),
        Path.home() / "Dropbox/Real Estate",
        Path("/home/digit/Dropbox/Real Estate"),
        Path("/mnt/f/Real Estate"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path("/mnt/c/Users/digit/Dropbox/Real Estate")


DEFAULT_RE = default_real_estate_root()

# Ordered mapping rules (first match wins)
DEST_RULES = [
    ('1 COOLWOOD', 'AR/1 Coolwood Dr Little Rock, AR 72202/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('22164 UMLAND CIRCLE', 'CA/22164 Umland Cir, Jenner, CA 95450/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('1315 E 114TH ST', 'OH/1315 E 114th St, Cleveland, OH 44106/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('724 3RD AVE', 'NY/724 3rd Ave, Watervliet, NY 12189/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('88 MADISON AVE', 'NY/88 Madison Ave Albany, NY 12202/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('88 MADISON OPERATIONS', 'NY/88 Madison Ave Albany, NY 12202/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('88 MADISON RESERVES', 'NY/88 Madison Ave Albany, NY 12202/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('86 MADISON AVE', 'NY/86 Madison Ave Albany, NY 12202/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('84 MADISON AVE', 'NY/84 Madison Ave Albany, NY 12202/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('90 MADISON AVE', 'NY/90 Madison Ave Albany, NY 12202/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('1321 ALLENDALE AVE', 'OH/Ohio 3-Property Package/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('1518 DILLE RD', 'OH/APG/1518 Dille Rd/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('OHIO-3 SECURITY DEPOSITS', 'OH/APG/1518 Dille Rd/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('566 NASH ST', 'OH/566 Nash St, Akron, OH 44306/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('566 SECURITY DEPOSITS', 'OH/566 Nash St, Akron, OH 44306/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('254 BOWMANVILLE', 'OH/254 Bowmanville St, Akron, OH 44305/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('1278 E 187TH ST', 'OH/1278 E 187th St, Cleveland, OH 44110/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('1278 SECURITY DEPOSITS', 'OH/1278 E 187th St, Cleveland, OH 44110/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('1432 SARA AVE', 'OH/1432 Sara Ave, Akron, Ohio 44305/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('15555 MILLARD AVE', 'IL/15555 Millard Ave, Markham, IL 60428/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('25 CIRCLE DR', 'IL/25 Circle Dr, Dixmoor, IL 60426/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('27 PILLAR LN', 'FL/27 Pillar Ln, Palm Coast, FL 32164/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('49 BANNBURY LN', 'FL/49 Bannbury Ln, Palm Coast, FL 32137/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('428 CROSS ST', 'OH/428 Cross St, Akron, OH 44311/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('428 SECURITY DEPOSITS', 'OH/428 Cross St, Akron, OH 44311/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('5401 ODOM AVE', 'TX/5401 Odom Ave Fort Worth, TX 76114/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('5541 S PEORIA ST', 'IL/5541 S Peoria St, Chicago, IL 60621/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('7542 & 7656 S COLFAX AVE', 'IL/7542 and 7656 S Colfax Ave, Chicago, IL 60649/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('8143 S SANGAMON ST', 'IL/8143 S Sangamon St, Chicago, IL 60620/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('918 FREDERICK BLVD', 'OH/918 Frederick Blvd, Akron, OH 44320/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('918 SECURITY DEPOSITS', 'OH/918 Frederick Blvd, Akron, OH 44320/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('9634 S GREEN ST', 'IL/9634 S Green St, Chicago, IL 60643/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('9902 GARFIELD AVE', 'OH/9902 Garfield Ave, Cleveland, OH, 44108/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('9919 S OGLESBY AVE', 'IL/9919 S Oglesby Ave, Chicago, IL 60617/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('326-332 S ALCOTT', 'CO/326-332 S Alcott St Denver, CO 80219/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('85-104 ALAWA PL', 'HI/85-104 Alawa Pl, Waianae, HI 96792/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('9 COUNTRY CLUB LANE N', 'NY/9 Country Club Lane N Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('9 COUNTRY CLUB LN', 'NY/9 Country Club Lane N Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('804 S QUITMAN ST', 'CO/804 S Quitman St, Denver, CO 80219/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('3560 SAINT ALBANS', 'MO/3560 Saint Albans Rd. Saint Albans, MO 63073/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
    ('1935 S GLEN RD', 'MI/1935 S Glen Rd, Shelby, MI 49455/Public/07 - P&L & Owner Statements/Bank Statements/{year}'),
]


def normalize_name(name: str) -> str:
    s = re.sub(r' \(\d+\)\.pdf$', '.pdf', name, flags=re.I)
    s = re.sub(r'\.\d{8}-\d{6}\.pdf$', '.pdf', s, flags=re.I)
    return s


def statement_period_from_name(filename: str) -> tuple[int, str] | None:
    match = STATEMENT_PERIOD_RE.search(filename.upper())
    if not match:
        return None
    month = MONTH_TOKEN_TO_NUMBER.get(match.group(1))
    if not month:
        return None
    return int(match.group(2)), MONTH_TOKENS[month]


def strip_lfty_prefix_segments(path: Path) -> Path:
    rebuilt = Path(path.anchor) if path.is_absolute() else Path()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        stripped = LFTY_PREFIX_RE.sub("", part).strip()
        if stripped and stripped != part:
            rebuilt = rebuilt / stripped
            continue
        rebuilt = rebuilt / part
    return rebuilt


def prefer_existing_public_sibling_destination(base: Path, destination: Path) -> Path:
    try:
        parts = list(destination.relative_to(base).parts)
    except ValueError:
        return destination
    if "Public" not in parts:
        return destination
    public_index = parts.index("Public")
    if public_index == 0:
        return destination
    property_parts = parts[:public_index]
    public_sibling = base.joinpath(*property_parts[:-1], f"{property_parts[-1]} Public")
    if not public_sibling.exists():
        return destination
    return public_sibling.joinpath(*parts[public_index + 1 :])


def resolve_dest(base: Path, filename: str, year: int):
    up = filename.upper()
    for key, rel in DEST_RULES:
        if key in up:
            destination = strip_lfty_prefix_segments(base / rel.format(year=year))
            return prefer_existing_public_sibling_destination(base, destination)
    return None


def destination_rule_property_root(rel: str) -> Path:
    formatted = rel.format(year=2000)
    for marker in (
        "/Public/07 - P&L & Owner Statements/Bank Statements/",
        "/07 - P&L & Owner Statements/Bank Statements/",
    ):
        if marker in formatted:
            return Path(formatted.split(marker, 1)[0])
    return Path(formatted)


def destination_rule_validation_enabled(real_estate_root: Path) -> bool:
    parts = {part.lower() for part in real_estate_root.parts}
    return "dropbox" in parts and real_estate_root.exists()


def validate_destination_rules(real_estate_root: Path) -> list[dict[str, Any]]:
    issues = []
    seen: set[Path] = set()
    for key, rel in DEST_RULES:
        property_rel = strip_lfty_prefix_segments(destination_rule_property_root(rel))
        if property_rel in seen:
            continue
        seen.add(property_rel)
        property_root = real_estate_root / property_rel
        if not property_root.exists():
            issues.append(
                {
                    "type": "destination-rule-property-root-missing",
                    "severity": "high",
                    "message": "Baselane statement destination rule points to a missing property root",
                    "key": key,
                    "rule": rel,
                    "property_root": str(property_root),
                    "path": str(property_root),
                }
            )
        if has_legacy_lfty_segment(property_rel):
            issues.append(
                {
                    "type": "destination-rule-legacy-lfty-segment",
                    "severity": "high",
                    "message": "Baselane statement destination rule contains an LFTY-prefixed path segment",
                    "key": key,
                    "rule": rel,
                    "property_root": str(property_root),
                    "path": str(property_root),
                }
            )
        if "Financials" in property_rel.parts:
            issues.append(
                {
                    "type": "destination-rule-old-financials-segment",
                    "severity": "high",
                    "message": "Baselane statement destination rule points at legacy Financials",
                    "key": key,
                    "rule": rel,
                    "property_root": str(property_root),
                    "path": str(property_root),
                }
            )
    return issues


def iter_child_dirs(path: Path):
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_dir():
                        yield Path(entry.path)
                except OSError:
                    continue
    except OSError:
        return


def path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def is_statement_property_root(path: Path) -> bool:
    return any(
        (path / marker).exists()
        for marker in (
            "Public",
            "07 - P&L & Owner Statements",
            LEGACY_FINANCIALS_DIR,
            "Legal",
            "Operations",
        )
    )


def iter_property_roots(root: Path):
    if not root.exists():
        return
    seen: set[str] = set()

    def yield_once(candidate: Path):
        key = path_key(candidate)
        if key in seen:
            return
        seen.add(key)
        yield candidate

    def walk_grouping_dir(grouping_dir: Path, depth: int):
        for child in iter_child_dirs(grouping_dir):
            if is_statement_property_root(child) or has_legacy_lfty_segment(child, root):
                yield from yield_once(child)
                continue
            if depth > 0:
                yield from walk_grouping_dir(child, depth - 1)

    for state_dir in iter_child_dirs(root):
        yield from yield_once(state_dir)
        yield from walk_grouping_dir(state_dir, 2)


def known_statement_dirs(base: Path) -> list[Path]:
    return [
        base / "Public" / "07 - P&L & Owner Statements" / BANK_STATEMENTS_DIR,
        base / "Public" / LEGACY_FINANCIALS_DIR / BANK_STATEMENTS_DIR,
        base / "07 - P&L & Owner Statements" / BANK_STATEMENTS_DIR,
        base / LEGACY_FINANCIALS_DIR / BANK_STATEMENTS_DIR,
        base / "Legal" / BANK_STATEMENTS_DIR,
        base / BANK_STATEMENTS_DIR,
    ]


def iter_nested_statement_roots(property_root: Path):
    for container in (property_root, property_root / "Legal"):
        if not container.exists() or not container.is_dir():
            continue
        for child in iter_child_dirs(container):
            if "LFTY" not in child.name.upper():
                continue
            yield child


def iter_statement_scan_dirs(root: Path):
    if not root.exists():
        return
    seen: set[str] = set()
    for property_root in iter_property_roots(root):
        candidates = list(known_statement_dirs(property_root))
        for lfty_root in iter_nested_statement_roots(property_root):
            candidates.extend(known_statement_dirs(lfty_root))
        captured = property_root / "Captured"
        if captured.is_dir():
            candidates.append(captured)
        for candidate in candidates:
            if not candidate.is_dir():
                continue
            key = path_key(candidate)
            if key in seen:
                continue
            seen.add(key)
            yield candidate


def iter_files_under_statement_dir(statement_dir: Path):
    for dirpath, dirnames, filenames in os.walk(statement_dir):
        dirnames[:] = [
            dirname
            for dirname in dirnames
            if dirname not in {"node_modules", ".git", ".dropbox.cache", "__pycache__"}
        ]
        base = Path(dirpath)
        for filename in filenames:
            yield base / filename


def iter_matching_statement_files(root: Path, month_tok: str, year: int):
    for statement_dir in iter_statement_scan_dirs(root):
        for path in iter_files_under_statement_dir(statement_dir):
            if statement_file_matches_period(path, month_tok, year):
                yield path


def matching_statement_files(files: list[Path], month_tok: str, year: int) -> list[Path]:
    return sorted(path for path in files if statement_file_matches_period(path, month_tok, year))


def is_statement_artifact(path: Path) -> bool:
    up = path.name.upper()
    return path.name.lower().endswith(STATEMENT_ARTIFACT_SUFFIXES) and "BASELANE_" in up and "_STATEMENT" in up


def statement_file_matches_period(path: Path, month_tok: str, year: int) -> bool:
    if not is_statement_artifact(path):
        return False
    period = statement_period_from_name(path.name)
    return period == (year, month_tok)


def iter_statement_files(root: Path):
    for statement_dir in iter_statement_scan_dirs(root):
        for path in iter_files_under_statement_dir(statement_dir):
            if is_statement_artifact(path):
                yield path


def collect_statement_scan(root: Path) -> tuple[list[Path], list[Path], list[Path]]:
    if not root.exists():
        return [], [], []
    statement_dirs = list(iter_statement_scan_dirs(root))
    all_files = []
    statement_files = []
    for statement_dir in statement_dirs:
        for path in iter_files_under_statement_dir(statement_dir):
            all_files.append(path)
            if is_statement_artifact(path):
                statement_files.append(path)
    return statement_dirs, all_files, statement_files


def list_matching_files(root: Path, month_tok: str, year: int):
    return sorted(iter_matching_statement_files(root, month_tok, year))


def has_lfty_prefix_segment(path: Path, base: Path | None = None) -> bool:
    try:
        parts = path.relative_to(base).parts if base else path.parts
    except ValueError:
        parts = path.parts
    return any(LFTY_PREFIX_RE.match(part) for part in parts)


def has_legacy_lfty_segment(path: Path, base: Path | None = None) -> bool:
    try:
        parts = path.relative_to(base).parts if base else path.parts
    except ValueError:
        parts = path.parts
    return any(LFTY_PREFIX_RE.match(part) or LFTY_SEGMENT_RE.search(part) for part in parts)


def has_legacy_lfty_folder(path: Path) -> bool:
    return has_legacy_lfty_segment(path)


def legacy_lfty_statement_files(
    root: Path,
    month_tok: str | None = None,
    year: int | None = None,
    files: list[Path] | None = None,
    *,
    all_statement_months: bool = False,
) -> list[Path]:
    if files is not None:
        candidates = files
    elif all_statement_months:
        candidates = list(iter_statement_files(root))
    elif month_tok is not None and year is not None:
        candidates = list(iter_matching_statement_files(root, month_tok, year))
    else:
        candidates = []
    return sorted(
        p
        for p in candidates
        if "BASELANE_" in p.name.upper() and has_legacy_lfty_segment(p.parent, root)
    )


def statement_target_year(source: Path, fallback_year: int) -> int:
    period = statement_period_from_name(source.name)
    if not period:
        return fallback_year
    return period[0]


def resolve_legacy_lfty_statement_target(re_base: Path, source: Path, year: int) -> Path | None:
    dest_dir = resolve_dest(re_base, source.name, statement_target_year(source, year))
    if dest_dir:
        try:
            if source.parent.resolve() != dest_dir.resolve():
                return dest_dir / source.name
        except OSError:
            return dest_dir / source.name
    stripped = strip_lfty_prefix_segments(source)
    if stripped != source:
        old_financials_target = resolve_old_financials_path_target(re_base, stripped, year)
        return old_financials_target or stripped
    return None


def remove_empty_legacy_parents(start: Path, stop: Path) -> list[Path]:
    removed = []
    cur = start
    stop = stop.resolve()
    while True:
        try:
            resolved = cur.resolve()
            resolved.relative_to(stop)
        except ValueError:
            break
        if resolved == stop:
            break
        if not has_lfty_prefix_segment(cur, stop) and cur.name not in {
            "Bank Statements",
            "07 - P&L & Owner Statements",
            "Public",
        } and not cur.name.isdigit():
            break
        try:
            cur.rmdir()
        except OSError:
            break
        removed.append(cur)
        cur = cur.parent
    return removed


def empty_legacy_statement_dirs(root: Path) -> list[Path]:
    if not root.exists():
        return []
    candidates: set[Path] = set()
    for state_dir in root.iterdir():
        if not state_dir.is_dir():
            continue
        for legacy_root in state_dir.glob("LFTY*"):
            if not legacy_root.is_dir():
                continue
            try:
                if not any(legacy_root.iterdir()):
                    candidates.add(legacy_root)
            except OSError:
                continue
            for bank_dir in legacy_root.rglob("Bank Statements"):
                if not bank_dir.is_dir():
                    continue
                statement_dirs = [bank_dir, *(path for path in bank_dir.rglob("*") if path.is_dir())]
                for statement_dir in statement_dirs:
                    try:
                        if not any(statement_dir.iterdir()):
                            candidates.add(statement_dir)
                    except OSError:
                        continue
    return sorted(candidates, key=lambda path: (len(path.parts), str(path)), reverse=True)


def planned_empty_legacy_statement_dirs(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": str(path),
            "action": "delete-empty-legacy-lfty-dir",
            "bucket": "legacy-empty-dir",
        }
        for path in empty_legacy_statement_dirs(root)
    ]


def remove_empty_legacy_statement_dirs(root: Path) -> list[Path]:
    removed: list[Path] = []
    seen: set[Path] = set()
    for candidate in empty_legacy_statement_dirs(root):
        if not candidate.exists():
            continue
        for path in remove_empty_legacy_parents(candidate, root):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            removed.append(path)
    return removed


def old_financials_statement_files(
    root: Path,
    month_tok: str | None = None,
    year: int | None = None,
    files: list[Path] | None = None,
    *,
    all_statement_months: bool = False,
) -> list[Path]:
    if files is not None:
        candidates = files
    elif all_statement_months:
        candidates = list(iter_statement_files(root))
    elif month_tok is not None and year is not None:
        candidates = list(iter_matching_statement_files(root, month_tok, year))
    else:
        candidates = []
    return sorted(
        p
        for p in candidates
        if "BASELANE_" in p.name.upper() and "Financials" in p.parts and "Bank Statements" in p.parts
        and not has_legacy_lfty_segment(p.parent, root)
    )


def old_financials_bank_statement_files(root: Path, files: list[Path] | None = None) -> list[Path]:
    if not root.exists() and files is None:
        return []
    if files is not None:
        return sorted(
            path
            for path in files
            if "Financials" in path.parts and "Bank Statements" in path.parts and not is_statement_artifact(path)
        )
    files: list[Path] = []
    for statement_dir in iter_statement_scan_dirs(root):
        if "Financials" not in statement_dir.parts or "Bank Statements" not in statement_dir.parts:
            continue
        for path in iter_files_under_statement_dir(statement_dir):
            if is_statement_artifact(path):
                continue
            files.append(path)
    return sorted(files)


def resolve_old_financials_statement_target(re_base: Path, source: Path, year: int) -> Path | None:
    dest_dir = resolve_dest(re_base, source.name, statement_target_year(source, year))
    if not dest_dir:
        return resolve_old_financials_path_target(re_base, source, year)
    try:
        if source.parent.resolve() == dest_dir.resolve():
            return None
    except OSError:
        pass
    return dest_dir / source.name


def resolve_old_financials_bank_file_target(re_base: Path, source: Path) -> Path | None:
    try:
        parts = list(source.relative_to(re_base).parts)
    except ValueError:
        return None
    for index, part in enumerate(parts[:-2]):
        if part != "Financials" or parts[index + 1] != "Bank Statements":
            continue
        prefix = parts[:index]
        suffix = parts[index + 2 :]
        if not prefix or not suffix:
            return None
        target = re_base.joinpath(
            *prefix,
            "07 - P&L & Owner Statements",
            "Bank Statements",
            *suffix,
        )
        try:
            if source.resolve() == target.resolve():
                return None
        except OSError:
            pass
        return target
    return None


def resolve_old_financials_path_target(re_base: Path, source: Path, year: int) -> Path | None:
    try:
        parts = list(source.relative_to(re_base).parts)
    except ValueError:
        return None
    for index, part in enumerate(parts[:-2]):
        if part != "Financials" or parts[index + 1] != "Bank Statements":
            continue
        prefix = parts[:index]
        if not prefix:
            return None
        target_year = str(statement_target_year(source, year))
        target_dir = re_base.joinpath(
            *prefix,
            "07 - P&L & Owner Statements",
            "Bank Statements",
            target_year,
        )
        try:
            if source.parent.resolve() == target_dir.resolve():
                return None
        except OSError:
            pass
        return target_dir / source.name
    return None


def remove_empty_old_financials_parents(start: Path, stop: Path) -> list[Path]:
    removed = []
    cur = start
    stop = stop.resolve()
    allowed_names = {"Financials", "Bank Statements", "Baselane"}
    while True:
        try:
            resolved = cur.resolve()
            resolved.relative_to(stop)
        except ValueError:
            break
        if resolved == stop:
            break
        if cur.name not in allowed_names and not cur.name.isdigit():
            break
        try:
            cur.rmdir()
        except OSError:
            break
        removed.append(cur)
        cur = cur.parent
    return removed


def empty_old_financials_bank_statement_dirs(root: Path, statement_dirs: list[Path] | None = None) -> list[Path]:
    if not root.exists() and statement_dirs is None:
        return []
    candidates: set[Path] = set()
    scan_dirs = statement_dirs if statement_dirs is not None else list(iter_statement_scan_dirs(root))
    for bank_dir in scan_dirs:
        if "Financials" not in bank_dir.parts or "Bank Statements" not in bank_dir.parts:
            continue
        try:
            for path in bank_dir.rglob("*"):
                if path.is_dir():
                    candidates.add(path)
            candidates.add(bank_dir)
            if bank_dir.parent.name == LEGACY_FINANCIALS_DIR:
                candidates.add(bank_dir.parent)
        except OSError:
            continue
    empty_dirs = []
    for path in candidates:
        try:
            if path.exists() and path.is_dir() and not any(path.iterdir()):
                empty_dirs.append(path)
        except OSError:
            continue
    return sorted(empty_dirs, key=lambda path: (len(path.parts), str(path)), reverse=True)


def planned_empty_old_financials_bank_statement_dirs(root: Path, statement_dirs: list[Path] | None = None) -> list[dict[str, Any]]:
    return [
        {
            "path": str(path),
            "action": "delete-empty-old-financials-bank-dir",
            "bucket": "old-financials-empty-dir",
        }
        for path in empty_old_financials_bank_statement_dirs(root, statement_dirs)
    ]


def remove_empty_old_financials_bank_statement_dirs(root: Path) -> list[Path]:
    removed: list[Path] = []
    seen: set[Path] = set()
    while True:
        candidates = empty_old_financials_bank_statement_dirs(root)
        if not candidates:
            break
        removed_this_pass = False
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                candidate.rmdir()
            except OSError:
                continue
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                removed.append(candidate)
            removed_this_pass = True
            parent = candidate.parent
            while parent.name == LEGACY_FINANCIALS_DIR:
                try:
                    parent.rmdir()
                except OSError:
                    break
                resolved_parent = parent.resolve()
                if resolved_parent not in seen:
                    seen.add(resolved_parent)
                    removed.append(parent)
                parent = parent.parent
        if not removed_this_pass:
            break
    return removed


def move_statement_file(source: Path, target: Path, duplicate_action: str, move_action: str, suffix_action: str) -> tuple[Path, str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    action = move_action
    if target.exists():
        if target.stat().st_size == source.stat().st_size:
            source.unlink()
            return target, duplicate_action
        ts = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
        target = target.parent / f'{target.stem}.{ts}{target.suffix}'
        shutil.move(str(source), str(target))
        return target, suffix_action
    shutil.move(str(source), str(target))
    return target, action


def move_legacy_lfty_statements(
    re_base: Path,
    month_tok: str,
    year: int,
    *,
    all_statement_months: bool = True,
):
    moved = []
    removed_dirs = []
    skipped = []
    for source in legacy_lfty_statement_files(
        re_base,
        month_tok,
        year,
        all_statement_months=all_statement_months,
    ):
        target = resolve_legacy_lfty_statement_target(re_base, source, year)
        if not target or target == source:
            skipped.append({"source": str(source), "reason": "target-same-as-source"})
            continue
        source_parent = source.parent
        target, action = move_statement_file(
            source,
            target,
            "delete-duplicate-legacy-source",
            "move-legacy-lfty-statement",
            "move-legacy-lfty-statement-with-timestamp-suffix",
        )
        removed = remove_empty_legacy_parents(source_parent, re_base)
        removed_dirs.extend(removed)
        moved.append(
            {
                "source": str(source),
                "target": str(target),
                "destination_dir": str(target.parent),
                "name": target.name,
                "action": action,
                "removed_empty_dirs": [str(p) for p in removed],
            }
        )
    return moved, removed_dirs, skipped


def migrate_legacy_lfty_statement_folders(root: Path) -> tuple[list[Path], list[Path]]:
    moved, removed_dirs, _ = move_legacy_lfty_statements(
        root,
        "JAN",
        2000,
        all_statement_months=True,
    )
    return [Path(item["target"]) for item in moved], removed_dirs


def move_old_financials_statements(
    re_base: Path,
    month_tok: str,
    year: int,
    *,
    all_statement_months: bool = True,
):
    moved = []
    removed_dirs = []
    skipped = []
    for source in old_financials_statement_files(
        re_base,
        month_tok,
        year,
        all_statement_months=all_statement_months,
    ):
        target = resolve_old_financials_statement_target(re_base, source, year)
        if not target or target == source:
            skipped.append({"source": str(source), "reason": "target-same-or-unresolved"})
            continue
        source_parent = source.parent
        target, action = move_statement_file(
            source,
            target,
            "delete-duplicate-old-financials-source",
            "move-old-financials-statement",
            "move-old-financials-statement-with-timestamp-suffix",
        )
        removed = remove_empty_old_financials_parents(source_parent, re_base)
        removed_dirs.extend(removed)
        moved.append(
            {
                "source": str(source),
                "target": str(target),
                "destination_dir": str(target.parent),
                "name": target.name,
                "action": action,
                "removed_empty_dirs": [str(p) for p in removed],
            }
        )
    return moved, removed_dirs, skipped


def move_old_financials_bank_files(re_base: Path):
    moved = []
    removed_dirs = []
    skipped = []
    for source in old_financials_bank_statement_files(re_base):
        target = resolve_old_financials_bank_file_target(re_base, source)
        if not target or target == source:
            skipped.append({"source": str(source), "reason": "target-same-or-unresolved"})
            continue
        source_parent = source.parent
        target, action = move_statement_file(
            source,
            target,
            "delete-duplicate-old-financials-bank-file-source",
            "move-old-financials-bank-file",
            "move-old-financials-bank-file-with-timestamp-suffix",
        )
        removed = remove_empty_old_financials_parents(source_parent, re_base)
        removed_dirs.extend(removed)
        moved.append(
            {
                "source": str(source),
                "target": str(target),
                "destination_dir": str(target.parent),
                "name": target.name,
                "action": action,
                "removed_empty_dirs": [str(p) for p in removed],
            }
        )
    return moved, removed_dirs, skipped


def move_downloads(downloads: Path, re_base: Path, month_tok: str, year: int):
    moved = []
    unmapped = []
    for p in downloads.glob(f'BASELANE_*DAO LLC*_{month_tok}_{year}_STATEMENT*.pdf'):
        dest_dir = resolve_dest(re_base, p.name, year)
        if not dest_dir:
            unmapped.append(p)
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / p.name
        if target.exists():
            if target.stat().st_size == p.stat().st_size:
                p.unlink()
                moved.append(target)
                continue
            ts = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
            target = dest_dir / f'{p.stem}.{ts}.pdf'
        shutil.move(str(p), str(target))
        moved.append(target)
    return moved, unmapped


def move_non_property(downloads: Path, month_tok: str, year: int, personal_base: Path, holdings_base: Path):
    personal = []
    holdings = []
    for p in downloads.glob(f'BASELANE_*_{month_tok}_{year}_STATEMENT*.pdf'):
        up = p.name.upper()
        if 'DAO LLC' in up:
            continue
        if any(x in up for x in ['EVCO ', 'NARWALL ']):
            dest_dir = holdings_base / str(year)
            dest_dir.mkdir(parents=True, exist_ok=True)
            target = dest_dir / p.name
            if target.exists():
                if target.stat().st_size == p.stat().st_size:
                    p.unlink()
                    holdings.append(target)
                    continue
                ts = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
                target = dest_dir / f'{p.stem}.{ts}.pdf'
            shutil.move(str(p), str(target))
            holdings.append(target)
            continue
        if any(x in up for x in ['EARL VANZE CO', 'EARLDAO', 'ECO SYSTEMS, LLC']):
            dest_dir = personal_base / str(year)
            dest_dir.mkdir(parents=True, exist_ok=True)
            target = dest_dir / p.name
            if target.exists():
                if target.stat().st_size == p.stat().st_size:
                    p.unlink()
                    personal.append(target)
                    continue
                ts = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
                target = dest_dir / f'{p.stem}.{ts}.pdf'
            shutil.move(str(p), str(target))
            personal.append(target)
    return personal, holdings


def planned_property_downloads(downloads: Path, re_base: Path, month_tok: str, year: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    planned = []
    unmapped = []
    for p in downloads.glob(f'BASELANE_*DAO LLC*_{month_tok}_{year}_STATEMENT*.pdf'):
        dest_dir = resolve_dest(re_base, p.name, year)
        if not dest_dir:
            unmapped.append({"source": str(p), "name": p.name, "reason": "no-destination-rule"})
            continue
        target = dest_dir / p.name
        action = "move"
        target_exists = target.exists()
        same_size = False
        if target_exists:
            same_size = target.stat().st_size == p.stat().st_size
            action = "delete-duplicate-source" if same_size else "move-with-timestamp-suffix"
        planned.append(
            {
                "source": str(p),
                "target": str(target),
                "destination_dir": str(dest_dir),
                "name": p.name,
                "action": action,
                "target_exists": target_exists,
                "target_same_size": same_size,
                "source_size": p.stat().st_size,
                "target_size": target.stat().st_size if target_exists else None,
            }
        )
    return planned, unmapped


def planned_legacy_lfty_statement_moves(
    re_base: Path,
    month_tok: str,
    year: int,
    files: list[Path] | None = None,
    *,
    all_statement_months: bool = True,
) -> list[dict[str, Any]]:
    planned = []
    for source in legacy_lfty_statement_files(
        re_base,
        month_tok,
        year,
        files,
        all_statement_months=all_statement_months,
    ):
        target = resolve_legacy_lfty_statement_target(re_base, source, year)
        if not target or target == source:
            continue
        target_exists = target.exists()
        same_size = target_exists and target.stat().st_size == source.stat().st_size
        action = "delete-duplicate-legacy-source" if same_size else (
            "move-legacy-lfty-statement-with-timestamp-suffix" if target_exists else "move-legacy-lfty-statement"
        )
        planned.append(
            {
                "source": str(source),
                "target": str(target),
                "destination_dir": str(target.parent),
                "name": source.name,
                "action": action,
                "target_exists": target_exists,
                "target_same_size": same_size,
                "source_size": source.stat().st_size,
                "target_size": target.stat().st_size if target_exists else None,
            }
        )
    return planned


def planned_old_financials_statement_moves(
    re_base: Path,
    month_tok: str,
    year: int,
    files: list[Path] | None = None,
    *,
    all_statement_months: bool = True,
) -> list[dict[str, Any]]:
    planned = []
    for source in old_financials_statement_files(
        re_base,
        month_tok,
        year,
        files,
        all_statement_months=all_statement_months,
    ):
        target = resolve_old_financials_statement_target(re_base, source, year)
        if not target or target == source:
            continue
        target_exists = target.exists()
        same_size = target_exists and target.stat().st_size == source.stat().st_size
        action = "delete-duplicate-old-financials-source" if same_size else (
            "move-old-financials-statement-with-timestamp-suffix" if target_exists else "move-old-financials-statement"
        )
        planned.append(
            {
                "source": str(source),
                "target": str(target),
                "destination_dir": str(target.parent),
                "name": source.name,
                "action": action,
                "target_exists": target_exists,
                "target_same_size": same_size,
                "source_size": source.stat().st_size,
                "target_size": target.stat().st_size if target_exists else None,
                "bucket": "old-financials-property",
            }
        )
    return planned


def planned_old_financials_bank_file_moves(re_base: Path, files: list[Path] | None = None) -> list[dict[str, Any]]:
    planned = []
    for source in old_financials_bank_statement_files(re_base, files):
        target = resolve_old_financials_bank_file_target(re_base, source)
        if not target or target == source:
            continue
        target_exists = target.exists()
        same_size = target_exists and target.stat().st_size == source.stat().st_size
        action = "delete-duplicate-old-financials-bank-file-source" if same_size else (
            "move-old-financials-bank-file-with-timestamp-suffix" if target_exists else "move-old-financials-bank-file"
        )
        planned.append(
            {
                "source": str(source),
                "target": str(target),
                "destination_dir": str(target.parent),
                "name": source.name,
                "action": action,
                "target_exists": target_exists,
                "target_same_size": same_size,
                "source_size": source.stat().st_size,
                "target_size": target.stat().st_size if target_exists else None,
                "bucket": "old-financials-bank-file",
            }
        )
    return planned


def planned_non_property_downloads(
    downloads: Path,
    month_tok: str,
    year: int,
    personal_base: Path,
    holdings_base: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    personal = []
    holdings = []
    for p in downloads.glob(f'BASELANE_*_{month_tok}_{year}_STATEMENT*.pdf'):
        up = p.name.upper()
        if 'DAO LLC' in up:
            continue
        bucket = None
        dest_base = None
        if any(x in up for x in ['EVCO ', 'NARWALL ']):
            bucket = "holdings"
            dest_base = holdings_base
        elif any(x in up for x in ['EARL VANZE CO', 'EARLDAO', 'ECO SYSTEMS, LLC']):
            bucket = "personal"
            dest_base = personal_base
        if bucket is None or dest_base is None:
            continue

        dest_dir = dest_base / str(year)
        target = dest_dir / p.name
        target_exists = target.exists()
        same_size = target_exists and target.stat().st_size == p.stat().st_size
        action = "delete-duplicate-source" if same_size else ("move-with-timestamp-suffix" if target_exists else "move")
        record = {
            "source": str(p),
            "target": str(target),
            "destination_dir": str(dest_dir),
            "name": p.name,
            "bucket": bucket,
            "action": action,
            "target_exists": target_exists,
            "target_same_size": same_size,
            "source_size": p.stat().st_size,
            "target_size": target.stat().st_size if target_exists else None,
        }
        if bucket == "holdings":
            holdings.append(record)
        else:
            personal.append(record)
    return personal, holdings


def read_manifest(path: Path):
    if not path or not path.exists():
        return None
    lines = [x.strip() for x in path.read_text(encoding='utf-8').splitlines() if x.strip() and not x.strip().startswith('#')]
    return set(lines)


def review_command(args: argparse.Namespace) -> str:
    parts = [
        "python3",
        str(SCRIPT_PATH),
        "--year",
        str(args.year),
        "--month",
        str(args.month),
        "--downloads",
        str(args.downloads),
        "--real-estate",
        str(args.real_estate),
        "--personal",
        str(args.personal),
        "--holdings",
        str(args.holdings),
    ]
    if args.manifest:
        parts.extend(["--manifest", str(args.manifest)])
    if args.target_month_cleanup_only:
        parts.append("--target-month-cleanup-only")
    parts.append("--json")
    return " ".join(shlex.quote(part) for part in parts)


def review_command_validation(command_text: str, args: argparse.Namespace) -> dict[str, Any]:
    validation: dict[str, Any] = {
        "command": command_text,
        "valid": False,
        "issues": [],
        "parts": [],
        "script_path": str(SCRIPT_PATH),
        "script_exists": SCRIPT_PATH.exists(),
        "script_is_file": SCRIPT_PATH.is_file(),
    }
    try:
        parts = shlex.split(command_text)
    except ValueError as exc:
        validation["issues"].append(f"parse-error:{exc}")
        return validation

    validation["parts"] = parts
    expected_pairs = {
        "--year": str(args.year),
        "--month": str(args.month),
        "--downloads": str(args.downloads),
        "--real-estate": str(args.real_estate),
        "--personal": str(args.personal),
        "--holdings": str(args.holdings),
    }
    if args.manifest:
        expected_pairs["--manifest"] = str(args.manifest)

    expected_len = 2 + (len(expected_pairs) * 2) + 1
    if args.target_month_cleanup_only:
        expected_len += 1
    if len(parts) != expected_len:
        validation["issues"].append("unexpected-argument-count")
    if not parts or parts[0] != "python3":
        validation["issues"].append("missing-python3")
    if len(parts) < 2 or Path(parts[1]).resolve() != SCRIPT_PATH:
        validation["issues"].append("unexpected-script-path")
    for flag, expected in expected_pairs.items():
        if flag not in parts:
            validation["issues"].append(f"missing-{flag.lstrip('-')}-flag")
            continue
        idx = parts.index(flag)
        if idx + 1 >= len(parts) or parts[idx + 1] != expected:
            validation["issues"].append(f"unexpected-{flag.lstrip('-')}")
    if "--json" not in parts:
        validation["issues"].append("missing-json-flag")
    if args.target_month_cleanup_only and "--target-month-cleanup-only" not in parts:
        validation["issues"].append("missing-target-month-cleanup-only-flag")
    if "--apply" in parts:
        validation["issues"].append("unexpected-apply-flag")
    if not SCRIPT_PATH.exists():
        validation["issues"].append("script-missing")
    elif not SCRIPT_PATH.is_file():
        validation["issues"].append("script-not-file")

    validation["valid"] = not validation["issues"]
    return validation


def remediation_fields(ok_state: bool, args: argparse.Namespace) -> dict[str, Any]:
    command = review_command(args)
    validation = review_command_validation(command, args)
    return {
        "remediation_class": "no-remediation-needed" if ok_state else "operator-reviewed-baselane-statements",
        "requires_operator_approval": not ok_state,
        "requires_interactive_sudo": False,
        "requires_interactive_oauth": False,
        "safe_to_run_automatically": ok_state,
        "review_command": None if ok_state else command,
        "review_command_safe_to_run_automatically": not ok_state,
        "review_command_valid": None if ok_state else validation["valid"],
        "review_command_validation": None if ok_state else validation,
        "cleanup_command_after_review": None,
        "restart_command_after_review": None,
        "oauth_command_after_review": None,
        "helper_command_after_review": None,
        "remediation": {
            "command": None,
            "review_command": None if ok_state else command,
            "review_command_validation": None if ok_state else validation,
        },
    }


def classified_issue_records(report: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    fields = remediation_fields(False, args)
    records = []

    for bucket, severity in [
        ("planned_property_moves", "medium"),
        ("planned_personal_moves", "medium"),
        ("planned_holdings_moves", "medium"),
        ("planned_legacy_lfty_moves", "high"),
        ("planned_old_financials_moves", "high"),
        ("planned_old_financials_bank_file_moves", "high"),
        ("planned_empty_legacy_dirs", "high"),
        ("planned_empty_old_financials_bank_dirs", "high"),
    ]:
        for item in report.get(bucket, []):
            records.append(
                {
                    "class": ISSUE_CLASS,
                    "issue_class": ISSUE_CLASS,
                    "classification": report["classification"],
                    "route_classification": report["classification"],
                    "type": item.get("action"),
                    "severity": severity,
                    "source": item.get("source"),
                    "target": item.get("target"),
                    "path": item.get("path"),
                    "bucket": item.get("bucket") or (
                        "legacy-property" if bucket == "planned_legacy_lfty_moves" else "property"
                    ),
                    "year": report["year"],
                    "month": report["month"],
                    **fields,
                }
            )
    for item in report.get("unmapped", []):
        records.append(
            {
                "class": ISSUE_CLASS,
                "issue_class": ISSUE_CLASS,
                "classification": report["classification"],
                "route_classification": report["classification"],
                "type": "unmapped-statement",
                "severity": "high",
                "source": item.get("source"),
                "name": item.get("name"),
                "year": report["year"],
                "month": report["month"],
                **fields,
            }
        )
    for missing in report.get("manifest_missing", []):
        records.append(
            {
                "class": ISSUE_CLASS,
                "issue_class": ISSUE_CLASS,
                "classification": report["classification"],
                "route_classification": report["classification"],
                "type": "manifest-missing",
                "severity": "high",
                "filename": missing,
                "year": report["year"],
                "month": report["month"],
                **fields,
            }
        )
    for issue in report.get("input_issues", []):
        records.append(
            {
                "class": ISSUE_CLASS,
                "issue_class": ISSUE_CLASS,
                "classification": report["classification"],
                "route_classification": report["classification"],
                "type": issue.get("type"),
                "severity": issue.get("severity", "high"),
                "message": issue.get("message"),
                "path": issue.get("path"),
                "year": report["year"],
                "month": report["month"],
                **fields,
            }
        )
    return records


def classified_issue_summary(report: dict[str, Any]) -> dict[str, Any]:
    issues = report.get("classified_issues", [])
    class_counts: dict[str, int] = {}
    severity_counts: dict[str, int] = {}
    action_counts: dict[str, int] = {}
    valid_count = 0
    invalid_count = 0
    validation_issues: list[str] = []
    for issue in issues:
        cls = str(issue.get("issue_class") or issue.get("class") or ISSUE_CLASS)
        class_counts[cls] = class_counts.get(cls, 0) + 1
        severity = issue.get("severity")
        if severity:
            severity_counts[str(severity)] = severity_counts.get(str(severity), 0) + 1
        typ = issue.get("type")
        if typ:
            action_counts[str(typ)] = action_counts.get(str(typ), 0) + 1
        if issue.get("review_command_safe_to_run_automatically"):
            if issue.get("review_command_valid"):
                valid_count += 1
            else:
                invalid_count += 1
                validation = issue.get("review_command_validation") or {}
                validation_issues.extend(str(item) for item in validation.get("issues", []))
    return {
        "total": len(issues),
        "total_count": len(issues),
        "issue_count": len(issues),
        "classification": report["classification"],
        "classes": sorted(class_counts),
        "class_counts": dict(sorted(class_counts.items())),
        "issue_class_counts": dict(sorted(class_counts.items())),
        "severity_counts": dict(sorted(severity_counts.items())),
        "action_counts": dict(sorted(action_counts.items())),
        "planned_move_count": report["planned_move_count"],
        "unmapped_count": report["unmapped_count"],
        "manifest_missing_count": report["manifest_missing_count"],
        "input_issue_count": len(report.get("input_issues", [])),
        "captured_unique_count": report["captured_unique_count"],
        "review_required_count": len(issues),
        "requires_operator_approval_count": len(issues),
        "requires_interactive_sudo_count": 0,
        "requires_interactive_oauth_count": 0,
        "safe_review_command_count": valid_count + invalid_count,
        "valid_review_command_count": valid_count,
        "invalid_review_command_count": invalid_count,
        "review_command_validation_issues": sorted(set(validation_issues)),
        "move_attempted": report["move_attempted"],
        "delete_attempted": report["delete_attempted"],
        "write_attempted": report["write_attempted"],
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    month_tok = MONTH_TOKENS[args.month]
    input_issues = []
    if not args.real_estate.exists():
        input_issues.append(
            {
                "type": "real-estate-root-missing",
                "severity": "high",
                "message": "Real Estate root does not exist; captured statement scan cannot run",
                "path": str(args.real_estate),
            }
        )
    if args.manifest and not args.manifest.exists():
        input_issues.append(
            {
                "type": "manifest-missing-file",
                "severity": "high",
                "message": "Manifest path was supplied but does not exist",
                "path": str(args.manifest),
            }
        )
    destination_rule_validation_active = destination_rule_validation_enabled(args.real_estate)
    destination_rule_issues = (
        validate_destination_rules(args.real_estate)
        if destination_rule_validation_active
        else []
    )
    input_issues.extend(destination_rule_issues)
    statement_dirs, all_statement_dir_files, all_existing = collect_statement_scan(args.real_estate)
    existing = matching_statement_files(all_existing, month_tok, args.year)
    cleanup_all_statement_months = not args.target_month_cleanup_only
    planned, unmapped = planned_property_downloads(args.downloads, args.real_estate, month_tok, args.year)
    legacy = planned_legacy_lfty_statement_moves(
        args.real_estate,
        month_tok,
        args.year,
        all_existing if cleanup_all_statement_months else existing,
        all_statement_months=False,
    )
    old_financials = planned_old_financials_statement_moves(
        args.real_estate,
        month_tok,
        args.year,
        all_existing if cleanup_all_statement_months else existing,
        all_statement_months=False,
    )
    old_financials_bank_files = (
        planned_old_financials_bank_file_moves(args.real_estate, all_statement_dir_files)
        if cleanup_all_statement_months
        else []
    )
    empty_legacy_dirs = planned_empty_legacy_statement_dirs(args.real_estate)
    empty_old_financials_bank_dirs = planned_empty_old_financials_bank_statement_dirs(args.real_estate, statement_dirs)
    personal, holdings = planned_non_property_downloads(args.downloads, month_tok, args.year, args.personal, args.holdings)
    canon = {normalize_name(p.name) for p in existing}
    manifest = read_manifest(args.manifest) if args.manifest else None
    missing = sorted(manifest - canon) if manifest is not None else []
    planned_move_count = len(planned) + len(personal) + len(holdings) + len(legacy) + len(old_financials) + len(old_financials_bank_files)
    issue_count = planned_move_count + len(empty_legacy_dirs) + len(empty_old_financials_bank_dirs) + len(unmapped) + len(missing) + len(input_issues)
    ok_state = issue_count == 0
    report: dict[str, Any] = {
        "status": "NO_REPLY" if ok_state else "BASELANE_STATEMENTS_OPERATOR_REVIEW",
        "classification": "ok" if ok_state else "baselane-statements-operator-review",
        "ok_state": ok_state,
        "ok": [f"statements ok year={args.year} month={args.month} captured_unique={len(canon)}"] if ok_state else [],
        "visible_ok": [f"statements ok year={args.year} month={args.month} captured_unique={len(canon)}"] if ok_state else [],
        "ok_count": 1 if ok_state else 0,
        "year": args.year,
        "month": args.month,
        "month_token": month_tok,
        "downloads": str(args.downloads),
        "real_estate": str(args.real_estate),
        "personal": str(args.personal),
        "holdings": str(args.holdings),
        "manifest": str(args.manifest) if args.manifest else None,
        "cleanup_all_statement_months": cleanup_all_statement_months,
        "downloads_exists": args.downloads.exists(),
        "real_estate_exists": args.real_estate.exists(),
        "personal_exists": args.personal.exists(),
        "holdings_exists": args.holdings.exists(),
        "manifest_exists": args.manifest.exists() if args.manifest else None,
        "statement_scan_dir_count": len(statement_dirs),
        "statement_scan_file_count": len(all_statement_dir_files),
        "destination_rule_validation_enabled": destination_rule_validation_active,
        "destination_rule_issue_count": len(destination_rule_issues),
        "destination_rule_issues": destination_rule_issues,
        "planned_property_moves": planned,
        "planned_legacy_lfty_moves": legacy,
        "planned_old_financials_moves": old_financials,
        "planned_old_financials_bank_file_moves": old_financials_bank_files,
        "planned_empty_legacy_dirs": empty_legacy_dirs,
        "planned_empty_old_financials_bank_dirs": empty_old_financials_bank_dirs,
        "planned_personal_moves": personal,
        "planned_holdings_moves": holdings,
        "unmapped": unmapped,
        "manifest_missing": missing,
        "manifest_total": len(manifest) if manifest is not None else None,
        "input_issues": input_issues,
        "captured_unique_count": len(canon),
        "planned_move_count": planned_move_count,
        "property_move_count": len(planned),
        "legacy_lfty_move_count": len(legacy),
        "old_financials_move_count": len(old_financials),
        "old_financials_bank_file_move_count": len(old_financials_bank_files),
        "empty_legacy_dir_count": len(empty_legacy_dirs),
        "empty_old_financials_bank_dir_count": len(empty_old_financials_bank_dirs),
        "personal_move_count": len(personal),
        "holdings_move_count": len(holdings),
        "unmapped_count": len(unmapped),
        "manifest_missing_count": len(missing),
        "issue_count": issue_count,
        "advisory_count": 0,
        "review_required_count": issue_count,
        "approval_required_count": issue_count,
        "move_attempted": False,
        "delete_attempted": False,
        "write_attempted": False,
        "issue_classes": [] if ok_state else [ISSUE_CLASS],
    }
    report.update(remediation_fields(ok_state, args))
    report["classified_issues"] = [] if ok_state else classified_issue_records(report, args)
    report["classified_issue_summary"] = classified_issue_summary(report)
    report["safe_review_command_count"] = int(report["classified_issue_summary"]["safe_review_command_count"])
    report["valid_review_command_count"] = int(report["classified_issue_summary"]["valid_review_command_count"])
    report["invalid_review_command_count"] = int(report["classified_issue_summary"]["invalid_review_command_count"])
    report["review_command_validation_issues"] = list(report["classified_issue_summary"]["review_command_validation_issues"])
    return report


def emit_js(path: Path, missing: set[str], year: int, month: int):
    start = dt.date(year, month, 1)
    end_day = calendar.monthrange(year, month)[1]
    month_name = start.strftime('%b')
    period = f'{month_name} 1 - {month_name} {end_day}, {year}'
    arr = ',\n  '.join(repr(x) for x in sorted(missing))
    js = f"""// Paste this in Baselane statements page console.
(async () => {{
  const missing = new Set([\n  {arr}\n  ]);
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const period = {period!r};
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const p = [...document.querySelectorAll('p')].find(x => norm(x.textContent) === period);
  const c = p?.parentElement?.parentElement;
  if (!c) return console.log('Period not found:', period);
  const kids = [...c.children];
  let clicked = 0;
  for (let i = 0; i + 2 < kids.length; i += 3) {{
    const acctDiv = kids[i], perDiv = kids[i+1], btnWrap = kids[i+2];
    const per = norm(perDiv.textContent);
    const ps = [...acctDiv.querySelectorAll('p')].map(p => norm(p.textContent)).filter(Boolean);
    const l1 = (ps[0] || norm(acctDiv.textContent)).toUpperCase();
    const l2 = (ps[1] || '').toUpperCase();
    if (!(l1 + ' ' + l2).includes('DAO LLC') || per !== period) continue;
    const expected = `BASELANE_${{l1}}_${{l2}}_{MONTH}_{YEAR}_STATEMENT.pdf`
      .replace('{MONTH}', '{MONTH}'.replace('{MONTH}', '{MONTH}'));
  }}
}})();
"""
    # Simpler robust template with direct formatting
    js = js.replace('{MONTH}', MONTH_TOKENS[month]).replace('{YEAR}', str(year))
    path.write_text(js, encoding='utf-8')


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description='Baselane statements operator helper (move/status/missing)')
    ap.add_argument('--year', type=int, required=True)
    ap.add_argument('--month', type=int, required=True, choices=range(1, 13), metavar='1-12', help='1-12')
    ap.add_argument('--downloads', type=Path, default=DEFAULT_DOWNLOADS)
    ap.add_argument('--real-estate', type=Path, default=DEFAULT_RE)
    ap.add_argument('--personal', type=Path, default=DEFAULT_PERSONAL)
    ap.add_argument('--holdings', type=Path, default=DEFAULT_HOLDINGS)
    ap.add_argument('--manifest', type=Path, default=None, help='Text file of expected canonical filenames')
    ap.add_argument('--target-month-cleanup-only', action='store_true', help='Only clean legacy statement placements for the requested month/year')
    ap.add_argument('--json', action='store_true', help='Emit read-only dashboard JSON; do not move or delete files')
    return ap.parse_args(argv)


def main(argv: list[str] | None = None, stdout: TextIO | None = None) -> int:
    args = parse_args(argv)
    stdout = stdout or sys.stdout

    month_tok = MONTH_TOKENS[args.month]

    if args.json:
        report = build_report(args)
        json.dump(report, stdout, indent=2, sort_keys=True)
        stdout.write("\n")
        return 0 if report["ok_state"] else 1

    moved, unmapped = move_downloads(args.downloads, args.real_estate, month_tok, args.year)
    cleanup_all_statement_months = not args.target_month_cleanup_only
    legacy, removed_dirs, legacy_skipped = move_legacy_lfty_statements(
        args.real_estate,
        month_tok,
        args.year,
        all_statement_months=cleanup_all_statement_months,
    )
    old_financials, old_financials_removed_dirs, old_financials_skipped = move_old_financials_statements(
        args.real_estate,
        month_tok,
        args.year,
        all_statement_months=cleanup_all_statement_months,
    )
    if cleanup_all_statement_months:
        old_financials_bank_files, old_financials_bank_file_removed_dirs, old_financials_bank_file_skipped = move_old_financials_bank_files(
            args.real_estate,
        )
    else:
        old_financials_bank_files, old_financials_bank_file_removed_dirs, old_financials_bank_file_skipped = [], [], []
    empty_legacy_removed_dirs = remove_empty_legacy_statement_dirs(args.real_estate)
    empty_old_financials_bank_removed_dirs = remove_empty_old_financials_bank_statement_dirs(args.real_estate)
    personal, holdings = move_non_property(args.downloads, month_tok, args.year, args.personal, args.holdings)
    existing = list_matching_files(args.real_estate, month_tok, args.year)
    canon = {normalize_name(p.name) for p in existing}

    all_removed_dirs = removed_dirs + old_financials_removed_dirs + old_financials_bank_file_removed_dirs + empty_legacy_removed_dirs + empty_old_financials_bank_removed_dirs
    print(f'MOVED={len(moved)} LEGACY_LFTY_MOVED={len(legacy)} OLD_FINANCIALS_MOVED={len(old_financials)} OLD_FINANCIALS_BANK_FILES_MOVED={len(old_financials_bank_files)} EMPTY_LEGACY_DIRS_DELETED={len(empty_legacy_removed_dirs)} EMPTY_OLD_FINANCIALS_BANK_DIRS_DELETED={len(empty_old_financials_bank_removed_dirs)} EMPTY_DIRS_DELETED={len(all_removed_dirs)} PERSONAL={len(personal)} HOLDINGS={len(holdings)} UNMAPPED={len(unmapped)} CAPTURED_UNIQUE={len(canon)}', file=stdout)
    for p in moved:
        print(f'OK\t{p}', file=stdout)
    for item in legacy:
        print(f'LEGACY\t{item["target"]}', file=stdout)
    for item in old_financials:
        print(f'OLD_FINANCIALS\t{item["target"]}', file=stdout)
    for item in old_financials_bank_files:
        print(f'OLD_FINANCIALS_BANK_FILE\t{item["target"]}', file=stdout)
    for p in all_removed_dirs:
        print(f'DELETED_EMPTY_DIR\t{p}', file=stdout)
    for item in legacy_skipped:
        print(f'LEGACY_SKIPPED\t{item["source"]}\t{item["reason"]}', file=stdout)
    for item in old_financials_skipped:
        print(f'OLD_FINANCIALS_SKIPPED\t{item["source"]}\t{item["reason"]}', file=stdout)
    for item in old_financials_bank_file_skipped:
        print(f'OLD_FINANCIALS_BANK_FILE_SKIPPED\t{item["source"]}\t{item["reason"]}', file=stdout)
    for p in personal:
        print(f'PERSONAL\t{p}', file=stdout)
    for p in holdings:
        print(f'HOLDINGS\t{p}', file=stdout)
    for p in unmapped:
        print(f'UNMAPPED\t{p.name}', file=stdout)

    manifest = read_manifest(args.manifest) if args.manifest else None
    if manifest is not None:
        missing = sorted(manifest - canon)
        print(f'MANIFEST_TOTAL={len(manifest)} MISSING={len(missing)}', file=stdout)
        for m in missing:
            print(f'MISS\t{m}', file=stdout)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
