#!/usr/bin/env python3
"""Shared resolution policy for equivalent property General Ledger exports."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable


class DivergentCanonicalLedgerError(ValueError):
    """Raised when multiple files claim one canonical property ledger."""

    def __init__(self, paths: Iterable[Path]):
        self.paths = tuple(sorted({Path(path) for path in paths}, key=lambda path: str(path).casefold()))
        names = ", ".join(path.name for path in self.paths)
        super().__init__(f"divergent canonical property ledgers: {names}")


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_equivalent_ledgers(paths: Iterable[Path]) -> Path:
    """Resolve duplicate exports only when their contents are identical."""
    candidates = sorted(
        {Path(path) for path in paths if Path(path).is_file()},
        key=lambda path: (len(path.name), path.name.casefold(), str(path).casefold()),
    )
    if not candidates:
        raise FileNotFoundError("no canonical property ledger candidates")
    if len(candidates) == 1:
        return candidates[0]
    if len({file_digest(path) for path in candidates}) != 1:
        raise DivergentCanonicalLedgerError(candidates)
    return candidates[0]


def ledger_property_identity(value: str) -> str:
    """Normalize a ledger filename or property label to its street identity."""
    text = str(value or "").strip().casefold()
    text = re.sub(r"\.csv$", "", text)
    text = re.sub(r"^eco systems general ledger\s*-\s*", "", text)
    text = re.sub(r"\bpublic\b", " ", text)
    text = text.replace("&", " and ")
    replacements = {
        "avenue": "ave",
        "street": "st",
        "road": "rd",
        "lane": "ln",
        "drive": "dr",
        "place": "pl",
        "circle": "cir",
        "court": "ct",
        "boulevard": "blvd",
        "north": "n",
        "south": "s",
        "east": "e",
        "west": "w",
    }
    for source, target in replacements.items():
        text = re.sub(rf"\b{source}\b", target, text)
    street_match = re.match(
        r"^\s*(.*?\b(?:st|ave|rd|ln|dr|blvd|pl|ct|cir|pkwy|ter)\b)",
        text,
    )
    if street_match:
        text = street_match.group(1)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def equivalent_output_ledger_paths(directory: Path, property_name: str) -> list[Path]:
    """Return current ledger files representing the same street identity."""
    identity = ledger_property_identity(property_name)
    return sorted(
        [
            path
            for path in directory.glob("ECO Systems General Ledger*.csv")
            if path.is_file()
            and ledger_property_identity(path.name) == identity
            and not any(marker in path.name.casefold() for marker in (".bak", "backup", "conflict"))
        ],
        key=lambda path: (len(path.name), path.name.casefold(), str(path).casefold()),
    )


def canonical_output_ledger_path(
    directory: Path,
    property_name: str,
    *,
    allow_divergent_replacement: bool = False,
) -> Path:
    """Reuse one existing equivalent filename and reject divergent aliases."""
    candidates = equivalent_output_ledger_paths(directory, property_name)
    canonical_label = re.sub(r"\.+$", "", str(property_name or "").strip())
    desired = directory / f"ECO Systems General Ledger - {canonical_label}.csv"
    if candidates:
        if allow_divergent_replacement:
            return desired
        return resolve_equivalent_ledgers(candidates)
    return desired
