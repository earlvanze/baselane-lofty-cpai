#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 -m compileall -q scripts skills/baselane-mcp/src skills/baselane-financials/scripts
python3 -c 'import openpyxl' >/dev/null
# Scope discovery to the canonical test directory. The repository intentionally
# contains runtime symlinks (reports and companion skills); unconstrained pytest
# discovery can traverse those external trees and hang before collecting tests.
python3 -m pytest -q tests

while IFS= read -r -d '' script; do
  bash -n "$script"
done < <(find scripts -maxdepth 1 -type f -name '*.sh' -print0)

if command -v node >/dev/null 2>&1; then
  while IFS= read -r -d '' script; do
    node --check "$script"
  done < <(find scripts skills/baselane-mcp/scripts -maxdepth 1 -type f -name '*.js' -print0)
fi

git diff --check
printf 'Repository checks passed. No live system was contacted.\n'
