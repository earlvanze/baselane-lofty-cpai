#!/usr/bin/env python3
"""Baselane MCP Server - Property finance automation"""

from __future__ import annotations
from typing import Annotated, Any
from pydantic import Field
import subprocess
import json
import os
from pathlib import Path

from .transfers import (
    TransferError,
    TransferStateError,
    TransferValidationError,
    build_transfer_plan,
    execute_transfer,
    list_active_transfer_accounts,
    run_graphql_via_cdp,
)

try:
    from mcp.server.fastmcp import FastMCP
except Exception as exc:
    FastMCP = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

WORKSPACE_ROOT = Path(
    os.environ.get("OPENCLAW_WORKSPACE_ROOT", Path(__file__).resolve().parents[4])
)
SCRIPTS_DIR = WORKSPACE_ROOT / "scripts"
CONFIG_DIR = Path(__file__).parent.parent / "config"
AUTH_RECOVERY_SCRIPT = SCRIPTS_DIR / "baselane_cdp_auth_recovery.py"
GRAPHQL_CDP_BRIDGE = SCRIPTS_DIR / "baselane_graphql_via_cdp.js"
TRANSFER_STATE_PATH = Path(
    os.environ.get(
        "BASELANE_TRANSFER_STATE_PATH",
        WORKSPACE_ROOT / "reports" / "baselane_transfer_state.json",
    )
)


def _script_command(script: Path) -> list[str]:
    """Use the interpreter that matches the canonical automation script."""
    if script.suffix == ".js":
        return ["node", str(script)]
    return ["python3", str(script)]


def _auth_handoff() -> dict[str, Any]:
    """Verify an attached Baselane CDP session; login seeding is handled separately."""
    if not AUTH_RECOVERY_SCRIPT.is_file():
        return {
            "status": "auth_check_unavailable",
            "manual_action_required": True,
            "next_action": "Restore the canonical Baselane CDP auth recovery script.",
            "next_command": None,
        }

    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE_ROOT"] = str(WORKSPACE_ROOT)
    env["BASELANE_FORCE_LOGIN"] = "0"
    try:
        result = subprocess.run(
            [
                "python3",
                str(AUTH_RECOVERY_SCRIPT),
                "--graphql-auth-smoke",
                "--handoff",
            ],
            cwd=WORKSPACE_ROOT,
            capture_output=True,
            text=True,
            timeout=75,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "auth_check_timeout",
            "manual_action_required": False,
            "next_action": "Retry the read-only Baselane CDP auth check, then use the visible-browser session seeder if login is required.",
            "next_command": f"python3 {AUTH_RECOVERY_SCRIPT} --graphql-auth-smoke --handoff",
        }

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {
            "status": "auth_check_error",
            "manual_action_required": True,
            "next_action": "Inspect the Baselane CDP auth recovery output before continuing.",
            "next_command": f"python3 {AUTH_RECOVERY_SCRIPT} --graphql-auth-smoke --handoff",
            "stderr": (result.stderr or "")[-1000:],
        }
    payload["auth_check_returncode"] = result.returncode
    return payload


def _require_verified_auth() -> dict[str, Any] | None:
    handoff = _auth_handoff()
    if handoff.get("status") == "ready" and handoff.get("manual_action_required") is False:
        return None
    return {
        "status": "auth_required",
        "auth": handoff,
        "error": "Baselane operation not started because the attached CDP session is not verified.",
    }

