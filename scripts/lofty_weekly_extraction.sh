#!/bin/bash
# Weekly Lofty Property Data Extraction
# Runs via cron, extracts all guarded property markdown to Public/00 - README & Property Snapshot.

set -e

WORKSPACE="${OPENCLAW_WORKSPACE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SCRIPT="$WORKSPACE/skills/lofty-pm/scripts/extract_lofty_property_data.py"
LOG_FILE="$WORKSPACE/logs/lofty_extraction_$(date +%Y-%m-%d).log"

mkdir -p "$WORKSPACE/logs"

echo "=== Lofty Weekly Extraction - $(date) ===" | tee -a "$LOG_FILE"

# Check for authenticated Brave CDP session
if ! curl -s http://127.0.0.1:9222/json/version 2>/dev/null | grep -q "webSocketDebuggerUrl"; then
    echo "ERROR: Brave CDP not available at 127.0.0.1:9222" | tee -a "$LOG_FILE"
    echo "Ensure Brave is running with: brave --remote-debugging-port=9222" | tee -a "$LOG_FILE"
    exit 1
fi

# Run extraction
cd "$WORKSPACE"
python3 "$SCRIPT" 2>&1 | tee -a "$LOG_FILE"

# Count properties extracted
DETAILS_COUNT=$(find "$WORKSPACE" -path "*/Public/00 - README & Property Snapshot/DETAILS.md" | wc -l)
FINANCIALS_COUNT=$(find "$WORKSPACE" -path "*/Public/00 - README & Property Snapshot/FINANCIALS.md" | wc -l)

echo "=== Summary ===" | tee -a "$LOG_FILE"
echo "Properties with DETAILS.md: $DETAILS_COUNT" | tee -a "$LOG_FILE"
echo "Properties with FINANCIALS.md: $FINANCIALS_COUNT" | tee -a "$LOG_FILE"
echo "Log: $LOG_FILE" | tee -a "$LOG_FILE"

exit 0
