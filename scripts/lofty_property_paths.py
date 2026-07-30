from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


LFTY_PREFIX_RE = re.compile(r"^LFTY\d+\s+", re.IGNORECASE)
NESTED_PUBLIC_PREFERENCE_KEYS = frozenset({"724 3rd ave"})


def normalize_property_dir_name(value: object) -> str:
    text = LFTY_PREFIX_RE.sub("", str(value or "")).lower()
    text = text.replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def public_dir_for_property(property_path: Path) -> Path:
    if property_path.name == "Public":
        return property_path
    if property_path.name.endswith(" Public"):
        return property_path
    return property_path / "Public"


def display_name_for_property_path(property_path: Path, metadata: dict[str, Any] | None = None) -> str:
    if property_path.name.lower() == "public":
        return property_path.parent.name
    if not property_path.name.endswith(" Public"):
        return property_path.name
    input_path = Path(str((metadata or {}).get("input_property_path") or ""))
    if input_path.name and not input_path.name.endswith(" Public"):
        return input_path.name
    return property_path.name.removesuffix(" Public")


def public_sibling_match_score(target_key: str, candidate: Path) -> int:
    if not candidate.name.endswith(" Public"):
        return 0
    candidate_key = normalize_property_dir_name(candidate.name.removesuffix(" Public"))
    if not target_key or not candidate_key:
        return 0
    if candidate_key == target_key:
        return 1000 + len(candidate_key)
    if target_key.startswith(candidate_key + " ") or candidate_key in target_key:
        return 500 + len(candidate_key)
    return 0


def canonical_doc_score(property_path: Path) -> int:
    if os.environ.get("LOFTY_SKIP_PROPERTY_SIBLING_RESOLUTION") == "1":
        return 0
    public_dir = public_dir_for_property(property_path)
    score = 0
    if public_dir.is_dir():
        score += 1
    if (public_dir / "00 - README & Property Snapshot" / "UPDATES.md").is_file():
        score += 2
    if (public_dir / "00 - README & Property Snapshot" / "FINANCIALS.md").is_file():
        score += 4
    if (public_dir / "07 - P&L & Owner Statements" / "FINANCIALS.md").is_file():
        score += 4
    if (public_dir / "00 - README & Property Snapshot").is_dir():
        score += 1
    return score


def resolve_for_scan(property_path: Path) -> Path:
    expanded = property_path.expanduser()
    if os.environ.get("LOFTY_SKIP_PROPERTY_SIBLING_RESOLUTION") == "1":
        return expanded if expanded.is_absolute() else Path.cwd() / expanded
    return expanded.resolve()


