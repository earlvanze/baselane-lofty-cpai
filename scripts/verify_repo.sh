#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 -m compileall -q scripts skills/baselane-mcp/src

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