if FastMCP is not None:
    _P = {
        "entity_id": "Baselane entity ID (property, account, or transaction)",
        "property_address": "Property address for lookup",
        "start_date": "Start date (YYYY-MM-DD)",
        "end_date": "End date (YYYY-MM-DD)",
        "split_type": "Split type: mortgage, expense, income",
        "dry_run": "Preview changes without applying",
        "force": "Force operation even if preconditions not met",
        "output_dir": "Output directory for exports",
        "include_pdfs": "Include PDF statements in export",
        "account_type": "Account type: checking, savings, credit_card, loan",
        "from_transfer_account_id": "Source transferAccountId from list_transfer_accounts (not the bank account ID)",
        "to_transfer_account_id": "Destination transferAccountId from list_transfer_accounts (not the bank account ID)",
        "amount": "Positive USD amount with no more than two decimal places",
        "bookkeeping_note": "Required bookkeeping label, 255 characters or fewer",
        "property_id": "Baselane property ID assigned to the resulting transfer transaction",
        "tag_id": "Baselane bookkeeping tag ID; defaults to Transfers Between Accounts (24)",
        "transfer_date": "Transfer date in YYYY-MM-DD; defaults to today and cannot be in the past",
        "same_day": "Request same-day processing; only valid for today's date",
        "confirmation_token": "Exact token returned by this tool's dry-run preview",
    }

    def _F(name: str, default=None, **field_kwargs):
        desc = _P.get(name, name)
        if default is not None:
            return Field(default=default, description=desc, **field_kwargs)
        return Field(description=desc, **field_kwargs)

    mcp = FastMCP(
        "baselane",
        instructions=(
            "Baselane MCP server for property finance automation. "
            "Use CDP-based tools for auth-heavy operations (exports, splits). "
            "Always dry_run=true first before applying changes. "
            "transfer_cash is restricted to INTERNAL_TRANSFER between non-external "
            "accounts inside the Baselane workspace; never use it to send money "
            "to an outside recipient."
        ),
    )

    @mcp.tool()
    def export_statements(
        property_address: Annotated[str | None, _F("property_address")] = None,
        start_date: Annotated[str | None, _F("start_date")] = None,
        end_date: Annotated[str | None, _F("end_date")] = None,
        include_pdfs: Annotated[bool, _F("include_pdfs")] = False,
        dry_run: Annotated[bool, _F("dry_run")] = True,
    ) -> dict[str, Any]:
        """Export Baselane statements via CDP. Requires authenticated Brave browser session."""
        cmd = _script_command(SCRIPTS_DIR / "baselane_download_statements_cdp.js")
        if property_address:
            cmd.extend(["--property", property_address])
        if start_date:
            cmd.extend(["--start", start_date])
        if end_date:
            cmd.extend(["--end", end_date])
        if dry_run:
            return {"status": "dry_run", "command": " ".join(cmd)}
        auth_error = _require_verified_auth()
        if auth_error:
            return auth_error
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return {"status": "success" if result.returncode == 0 else "error", "output": result.stdout, "error": result.stderr}

    @mcp.tool()
    def split_mortgage(
        property_address: Annotated[str, _F("property_address")],
        split_type: Annotated[str, _F("split_type")] = "mortgage",
        dry_run: Annotated[bool, _F("dry_run")] = True,
    ) -> dict[str, Any]:
        """Split mortgage payments across properties using automation script."""
        cmd = [
            "python3", str(SCRIPTS_DIR / "baselane_mortgage_split_automation.py"),
            "--property", property_address,
            "--type", split_type,
        ]
        if dry_run:
            return {"status": "dry_run", "command": " ".join(cmd)}
        auth_error = _require_verified_auth()
        if auth_error:
            return auth_error
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return {"status": "success" if result.returncode == 0 else "error", "output": result.stdout, "error": result.stderr}

    @mcp.tool()
    def export_ledger(
        entity_id: Annotated[str | None, _F("entity_id")] = None,
        start_date: Annotated[str | None, _F("start_date")] = None,
        end_date: Annotated[str | None, _F("end_date")] = None,
        output_dir: Annotated[str | None, _F("output_dir")] = None,
        dry_run: Annotated[bool, _F("dry_run")] = True,
    ) -> dict[str, Any]:
        """Export general ledger from Baselane."""
        cmd = ["python3", str(SCRIPTS_DIR / "baselane_export_ledger_cdp.py")]
        if entity_id:
            cmd.extend(["--entity", entity_id])
        if start_date:
            cmd.extend(["--start", start_date])
        if end_date:
            cmd.extend(["--end", end_date])
        if output_dir:
            cmd.extend(["--output", output_dir])
        if dry_run:
            return {"status": "dry_run", "command": " ".join(cmd)}
        auth_error = _require_verified_auth()
        if auth_error:
            return auth_error
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return {"status": "success" if result.returncode == 0 else "error", "output": result.stdout, "error": result.stderr}

    @mcp.tool()
    def batch_split(
        start_date: Annotated[str | None, _F("start_date")] = None,
        end_date: Annotated[str | None, _F("end_date")] = None,
        dry_run: Annotated[bool, _F("dry_run")] = True,
    ) -> dict[str, Any]:
        """Batch split multiple transactions."""
        cmd = ["python3", str(SCRIPTS_DIR / "baselane_batch_split.py")]
        if start_date:
            cmd.extend(["--start", start_date])
        if end_date:
            cmd.extend(["--end", end_date])
        if dry_run:
            return {"status": "dry_run", "command": " ".join(cmd)}
        auth_error = _require_verified_auth()
        if auth_error:
            return auth_error
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return {"status": "success" if result.returncode == 0 else "error", "output": result.stdout, "error": result.stderr}

    @mcp.tool()
    def get_auth_status() -> dict[str, Any]:
        """Read-only Baselane CDP auth check; use the session seeder for normal credential login."""
        return _auth_handoff()

    @mcp.tool()
    def list_transfer_accounts() -> dict[str, Any]:
        """List eligible internal Baselane workspace accounts with bank details masked."""
        auth_error = _require_verified_auth()
        if auth_error:
            return auth_error

        def graphql_runner(payload: dict[str, Any]) -> dict[str, Any]:
            return run_graphql_via_cdp(
                payload,
                bridge_path=GRAPHQL_CDP_BRIDGE,
                workspace_root=WORKSPACE_ROOT,
            )

        try:
            accounts = list_active_transfer_accounts(graphql_runner)
        except TransferError as exc:
            return {"status": "error", "error": str(exc)}
        return {
            "status": "success",
            "count": len(accounts),
            "accounts": accounts,
            "note": "Use transfer_account_id, not bank_account_id, with transfer_cash.",
        }

    @mcp.tool()
    def transfer_cash(
        from_transfer_account_id: Annotated[int, _F("from_transfer_account_id")],
        to_transfer_account_id: Annotated[int, _F("to_transfer_account_id")],
        amount: Annotated[str, _F("amount")],
        bookkeeping_note: Annotated[str, _F("bookkeeping_note")],
        property_id: Annotated[int, _F("property_id")],
        tag_id: Annotated[int, _F("tag_id")] = 24,
        transfer_date: Annotated[str | None, _F("transfer_date")] = None,
        same_day: Annotated[bool, _F("same_day")] = True,
        dry_run: Annotated[bool, _F("dry_run")] = True,
        confirmation_token: Annotated[str | None, _F("confirmation_token")] = None,
    ) -> dict[str, Any]:
        """Preview or execute one guarded in-workspace Baselane cash transfer.

        A live transfer requires dry_run=false and the exact confirmation token
        returned by a dry run of the same inputs. Successful submissions and
        ambiguous attempts are persisted locally to suppress duplicate cash
        movement. Outside recipients, external accounts, ACH send-money, wires,
        checks, and every transfer type except INTERNAL_TRANSFER are unsupported.
        """
        try:
            plan = build_transfer_plan(
                from_transfer_account_id=from_transfer_account_id,
                to_transfer_account_id=to_transfer_account_id,
                amount=amount,
                bookkeeping_note=bookkeeping_note,
                property_id=property_id,
                tag_id=tag_id,
                transfer_date=transfer_date,
                same_day=same_day,
            )
        except TransferError as exc:
            return {"status": "validation_error", "error": str(exc)}

        if dry_run:
            return {
                "status": "dry_run",
                "will_move_cash": False,
                "plan": plan,
                "apply_instructions": (
                    "Review every field, then call transfer_cash again with "
                    "dry_run=false and this exact confirmation_token."
                ),
            }

        auth_error = _require_verified_auth()
        if auth_error:
            return auth_error

        def graphql_runner(payload: dict[str, Any]) -> dict[str, Any]:
            return run_graphql_via_cdp(
                payload,
                bridge_path=GRAPHQL_CDP_BRIDGE,
                workspace_root=WORKSPACE_ROOT,
            )

        try:
            return execute_transfer(
                plan=plan,
                confirmation_token=confirmation_token or "",
                graphql_runner=graphql_runner,
                state_path=TRANSFER_STATE_PATH,
            )
        except TransferValidationError as exc:
            return {
                "status": "validation_error",
                "cash_movement_may_require_reconciliation": False,
                "error": str(exc),
            }
        except TransferStateError as exc:
            return {
                "status": "reconciliation_required",
                "cash_movement_may_require_reconciliation": True,
                "error": str(exc),
            }
        except TransferError as exc:
            return {
                "status": "error",
                "cash_movement_may_require_reconciliation": False,
                "error": str(exc),
            }

    @mcp.tool()
    def weekly_unprocessed_report(
        start_date: Annotated[str | None, _F("start_date")] = None,
        end_date: Annotated[str | None, _F("end_date")] = None,
    ) -> dict[str, Any]:
        """Generate weekly unprocessed transactions report."""
        cmd = ["python3", str(SCRIPTS_DIR / "baselane_weekly_unprocessed_report.py")]
        if start_date:
            cmd.extend(["--start", start_date])
        if end_date:
            cmd.extend(["--end", end_date])

        auth_error = _require_verified_auth()
        if auth_error:
            return auth_error
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return {"status": "success" if result.returncode == 0 else "error", "output": result.stdout, "error": result.stderr}

    @mcp.tool()
    def get_pl_entry(
        entity_id: Annotated[str, _F("entity_id")],
        year: Annotated[int | None, _F("year")] = None,
        month: Annotated[int | None, _F("month")] = None,
    ) -> dict[str, Any]:
        """Get P&L entry for entity."""
        # Placeholder - would wrap actual P&L query logic
        return {"status": "not_implemented", "note": "P&L tools need Baselane API integration"}

    @mcp.tool()
    def create_pl_entry(
        entity_id: Annotated[str, _F("entity_id")],
        year: Annotated[int | None, _F("year")] = None,
        month: Annotated[int | None, _F("month")] = None,
        pl_entry: Annotated[dict[str, Any] | None, "P&L entry fields"] = None,
        dry_run: Annotated[bool, _F("dry_run")] = True,
    ) -> dict[str, Any]:
        """Create P&L entry."""
        return {"status": "dry_run", "note": "P&L write operations require Baselane API access"}

    @mcp.tool()
    def update_pl_entry(
        entity_id: Annotated[str, _F("entity_id")],
        year: Annotated[int | None, _F("year")] = None,
        month: Annotated[int | None, _F("month")] = None,
        pl_entry: Annotated[dict[str, Any] | None, "P&L entry fields to update"] = None,
        dry_run: Annotated[bool, _F("dry_run")] = True,
    ) -> dict[str, Any]:
        """Update P&L entry."""
        return {"status": "dry_run", "note": "P&L write operations require Baselane API access"}


def main() -> None:
    if FastMCP is None:
        raise SystemExit("MCP package not installed. Run: pip install mcp") from _IMPORT_ERROR
    mcp.run()


if __name__ == "__main__":
    main()