def resolve_property_path(property_path: Path) -> tuple[Path, dict[str, Any]]:
    resolved = resolve_for_scan(property_path)
    stripped_name = LFTY_PREFIX_RE.sub("", resolved.name).strip()
    metadata: dict[str, Any] = {
        "input_property_path": str(resolved),
        "property_path_resolution": "input",
    }
    if os.environ.get("LOFTY_SKIP_PROPERTY_SIBLING_RESOLUTION") == "1":
        metadata["property_path_resolution"] = "input_sibling_scan_skipped"
        metadata["input_doc_score"] = 0
        return resolved, metadata
    input_score = canonical_doc_score(resolved)
    # Windows cannot preserve the old Public symlink used by some NY folders.
    # Most migrated properties retain the complete flat ``<address> Public``
    # workflow, so retain it unless the nested directory is more complete.
    # 724 3rd Ave is the explicit exception: its canonical workflow is the
    # nested full-address ``Public`` directory.
    if resolved.name.endswith(" Public") and resolved.parent.is_dir():
        flat_public_key = normalize_property_dir_name(resolved.name.removesuffix(" Public"))
        nested_matches: list[tuple[int, Path]] = []
        try:
            sibling_candidates = tuple(resolved.parent.iterdir())
        except OSError as exc:
            metadata.update(
                {
                    "property_path_resolution_warning": "nested_public_sibling_scan_failed",
                    "property_path_resolution_error": f"{exc.__class__.__name__}: {exc}",
                }
            )
            sibling_candidates = ()
        for candidate in sibling_candidates:
            if candidate == resolved or not candidate.is_dir():
                continue
            candidate_key = normalize_property_dir_name(candidate.name)
            if not flat_public_key or not (
                candidate_key == flat_public_key or candidate_key.startswith(flat_public_key + " ")
            ):
                continue
            nested_public = candidate / "Public"
            nested_score = canonical_doc_score(nested_public)
            if nested_score > 0:
                nested_matches.append((nested_score, nested_public))
        if nested_matches:
            nested_matches.sort(key=lambda item: (item[0], item[1].parent.name), reverse=True)
            nested_score, nested_public = nested_matches[0]
            if flat_public_key in NESTED_PUBLIC_PREFERENCE_KEYS or nested_score > input_score:
                metadata.update(
                    {
                        "property_path_resolution": "flat_public_to_nested_public",
                        "resolved_property_path": str(resolve_for_scan(nested_public)),
                        "input_doc_score": input_score,
                        "resolved_doc_score": nested_score,
                    }
                )
                return resolve_for_scan(nested_public), metadata
    if input_score >= 7:
        return resolved, metadata
    if not stripped_name or stripped_name == resolved.name:
        sibling = None
    else:
        sibling = resolved.with_name(stripped_name)
    if sibling and sibling.exists():
        sibling_score = canonical_doc_score(sibling)
        if sibling_score > input_score:
            metadata.update(
                {
                    "property_path_resolution": "lfty_prefix_sibling",
                    "resolved_property_path": str(resolve_for_scan(sibling)),
                    "input_doc_score": input_score,
                    "resolved_doc_score": sibling_score,
                }
            )
            return resolve_for_scan(sibling), metadata

    target_key = normalize_property_dir_name(resolved.name)
    normalized_matches: list[tuple[int, int, Path]] = []
    if os.environ.get("LOFTY_SKIP_PROPERTY_SIBLING_RESOLUTION") == "1":
        metadata["property_path_resolution"] = "input_sibling_scan_skipped"
        metadata["input_doc_score"] = input_score
        return resolved, metadata
    if target_key and resolved.parent.is_dir():
        try:
            sibling_candidates = tuple(resolved.parent.iterdir())
        except OSError as exc:
            metadata.update(
                {
                    "property_path_resolution_warning": "normalized_sibling_scan_failed",
                    "property_path_resolution_error": f"{exc.__class__.__name__}: {exc}",
                }
            )
            sibling_candidates = ()
        for candidate in sibling_candidates:
            if candidate == resolved or not candidate.is_dir():
                continue
            normalized_match = normalize_property_dir_name(candidate.name) == target_key
            public_sibling_score = public_sibling_match_score(target_key, candidate)
            if not normalized_match and not public_sibling_score:
                continue
            candidate_score = canonical_doc_score(candidate)
            if candidate_score <= input_score and not public_sibling_score:
                continue
            unprefixed_bonus = 1 if not LFTY_PREFIX_RE.match(candidate.name) else 0
            normalized_matches.append((candidate_score + public_sibling_score, unprefixed_bonus, candidate))
    if normalized_matches:
        normalized_matches.sort(key=lambda item: (item[0], item[1], item[2].name), reverse=True)
        _, _, match = normalized_matches[0]
        metadata.update(
            {
                "property_path_resolution": "normalized_sibling",
                "resolved_property_path": str(resolve_for_scan(match)),
                "input_doc_score": input_score,
                "resolved_doc_score": canonical_doc_score(match),
            }
        )
        return resolve_for_scan(match), metadata
    return resolved, metadata


def resolve_index_property_path(row: dict[str, str]) -> tuple[Path, dict[str, Any]]:
    return resolve_property_path(Path(row.get("property_path") or ""))
