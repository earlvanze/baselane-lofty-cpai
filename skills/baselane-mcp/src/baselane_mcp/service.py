"""Baselane service layer - wraps existing scripts for MCP tools"""

import subprocess
import json
import os
from pathlib import Path
from typing import Any, Optional

WORKSPACE_ROOT = Path(
    os.environ.get("OPENCLAW_WORKSPACE_ROOT", Path(__file__).resolve().parents[4])
)
SCRIPTS_DIR = WORKSPACE_ROOT / "scripts"


def run_script(script_name: str, args: Optional[list[str]] = None, timeout: int = 120) -> dict[str, Any]:
    """Run a Baselane script and return structured result."""
    cmd = ["python3", str(SCRIPTS_DIR / script_name)]
    if args:
        cmd.extend(args)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {
            "status": "success" if result.returncode == 0 else "error",
            "stdout": result.stdout.strip() if result.stdout else "",
            "stderr": result.stderr.strip() if result.stderr else "",
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "error": f"Script timed out after {timeout}s"}
    except Exception as e:
        return {"status": "exception", "error": str(e)}


def export_statements_cdp(property_address: Optional[str] = None, start_date: Optional[str] = None, end_date: Optional[str] = None, include_pdfs: bool = False) -> dict[str, Any]:
    """Export statements via CDP."""
    args = []
    if property_address:
        args.extend(["--property", property_address])
    if start_date:
        args.extend(["--start", start_date])
    if end_date:
        args.extend(["--end", end_date])
    if include_pdfs:
        args.append("--pdfs")

    return run_script("baselane_download_statements_cdp.js", args, timeout=300)


def split_mortgage(property_address: str, split_type: str = "mortgage") -> dict[str, Any]:
    """Split mortgage payment across properties."""
    args = ["--property", property_address, "--type", split_type]
    return run_script("baselane_mortgage_split_automation.py", args, timeout=120)


def export_ledger(entity_id: Optional[str] = None, start_date: Optional[str] = None, end_date: Optional[str] = None, output_dir: Optional[str] = None) -> dict[str, Any]:
    """Export general ledger."""
    args = []
    if entity_id:
        args.extend(["--entity", entity_id])
    if start_date:
        args.extend(["--start", start_date])
    if end_date:
        args.extend(["--end", end_date])
    if output_dir:
        args.extend(["--output", output_dir])

    return run_script("baselane_export_ledger_cdp.py", args, timeout=180)


def batch_split(start_date: Optional[str] = None, end_date: Optional[str] = None) -> dict[str, Any]:
    """Batch split transactions."""
    args = []
    if start_date:
        args.extend(["--start", start_date])
    if end_date:
        args.extend(["--end", end_date])

    return run_script("baselane_batch_split.py", args, timeout=300)


def check_auth_status() -> dict[str, Any]:
    """Check Baselane auth status."""
    return run_script("baselane_attached_login_and_tokens.py", ["--check"], timeout=30)


def weekly_report(start_date: Optional[str] = None, end_date: Optional[str] = None) -> dict[str, Any]:
    """Generate weekly unprocessed report."""
    args = []
    if start_date:
        args.extend(["--start", start_date])
    if end_date:
        args.extend(["--end", end_date])

    return run_script("baselane_weekly_unprocessed_report.py", args, timeout=60)
