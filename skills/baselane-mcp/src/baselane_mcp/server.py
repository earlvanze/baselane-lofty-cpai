#!/usr/bin/env python3
"""Baselane MCP Server - Property finance automation"""

from __future__ import annotations
from typing import Annotated, Any
from pydantic import Field
import subprocess
import json
import os
import time
from pathlib import Path

from .pipeline import (
    PipelineValidationError,
    inspect_pipeline_artifact,
    rebuild_dao_cash_reconciliation,
    rebuild_monthly_review_artifacts,
    validate_intercompany_policy,
)
from .transfers import (
    TransferAuthenticationRequired,
    TransferError,
    TransferStateError,
    TransferValidationError,
    build_transfer_plan,
    execute_transfer,
    get_transfer_state,
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
FOLD7_MFA_SCRIPT = SCRIPTS_DIR / "baselane_fold7_mfa.py"
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


def _complete_fold7_mfa(
    bank_account_id: int, timeout_seconds: int, not_before_ms: int
) -> dict[str, Any]:
    """Run the local no-secret Fold 7 verifier and return only its safe JSON report."""
    if not FOLD7_MFA_SCRIPT.is_file():
        return {
            "status": "mfa_helper_unavailable",
            "stage": "preflight",
            "detail": "The canonical Fold 7 MFA helper is unavailable.",
            "sensitive_values_exposed": False,
        }
    timeout_seconds = max(15, min(int(timeout_seconds), 300))
    env = os.environ.copy()
    env["OPENCLAW_WORKSPACE_ROOT"] = str(WORKSPACE_ROOT)
    try:
        result = subprocess.run(
            [
                "python3",
                str(FOLD7_MFA_SCRIPT),
                "--bank-id",
                str(bank_account_id),
                "--timeout",
                str(timeout_seconds),
                "--not-before-ms",
                str(not_before_ms),
            ],
            cwd=WORKSPACE_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout_seconds + 120,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "mfa_helper_timeout",
            "stage": "mfa",
            "detail": "Fold 7 verification timed out without confirming MFA.",
            "sensitive_values_exposed": False,
        }
    try:
        report = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return {
            "status": "mfa_helper_error",
            "stage": "mfa",
            "detail": "Fold 7 verification returned an unreadable safe-status report.",
            "sensitive_values_exposed": False,
        }
    if not isinstance(report, dict):
        return {
            "status": "mfa_helper_error",
            "stage": "mfa",
            "detail": "Fold 7 verification returned an invalid safe-status report.",
            "sensitive_values_exposed": False,
        }
    return {
        "status": report.get("status"),
        "stage": report.get("stage"),
        "detail": report.get("detail"),
        "bank_id": report.get("bank_id"),
        "device_model": report.get("device_model"),
        "otp_source": report.get("otp_source"),
        "sensitive_values_exposed": False,
    }

if FastMCP is not None:
    _P = {
        "entity_id": "Baselane entity ID (property, account, or transaction)",
        "property_address": "Property address for lookup",
        "property_name": "Canonical property name from the no-DAO-mortgage reconciliation policy",
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
        "auto_mfa": "Automatically complete a requested bank SMS challenge through the authorized Fold 7 and retry the exact idempotent transfer",
        "mfa_timeout_seconds": "Seconds to wait for a fresh Fold 7 Baselane SMS; bounded to 15-300",
        "as_of": "Accounting cutoff date in YYYY-MM-DD",
        "run_month": "Accounting month in YYYY-MM",
        "reporting_cutoff_date": "Reporting cutoff date in YYYY-MM-DD",
        "artifact": "Allowlisted CPAI artifact name",
        "include_payload": "Return the complete bounded JSON payload instead of metadata",
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
    def validate_intercompany_overrides(
        as_of: Annotated[str, _F("as_of")],
    ) -> dict[str, Any]:
        """Validate exact ID-bearing ECO/DAO override policy without live mutation."""
        try:
            return validate_intercompany_policy(
                workspace_root=WORKSPACE_ROOT,
                as_of=as_of,
            )
        except PipelineValidationError as exc:
            return {"status": "validation_error", "error": str(exc)}

    @mcp.tool()
    def refresh_dao_cash_reconciliation(
        as_of: Annotated[str, _F("as_of")],
    ) -> dict[str, Any]:
        """Refresh canonical DAO cash and intercompany artifacts using live read-only data."""
        auth_error = _require_verified_auth()
        if auth_error:
            return auth_error
        try:
            return rebuild_dao_cash_reconciliation(
                workspace_root=WORKSPACE_ROOT,
                as_of=as_of,
            )
        except PipelineValidationError as exc:
            return {"status": "validation_error", "error": str(exc)}

    @mcp.tool()
    def rebuild_monthly_review(
        run_month: Annotated[str, _F("run_month")],
        reporting_cutoff_date: Annotated[str, _F("reporting_cutoff_date")],
    ) -> dict[str, Any]:
        """Rebuild monthly review artifacts with cash writes and sends forced off."""
        try:
            return rebuild_monthly_review_artifacts(
                workspace_root=WORKSPACE_ROOT,
                run_month=run_month,
                reporting_cutoff_date=reporting_cutoff_date,
            )
        except PipelineValidationError as exc:
            return {"status": "validation_error", "error": str(exc)}

    @mcp.tool()
    def get_pipeline_artifact(
        artifact: Annotated[str, _F("artifact")],
        property_name: Annotated[str | None, _F("property_name")] = None,
        include_payload: Annotated[bool, _F("include_payload")] = False,
    ) -> dict[str, Any]:
        """Read an allowlisted CPAI JSON artifact or one property's matching records."""
        try:
            return inspect_pipeline_artifact(
                workspace_root=WORKSPACE_ROOT,
                artifact=artifact,
                property_name=property_name,
                include_payload=include_payload,
            )
        except (PipelineValidationError, json.JSONDecodeError, OSError) as exc:
            return {"status": "validation_error", "error": str(exc)}

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
    def get_transfer_status(
        confirmation_token: Annotated[str, _F("confirmation_token")],
    ) -> dict[str, Any]:
        """Inspect durable transfer state before resuming or reconciling an attempt."""
        try:
            return get_transfer_state(
                confirmation_token=confirmation_token,
                state_path=TRANSFER_STATE_PATH,
            )
        except TransferError as exc:
            return {"status": "validation_error", "error": str(exc)}

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
        auto_mfa: Annotated[bool, _F("auto_mfa")] = True,
        mfa_timeout_seconds: Annotated[int, _F("mfa_timeout_seconds")] = 90,
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

        def submit() -> dict[str, Any]:
            return execute_transfer(
                plan=plan,
                confirmation_token=confirmation_token or "",
                graphql_runner=graphql_runner,
                state_path=TRANSFER_STATE_PATH,
            )

        transfer_attempt_started_ms = int(time.time() * 1000) - 5_000
        try:
            return submit()
        except TransferAuthenticationRequired as exc:
            challenge = {
                "status": "authentication_required",
                "challenge_type": "bank_sms_otp",
                "cash_movement_may_require_reconciliation": False,
                "retry_safe_after_mfa": True,
                "confirmation_token": exc.confirmation_token,
                "mfa_bank_account_id": exc.bank_account_id,
                "error": str(exc),
            }
            if not auto_mfa or exc.bank_account_id is None:
                return challenge
            mfa = _complete_fold7_mfa(
                exc.bank_account_id,
                mfa_timeout_seconds,
                transfer_attempt_started_ms,
            )
            if mfa.get("status") != "verified":
                return {
                    **challenge,
                    "status": "mfa_pending",
                    "mfa": mfa,
                }
            try:
                completed = submit()
            except TransferAuthenticationRequired as retry_exc:
                return {
                    **challenge,
                    "status": "authentication_required",
                    "mfa": mfa,
                    "error": str(retry_exc),
                }
            except TransferValidationError as retry_exc:
                return {
                    "status": "validation_error",
                    "cash_movement_may_require_reconciliation": False,
                    "mfa": mfa,
                    "error": str(retry_exc),
                }
            except TransferStateError as retry_exc:
                return {
                    "status": "reconciliation_required",
                    "cash_movement_may_require_reconciliation": True,
                    "mfa": mfa,
                    "error": str(retry_exc),
                }
            except TransferError as retry_exc:
                return {
                    "status": "error",
                    "cash_movement_may_require_reconciliation": False,
                    "mfa": mfa,
                    "error": str(retry_exc),
                }
            return {**completed, "mfa": mfa}
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
    def reconcile_no_dao_mortgage_liability(
        property_name: Annotated[str, _F("property_name")] = "85-104 Alawa Pl",
    ) -> dict[str, Any]:
        """Build an exact-ID mortgage/ECO liability waterfall without mutation.

        Confirmed purpose-supported reimbursements reduce the amount due from
        ECO. Unlabeled or composite transfers remain review candidates, and
        escrow remains restricted DAO cash rather than ECO responsibility.
        """
        auth_error = _require_verified_auth()
        if auth_error:
            return auth_error
        cmd = [
            "python3",
            str(SCRIPTS_DIR / "baselane_reconcile_no_dao_mortgage_liability.py"),
            "--property",
            property_name,
        ]
        env = os.environ.copy()
        env["OPENCLAW_WORKSPACE_ROOT"] = str(WORKSPACE_ROOT)
        try:
            result = subprocess.run(
                cmd,
                cwd=WORKSPACE_ROOT,
                capture_output=True,
                text=True,
                timeout=180,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "timeout",
                "mode": "read_only",
                "error": "No-DAO-mortgage liability reconciliation exceeded 180 seconds.",
            }
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {
                "status": "error",
                "mode": "read_only",
                "returncode": result.returncode,
                "error": (result.stderr or result.stdout or "reconciler returned no JSON")[-2000:],
            }
        payload["returncode"] = result.returncode
        payload["mode"] = "read_only"
        if result.stderr:
            payload["stderr_tail"] = result.stderr[-1000:]
        return payload

    @mcp.tool()
    def split_alawa_eco_transfers(
        apply: Annotated[bool, _F("apply")] = False,
        confirmation_digest: Annotated[str | None, _F("confirmation_digest")] = None,
    ) -> dict[str, Any]:
        """Preview or apply the exact-ID ECO-to-Alawa native split plan.

        All children remain Transfers Between Accounts (tag 24). Applying
        requires the digest returned by a fresh preview and performs an
        independent exact-ID readback before returning success.
        """
        auth_error = _require_verified_auth()
        if auth_error:
            return auth_error
        cmd = ["python3", str(SCRIPTS_DIR / "baselane_split_alawa_eco_transfers.py")]
        if apply:
            if not confirmation_digest:
                return {"status": "validation_error", "error": "apply requires confirmation_digest"}
            cmd.extend(["--apply", "--require-plan-digest", confirmation_digest])
        env = os.environ.copy()
        env["OPENCLAW_WORKSPACE_ROOT"] = str(WORKSPACE_ROOT)
        try:
            result = subprocess.run(
                cmd, cwd=WORKSPACE_ROOT, capture_output=True, text=True,
                timeout=180, env=env,
            )
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "error": "Alawa ECO transfer split workflow exceeded 180 seconds."}
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {
                "status": "error", "returncode": result.returncode,
                "error": (result.stderr or result.stdout or "split workflow returned no JSON")[-2000:],
            }
        payload["returncode"] = result.returncode
        payload["mode"] = "apply" if apply else "preview"
        if result.stderr:
            payload["stderr_tail"] = result.stderr[-1000:]
        return payload

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
