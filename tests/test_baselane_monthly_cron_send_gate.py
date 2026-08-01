import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "baselane_financials_monthly_cron.sh"
STATEMENTS_SCRIPT = REPO_ROOT / "scripts" / "baselane_monthly_statements_idempotent.sh"


def write_file(path: Path, text: str, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)


class BaselaneMonthlyCronSendGateTests(unittest.TestCase):
    def test_monthly_review_drafts_are_owned_by_discord_public_agent(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('SEND_MONTHLY_DISCORD_REVIEW_DRAFTS="${SEND_MONTHLY_DISCORD_REVIEW_DRAFTS:-$BASELANE_MONTHLY_LIVE_ACTIONS_APPROVED}"', text)
        self.assertIn('CURRENT_STEP="discord_review_drafts"', text)
        self.assertIn('run_monthly_discord_review_via_agent.py', text)
        self.assertIn('send_monthly_discord_review_drafts.py', text)
        self.assertLess(text.index('CURRENT_STEP="discord_all_send_plan"'), text.index('CURRENT_STEP="discord_review_drafts"'))
        self.assertLess(text.index('CURRENT_STEP="discord_review_drafts"'), text.index('CURRENT_STEP="lofty_pm_publish"'))
        self.assertIn('MONTHLY_DISCORD_REVIEW_DRAFT_STATUS="ok"', text)
        self.assertIn('BASELANE_MONTHLY_DISCORD_REVIEW_DRAFT_AGENT_FILE', text)
        required_order = text.split("MONTHLY_CHAIN_REQUIRED_ORDER = [", 1)[1].split("]", 1)[0]
        self.assertIn('"dao_vendor_upstream_normalization",', required_order)
        self.assertIn('"discord_review_drafts",', required_order)
        self.assertNotIn('"dao_vendor_property_reconciliation",', required_order)

    def test_comms_workspace_requires_monthly_updates_directory(self):
        text = SCRIPT.read_text(encoding="utf-8")

        canonical = '"/home/digit/.openclaw/workspace-lofty-vp"'
        derived = '"$OPENCLAW_ROOT/workspace-lofty-vp"'
        self.assertLess(text.index(canonical), text.index(derived))
        self.assertIn('if [ -d "$candidate/updates" ]; then', text)

    def test_statement_downloader_only_reads_credentials_when_login_is_needed(self):
        text = (REPO_ROOT / "scripts" / "baselane_download_statements_cdp.js").read_text(encoding="utf-8")
        login_branch = text.index("if (!usingExistingAuthedTab) {")

        self.assertGreater(text.index("const email = getCred('username');"), login_branch)
        self.assertGreater(text.index("const password = getCred('password');"), login_branch)

    def test_finance_truth_success_explicitly_allows_downstream_generation(self):
        text = (
            REPO_ROOT / "scripts" / "baselane_monthly_finance_truth_refresh.sh"
        ).read_text(encoding="utf-8")

        self.assertGreaterEqual(
            text.count('"downstream_generation_allowed": True'),
            2,
        )
        self.assertIn("baselane_sync_cdp_deterministic.py", text)
        self.assertNotIn("baselane_sync_cdp_human_paced.py", text)

    def test_current_month_close_can_use_live_transaction_export(self):
        text = STATEMENTS_SCRIPT.read_text(encoding="utf-8")

        self.assertIn(
            'BASELANE_MONTHLY_ALLOW_CURRENT_MONTH_TRANSACTION_EXPORT_CLOSE',
            text,
        )
        self.assertIn(
            'write_statement_gate_report "ok" '
            '"current-month-live-transaction-export-close" "transaction-export"',
            text,
        )
        self.assertIn('"close_source_basis": (', text)
        self.assertIn('"live_transaction_export"', text)

    def test_guarded_apply_manifests_are_initialized_before_use(self):
        text = SCRIPT.read_text(encoding="utf-8")

        financial_definition = (
            'FINANCIAL_APPROVAL_MANIFEST="$REPORT_DIR/lofty_financial_approval_manifest.json"'
        )
        update_definition = (
            'UPDATE_APPROVAL_MANIFEST="$REPORT_DIR/lofty_update_approval_manifest.json"'
        )
        first_guarded_apply = text.index('CURRENT_STEP="lofty_guarded_apply"')

        self.assertLess(text.index(financial_definition), first_guarded_apply)
        self.assertLess(text.index(update_definition), first_guarded_apply)

    def test_owner_email_cooldown_defaults_to_seven_days(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('OWNER_EMAIL_INTERVAL_DAYS="${OWNER_EMAIL_INTERVAL_DAYS:-7}"', text)
        self.assertNotIn('OWNER_EMAIL_INTERVAL_DAYS="${OWNER_EMAIL_INTERVAL_DAYS:-31}"', text)
        self.assertIn('--send-interval-days "$OWNER_EMAIL_INTERVAL_DAYS"', text)

    def test_updates_are_off_cycle_and_live_financials_require_corrective_gate(self):
        cron = SCRIPT.read_text(encoding="utf-8")
        publisher = (REPO_ROOT / "scripts" / "lofty_monthly_publish_to_pm.py").read_text(encoding="utf-8")

        self.assertIn(
            'APPLY_LOFTY_LIVE_FINANCIAL_CORRECTIONS="${APPLY_LOFTY_LIVE_FINANCIAL_CORRECTIONS:-0}"',
            cron,
        )
        self.assertIn(
            'if [ "$APPLY_LOFTY_LIVE_FINANCIAL_CORRECTIONS" = "1" ]',
            cron,
        )
        self.assertNotIn(
            'elif [ "$DRY_RUN" != "1" ] && [ "$MONTHLY_LIVE_SEND_WINDOW_OK" != "1" ]',
            cron,
        )
        self.assertNotIn(
            '&& [ "$MONTHLY_LIVE_SEND_WINDOW_OK" = "1" ] \\\n  && [ "$PUBLISH_LOFTY_PM_UPDATES" = "1" ]',
            cron,
        )
        self.assertIn('"--live-financial-corrective"', publisher)
        self.assertIn("args.live_financial_corrective", publisher)

    def test_transfer_reconciliation_receives_ecogl_autonomy_gate(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('--ecogl-autonomy-report "$ECOGL_DATA_QUALITY_AUTONOMY_REPORT"', text)

    def test_early_upstream_failure_replaces_stale_lofty_publish_evidence(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("write_lofty_pm_publish_upstream_failure_hold_report()", text)
        self.assertIn('if [ "$rc" -ne 0 ] && [ "$CURRENT_STEP" != "baselane_disk_space_preflight" ]; then', text)
        self.assertIn('report.get("downstream_generation_allowed") is False', text)
        self.assertIn('existing.get("status") == "blocked_upstream"', text)
        self.assertIn('"publish_attempted": False', text)
        self.assertIn('"lofty_pm_publish_publish_attempted": False', text)
        self.assertIn('"safe_external_effect": False', text)

    def test_monthly_cron_has_internal_self_lock_for_manual_runs(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('BASELANE_MONTHLY_LOCK_PATH="${BASELANE_MONTHLY_LOCK_PATH:-/home/digit/.cache/openclaw-locks/baselane-monthly-v2.lock}"', text)
        self.assertIn('BASELANE_MONTHLY_SKIP_SELF_LOCK="${BASELANE_MONTHLY_SKIP_SELF_LOCK:-0}"', text)
        self.assertIn("baselane_monthly_parent_holds_lock()", text)
        self.assertIn('/proc/$pid/cmdline', text)
        self.assertIn('[ "$hop_count" -lt 16 ]', text)
        self.assertNotIn('ps -o args= -p', text)
        self.assertIn('grep -Fq "$BASELANE_MONTHLY_LOCK_PATH"', text)
        self.assertIn('flock -n -E 75 "$BASELANE_MONTHLY_LOCK_PATH" "$0" "$@"', text)
        self.assertIn("another monthly Baselane/Lofty run already holds", text)
        self.assertIn('exit 75', text)

    def test_monthly_source_cash_audit_fails_closed_quickly(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('MONTHLY_SOURCE_CASH_AUDIT_TIMEOUT_SECONDS="${BASELANE_MONTHLY_SOURCE_CASH_AUDIT_TIMEOUT_SECONDS:-300}"', text)
        self.assertIn('timeout --kill-after=30s "${MONTHLY_SOURCE_CASH_AUDIT_TIMEOUT_SECONDS}s"', text)
        self.assertIn('"reason": "monthly_source_cash_audit_timeout"', text)
        self.assertIn('continuing fail-closed', text)

    def test_auth_failure_does_not_downgrade_scheduled_live_run_to_dry_run(self):
        text = SCRIPT.read_text(encoding="utf-8")
        degraded_mode = text[
            text.index("enter_baselane_auth_degraded_mode() {") : text.index(
                'CURRENT_STEP="baselane_monthly_statements_idempotent"'
            )
        ]

        self.assertNotIn("DRY_RUN=1", degraded_mode)
        self.assertNotIn("APPLY_LOFTY_GUARDED_UPDATES=0", degraded_mode)
        self.assertNotIn("SEND_MONTHLY_DISCORD_PROPERTY_UPDATE=0", degraded_mode)
        self.assertNotIn("SEND_TRANSFER_RECONCILIATION_TELEGRAM=0", degraded_mode)
        self.assertIn("failing scheduled live run for recovery retry", text)

    def test_monthly_live_actions_require_explicit_global_approval(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('BASELANE_MONTHLY_LIVE_ACTIONS_APPROVED="${BASELANE_MONTHLY_LIVE_ACTIONS_APPROVED:-0}"', text)
        self.assertIn('if [ "$BASELANE_MONTHLY_LIVE_ACTIONS_APPROVED" != "1" ]; then', text)
        self.assertIn('BASELANE_MONTHLY_LIVE_ACTIONS_BLOCKED_REASON="BASELANE_MONTHLY_LIVE_ACTIONS_APPROVED is not enabled"', text)
        self.assertIn('BASELANE_MONTHLY_DRY_RUN_FORCED_BY_APPROVAL_GATE="0"', text)
        self.assertIn('DRY_RUN=1', text[text.index('if [ "$BASELANE_MONTHLY_LIVE_ACTIONS_APPROVED" != "1" ]; then'):])
        self.assertIn('BASELANE_MONTHLY_DRY_RUN_FORCED_BY_APPROVAL_GATE="1"', text)
        self.assertIn('SEND_OWNER_EMAILS=0', text)
        self.assertIn('APPLY_LOFTY_GUARDED_UPDATES=0', text)
        self.assertIn('SEND_TRANSFER_RECONCILIATION_TELEGRAM=0', text)
        self.assertIn('"live_actions_approved": os.environ.get("BASELANE_MONTHLY_LIVE_ACTIONS_APPROVED") == "1"', text)
        self.assertIn('"dry_run_forced_by_approval_gate": os.environ.get("BASELANE_MONTHLY_DRY_RUN_FORCED_BY_APPROVAL_GATE") == "1"', text)
        self.assertIn('"live_actions_blocked_reason": os.environ.get("BASELANE_MONTHLY_LIVE_ACTIONS_BLOCKED_REASON") or None', text)

    def test_source_cash_audit_runs_before_any_guarded_dropbox_apply(self):
        text = SCRIPT.read_text(encoding="utf-8")

        preapply = text.index('CURRENT_STEP="source_cash_preapply_freshness_refresh"')
        guarded_apply = text.index('CURRENT_STEP="lofty_guarded_apply"')
        self.assertLess(preapply, guarded_apply)
        self.assertIn('MONTHLY_SOURCE_CASH_MODE="${BASELANE_MONTHLY_SOURCE_CASH_MODE:-full_column_e}"', text)
        self.assertIn('full_column_e|as_of_month_end', text)
        self.assertIn('--source-cash-mode "$MONTHLY_SOURCE_CASH_MODE"', text[preapply:guarded_apply])
        self.assertIn('--reporting-cutoff-date "$REPORTING_CUTOFF_DATE"', text[preapply:guarded_apply])
        self.assertIn('source_cash_preapply_args+=(--apply)', text[preapply:guarded_apply])
        self.assertIn('SOURCE_CASH_PREAPPLY_ALLOWED="1"', text[preapply:guarded_apply])
        self.assertIn('LOFTY_GUARDED_APPLY_STATUS="blocked_source_cash_preapply"', text)
        self.assertIn('LOFTY_SAFE_CANDIDATE_APPROVAL_STATUS="blocked_source_cash_preapply"', text)
        self.assertIn('&& [ "$SOURCE_CASH_PREAPPLY_ALLOWED" = "1" ]', text)

    def test_monthly_cf_audit_verifies_workbooks_with_bounded_timeout(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn(
            'CF_BALANCE_SHEET_WORKBOOK_AUDIT_TIMEOUT_SECONDS="${BASELANE_CF_BALANCE_SHEET_WORKBOOK_AUDIT_TIMEOUT_SECONDS:-20}"',
            text,
        )
        self.assertIn(
            'CF_BALANCE_SHEET_WORKBOOK_AUDIT_TOTAL_TIMEOUT_SECONDS="${BASELANE_CF_BALANCE_SHEET_WORKBOOK_AUDIT_TOTAL_TIMEOUT_SECONDS:-900}"',
            text,
        )
        cf_block = text[
            text.index('CURRENT_STEP="cf_balance_sheet_consistency"') : text.index(
                'CURRENT_STEP="yhome_operating_cash_apply_verify"'
            )
        ]
        self.assertIn(
            'timeout --kill-after=30s "${CF_BALANCE_SHEET_WORKBOOK_AUDIT_TOTAL_TIMEOUT_SECONDS}s"',
            cf_block,
        )
        self.assertIn("--audit-workbooks", cf_block)
        self.assertIn("--workbook-timeout-seconds", cf_block)
        self.assertIn('cf_balance_sheet_consistency_rc" -eq 124', cf_block)
        self.assertIn('"reason": "cf_balance_sheet_workbook_audit_timeout"', cf_block)
        self.assertIn("refusing downstream outputs", cf_block)

    def test_monthly_publish_requires_clean_historical_cf_truth_apply(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn(
            'HISTORICAL_CF_STATEMENT_STANDARDIZE_FILE="${HISTORICAL_CF_STATEMENT_STANDARDIZE_FILE:-$REPORT_DIR/baselane_live_cf_statement_standardize_2026_readonly.json}"',
            text,
        )
        self.assertIn(
            'HISTORICAL_CF_STATEMENT_START_MONTH="${HISTORICAL_CF_STATEMENT_START_MONTH:-2026-01}"',
            text,
        )
        self.assertIn(
            'HISTORICAL_CF_STATEMENT_APPLY_P_AND_L_ONLY="${HISTORICAL_CF_STATEMENT_APPLY_P_AND_L_ONLY:-1}"',
            text,
        )
        self.assertIn('CURRENT_STEP="historical_cf_statement_standardize"', text)
        self.assertIn('HISTORICAL_CF_STATEMENT_STANDARDIZE_ACTION_COUNT="0"', text)
        self.assertIn(
            'BASELANE_MONTHLY_HISTORICAL_CF_STATEMENT_STANDARDIZE_STATUS="$HISTORICAL_CF_STATEMENT_STANDARDIZE_STATUS"',
            text,
        )
        self.assertIn('"historical_cf_statement_standardize_status"', text)
        self.assertIn('"historical_cf_statement_standardize_action_count"', text)
        self.assertIn('"historical_cf_statement_standardize_apply_p_and_l_only"', text)

        historical_block = text[
            text.index('CURRENT_STEP="historical_cf_statement_standardize"') : text.index(
                'if [ -f "$DAILY_SOURCE_CASH_BALANCE_SCRIPT" ]'
            )
        ]
        self.assertIn('historical_cf_month_args+=(--month "$historical_month")', historical_block)
        self.assertIn('"$HISTORICAL_CF_STATEMENT_START_MONTH" "$RUN_MONTH"', historical_block)
        self.assertIn('--report "$HISTORICAL_CF_STATEMENT_STANDARDIZE_FILE"', historical_block)
        self.assertIn('historical_cf_standardize_mode_args=(--p-and-l-only)', historical_block)
        self.assertIn('historical_cf_standardize_mode_args+=(--apply-p-and-l-only)', historical_block)

        publish_gate = text[
            text.index('monthly_financial_data_block()') : text.index(
                'if [ "$MONTHLY_FINANCIAL_DATA_PUBLISH_ALLOWED" = "1" ]'
            )
        ]
        self.assertIn('HISTORICAL_CF_STATEMENT_STANDARDIZE_CHANGED_PROPERTY_COUNT', publish_gate)
        self.assertIn('HISTORICAL_CF_STATEMENT_STANDARDIZE_MISSING_SOURCE_COUNT', publish_gate)
        self.assertIn('HISTORICAL_CF_STATEMENT_STANDARDIZE_ACTION_COUNT', publish_gate)
        self.assertNotIn(
            'HISTORICAL_CF_STATEMENT_STANDARDIZE_CHANGED_PROPERTY_COUNT" -ne 0',
            publish_gate,
        )

    def test_monthly_cron_repairs_eco_cash_before_live_auth_dependent_cf_work(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn(
            'ECO_CASH_SOURCE_ONLY_STANDARDIZE_FILE="${ECO_CASH_SOURCE_ONLY_STANDARDIZE_FILE:-$REPORT_DIR/baselane_live_cf_statement_standardize_eco_cash_source_only.json}"',
            text[: text.index('CURRENT_STEP="eco_cash_source_only_standardize"')],
        )
        self.assertIn(
            'ECO_CASH_SOURCE_ONLY_STANDARDIZE_FILE="${ECO_CASH_SOURCE_ONLY_STANDARDIZE_FILE:-$REPORT_DIR/baselane_live_cf_statement_standardize_eco_cash_source_only.json}"',
            text,
        )
        self.assertIn('CURRENT_STEP="eco_cash_source_only_standardize"', text)
        self.assertIn('--balance-sheet-source-only', text)
        self.assertIn('--apply-balance-sheet-source-only', text)
        self.assertIn('--yhome-csv "$YHOME_TRANSITION_RECONCILIATION_CSV"', text)
        self.assertIn('ECO Operating Cash source-only CF repair is not verified', text)
        source_only_block = text[
            text.index('CURRENT_STEP="eco_cash_source_only_standardize"') : text.index(
                'CURRENT_STEP="live_cf_statement_standardize"'
            )
        ]
        self.assertIn('--source-cash-report "$DAILY_SOURCE_CASH_BALANCE_REPORT_FILE"', source_only_block)
        self.assertIn('--report "$ECO_CASH_SOURCE_ONLY_STANDARDIZE_FILE"', source_only_block)

    def test_monthly_refreshes_and_requires_clean_untagged_ecogl_review(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('UNTAGGED_REVIEW_SCRIPT="$ROOT/scripts/baselane_cf_untagged_review_packet.py"', text)
        self.assertIn('CURRENT_STEP="untagged_review_refresh"', text)
        self.assertIn('--gl-csv "$UNTAGGED_REVIEW_SOURCE_LEDGER"', text)
        self.assertIn('--untagged-review-report "$UNTAGGED_REVIEW_REPORT"', text)
        publish_gate = text[
            text.index('monthly_financial_data_block()') : text.index(
                'if [ "$MONTHLY_FINANCIAL_DATA_PUBLISH_ALLOWED" = "1"'
            )
        ]
        self.assertIn('UNTAGGED_REVIEW_STATUS', publish_gate)
        self.assertIn('UNTAGGED_REVIEW_ROW_COUNT', publish_gate)
        self.assertIn('UNTAGGED_REVIEW_REQUIRED_COUNT', publish_gate)

    def test_baselane_auth_preflight_fails_closed_quickly(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('BASELANE_AUTH_PREFLIGHT_TIMEOUT_SECONDS="${BASELANE_AUTH_PREFLIGHT_TIMEOUT_SECONDS:-90}"', text)
        self.assertIn(
            'timeout --kill-after=15s "${BASELANE_AUTH_PREFLIGHT_TIMEOUT_SECONDS}s" "$PY" "$BASELANE_AUTH_RECOVERY_SCRIPT"',
            text,
        )
        self.assertIn('if [ "$baselane_auth_preflight_rc" -eq 124 ]; then', text)
        self.assertIn('"error": "Baselane auth recovery probe timed out"', text)

    def test_baselane_auth_preflight_preserves_verified_session_before_recovery(self):
        text = SCRIPT.read_text(encoding="utf-8")
        finance_truth_block = text[
            text.index('CURRENT_STEP="baselane_monthly_finance_truth_refresh"') : text.index('CURRENT_STEP="future_cf_values_cleanup"')
        ]

        first_smoke = finance_truth_block.index('--graphql-auth-smoke')
        recovery = finance_truth_block.index('--recover-login')
        self.assertLess(first_smoke, recovery)
        self.assertIn('if [ "$baselane_auth_preflight_rc" -ne 0 ] || [ "$baselane_auth_preflight_status" != "ok" ]; then', finance_truth_block)
        self.assertIn('Preserve a working visible Baselane session', finance_truth_block)

    def test_auth_failure_runs_only_auth_independent_eco_repair_before_exit(self):
        text = SCRIPT.read_text(encoding="utf-8")
        start = text.index('MONTHLY_FINANCE_TRUTH_REFRESH_STATUS="failed"')
        end = text.index('set +e\n  RUN_MONTH="$RUN_MONTH"', start)
        auth_failure_block = text[start:end]
        self.assertIn('--balance-sheet-source-only', auth_failure_block)
        self.assertIn('--apply-balance-sheet-source-only', auth_failure_block)
        self.assertIn('ECO_CASH_SOURCE_ONLY_STANDARDIZE_STATUS="failed_auth_fallback"', auth_failure_block)
        self.assertIn('failing scheduled live run for recovery retry', auth_failure_block)

    def test_transfer_report_financial_holds_are_wired_only_to_lofty_publisher(self):
        text = SCRIPT.read_text(encoding="utf-8")
        owner_packet_block = text[
            text.index("OWNER_EMAIL_PACKET_ARGS=(") : text.index("if [ \"$packet_mode\" = \"send\" ]")
        ]
        publish_block = text[
            text.index("PUBLISH_ARGS=(") : text.index("if [ \"$REQUIRE_GUILD_TEST_POST_BEFORE_OWNER_EMAIL\" = \"1\" ]")
        ]

        self.assertNotIn("--transfer-reconciliation-report", owner_packet_block)
        self.assertIn('--transfer-reconciliation-report "$TRANSFER_RECONCILIATION_FILE"', publish_block)
        self.assertIn(
            'PUBLISH_ARGS+=(--listing-update-policy "$ROOT/config/lofty_listing_update_policy.json")',
            publish_block,
        )

    def test_transfer_telegram_send_uses_idempotent_sent_state(self):
        text = SCRIPT.read_text(encoding="utf-8")
        telegram_block = text[
            text.index('CURRENT_STEP="transfer_reconciliation_telegram"') : text.index(
                'CURRENT_STEP="monthly_readiness_report_post_reconciliation"'
            )
        ]

        self.assertIn(
            'TRANSFER_RECONCILIATION_TELEGRAM_SEND_STATE_FILE="${TRANSFER_RECONCILIATION_TELEGRAM_SEND_STATE_FILE:-$REPORT_DIR/baselane_lofty_transfer_requirements_telegram_send_state.json}"',
            text,
        )
        self.assertIn('--sent-state-file "$TRANSFER_RECONCILIATION_TELEGRAM_SEND_STATE_FILE"', telegram_block)
        self.assertIn('--current-run-started-at "$RUN_STARTED_AT"', telegram_block)
        self.assertIn('ALLOW_REVIEW_TRANSFER_RECONCILIATION_TELEGRAM="${ALLOW_REVIEW_TRANSFER_RECONCILIATION_TELEGRAM:-0}"', text)
        self.assertIn('ALLOW_INFORMATIONAL_TRANSFER_RECONCILIATION_TELEGRAM="${ALLOW_INFORMATIONAL_TRANSFER_RECONCILIATION_TELEGRAM:-0}"', text)
        self.assertIn('ALLOW_BLOCKED_TRANSFER_RECONCILIATION_TELEGRAM="${ALLOW_BLOCKED_TRANSFER_RECONCILIATION_TELEGRAM:-0}"', text)
        self.assertIn('TELEGRAM_ARGS+=(--allow-informational-report)', telegram_block)
        self.assertIn('TELEGRAM_ARGS+=(--allow-blocked-report)', telegram_block)
        self.assertIn('TRANSFER_RECONCILIATION_TELEGRAM_STATUS="blocked_informational"', telegram_block)
        self.assertIn('TRANSFER_RECONCILIATION_CURRENT_FOR_RUN="0"', text)
        self.assertIn('TRANSFER_RECONCILIATION_CURRENT_FOR_RUN', telegram_block)
        self.assertIn('TRANSFER_RECONCILIATION_TELEGRAM_STATUS="blocked_not_current"', telegram_block)
        self.assertIn('[ "$TRANSFER_RECONCILIATION_TELEGRAM_STATUS" != "blocked_informational" ]', telegram_block)
        self.assertIn('[ "$TRANSFER_RECONCILIATION_TELEGRAM_STATUS" != "blocked_not_current" ]', telegram_block)
        self.assertIn(
            'elif [ "$TRANSFER_RECONCILIATION_STATUS" != "ok" ] || [ "$TRANSFER_RECONCILIATION_RECOMMENDED_TOTAL_IS_FINAL" != "1" ]; then',
            telegram_block,
        )
        self.assertIn('TRANSFER_RECONCILIATION_TELEGRAM_STATUS="blocked_not_final"', telegram_block)
        self.assertIn('[ "$ALLOW_REVIEW_TRANSFER_RECONCILIATION_TELEGRAM" = "1" ] && [ "$DRY_RUN" = "1" ]', telegram_block)

    def test_transfer_telegram_dry_run_refreshes_evidence_when_live_send_is_disabled(self):
        text = SCRIPT.read_text(encoding="utf-8")
        telegram_block = text[
            text.index('CURRENT_STEP="transfer_reconciliation_telegram"') : text.index(
                'CURRENT_STEP="monthly_readiness_report_post_reconciliation"'
            )
        ]

        self.assertIn(
            'if [ "$SEND_TRANSFER_RECONCILIATION_TELEGRAM" != "1" ] && [ "$DRY_RUN" != "1" ]; then',
            telegram_block,
        )
        self.assertIn('if [ "$DRY_RUN" = "1" ]; then', telegram_block)
        self.assertIn('TELEGRAM_ARGS+=(--dry-run)', telegram_block)

    def test_transfer_telegram_send_uses_the_sender_stable_digest_scheme(self):
        text = SCRIPT.read_text(encoding="utf-8")
        telegram_block = text[
            text.index('CURRENT_STEP="transfer_reconciliation_telegram"') : text.index(
                'CURRENT_STEP="monthly_readiness_report_post_reconciliation"'
            )
        ]

        self.assertIn('"$TRANSFER_RECONCILIATION_FILE" "$SCRIPT_DIR"', telegram_block)
        self.assertIn('from transfer_report_digest import stable_transfer_report_digest', telegram_block)
        self.assertIn('stable_transfer_report_digest(Path(sys.argv[1]))', telegram_block)
        self.assertNotIn('hashlib.sha256(path.read_bytes()).hexdigest()', telegram_block)

    def test_monthly_yhome_apply_is_guarded_and_disabled_for_dry_runs(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('YHOME_GSHEET_APPLY="${YHOME_GSHEET_APPLY:-0}"', text)
        self.assertIn(
            'YHOME_OPERATING_CASH_GSHEET_UPDATE_FILE="${YHOME_OPERATING_CASH_GSHEET_UPDATE_FILE:-$REPORT_DIR/yhome_operating_cash_gsheet_update_report.json}"',
            text,
        )
        self.assertIn(
            'YHOME_OPERATING_CASH_APPLY_VERIFY_FILE="${YHOME_OPERATING_CASH_APPLY_VERIFY_FILE:-$REPORT_DIR/yhome_operating_cash_apply_verify_report.json}"',
            text,
        )
        yhome_block = text[
            text.index('CURRENT_STEP="yhome_operating_cash_apply_verify"') : text.index(
                'CURRENT_STEP="quitman_804_cash_alignment"'
            )
        ]
        self.assertIn('YHOME_OPERATING_CASH_APPLY_ARG=()', yhome_block)
        self.assertIn('[ "$DRY_RUN" != "1" ] && [ "$YHOME_GSHEET_APPLY" = "1" ]', yhome_block)
        self.assertIn('YHOME_OPERATING_CASH_APPLY_ARG=(--apply)', yhome_block)
        self.assertIn('"${YHOME_OPERATING_CASH_APPLY_ARG[@]}"', yhome_block)
        self.assertIn('YHOME_OPERATING_CASH_APPLY_VERIFY_TIMEOUT_SECONDS="${YHOME_OPERATING_CASH_APPLY_VERIFY_TIMEOUT_SECONDS:-120}"', text)
        self.assertIn('timeout --kill-after=30s "${YHOME_OPERATING_CASH_APPLY_VERIFY_TIMEOUT_SECONDS}s"', yhome_block)
        self.assertIn('YHOME_OPERATING_CASH_APPLY_VERIFY_STATUS="review_timeout"', yhome_block)
        self.assertIn('"reason": "yhome_apply_verify_timeout"', yhome_block)
        self.assertIn("continuing downstream pipeline", yhome_block)
        self.assertNotIn('exit "$yhome_operating_cash_apply_verify_rc"', yhome_block)

        financial_gate = text[
            text.index('if [ "$CF_BALANCE_SHEET_CONSISTENCY_STATUS" != "ok" ]') :
            text.index('TRANSFER_RECONCILIATION_RECOMMENDED_TOTAL_IS_FINAL="0"')
        ]
        self.assertNotIn("CF_BALANCE_SHEET_CONSISTENCY_YHOME_UPDATE_REQUIRED_COUNT", financial_gate)
        self.assertIn('"blocks_downstream": False', text)
        self.assertIn('"work_product_current": yhome_apply_verify_ok', text)
        self.assertIn(
            'non_blocking_work_product_steps = {\n    "yhome_operating_cash_apply_verify",\n}',
            text,
        )
        self.assertIn("and name not in non_blocking_work_product_steps", text)

    def test_monthly_cron_runs_future_cf_cleanup_before_monthly_artifacts(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('FUTURE_CF_VALUES_APPLY_REPORT_FILE="${FUTURE_CF_VALUES_APPLY_REPORT_FILE:-$REPORT_DIR/future_cf_statement_values_apply_report.json}"', text)
        self.assertIn('FUTURE_CF_VALUES_CLEAR_REPORT_FILE="${FUTURE_CF_VALUES_CLEAR_REPORT_FILE:-$REPORT_DIR/future_cf_statement_values_clear_report.json}"', text)
        self.assertIn('RUN_FUTURE_CF_VALUES_CLEANUP="${RUN_FUTURE_CF_VALUES_CLEANUP:-1}"', text)
        self.assertIn('CURRENT_STEP="future_cf_values_cleanup"', text)
        self.assertIn('FUTURE_CF_VALUES_SCRIPT="$ROOT/scripts/clear_future_cf_statement_values.py"', text)
        self.assertIn('FUTURE_CF_VALUES_CLEANUP_STATUS="skipped_disabled"', text)
        self.assertIn('if [ "$DRY_RUN" = "1" ]; then', text)
        self.assertIn('cp -- "$FUTURE_CF_VALUES_APPLY_REPORT_FILE" "$FUTURE_CF_VALUES_CLEAR_REPORT_FILE"', text)
        self.assertIn('--apply \\', text)
        self.assertIn('--include-archive \\', text)
        self.assertIn('--include-conflicts \\', text)
        self.assertIn('--report "$FUTURE_CF_VALUES_APPLY_REPORT_FILE"', text)
        self.assertIn('--report "$FUTURE_CF_VALUES_CLEAR_REPORT_FILE"', text)
        self.assertIn('FUTURE_CF_VALUES_CLEANUP_STATUS="review_missing_script"', text)
        self.assertIn('future cash-flow value cleanup did not prove a clean idempotent state', text)
        self.assertIn('[ "${FUTURE_CF_VALUES_APPLY_CHANGED_CELL_COUNT:-0}" -gt 0 ]', text)
        self.assertIn('[ "${FUTURE_CF_VALUES_CHANGED_CELL_COUNT:-0}" -gt 0 ]', text)
        self.assertIn('BASELANE_MONTHLY_FUTURE_CF_VALUES_CLEANUP_STATUS="$FUTURE_CF_VALUES_CLEANUP_STATUS"', text)
        self.assertIn('"future_cf_values_cleanup_status"', text)
        self.assertIn('"future_cf_values_cleanup": os.environ.get("BASELANE_MONTHLY_FUTURE_CF_VALUES_CLEANUP_STATUS")', text)
        self.assertIn('"future_cf_values_apply": future_cf_values_apply_path or None', text)
        self.assertIn('"future_cf_values_clear": future_cf_values_clear_path or None', text)
        self.assertLess(text.index('CURRENT_STEP="future_cf_values_cleanup"'), text.index('CURRENT_STEP="monthly_lofty_updates"'))
        self.assertLess(text.index('CURRENT_STEP="future_cf_values_cleanup"'), text.index('CURRENT_STEP="monthly_readiness_report"'))

    def test_monthly_cron_guards_against_repeated_template_mortgage_balances(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('RUN_CF_MORTGAGE_BALANCE_INTEGRITY_GUARD="${RUN_CF_MORTGAGE_BALANCE_INTEGRITY_GUARD:-1}"', text)
        self.assertIn('REQUIRE_CF_MORTGAGE_BALANCE_INTEGRITY_GUARD="${REQUIRE_CF_MORTGAGE_BALANCE_INTEGRITY_GUARD:-1}"', text)
        self.assertIn('CF_MORTGAGE_BALANCE_INTEGRITY_GUARD_FILE="$REPORT_DIR/baselane_cf_mortgage_balance_integrity_guard.json"', text)
        self.assertIn('CURRENT_STEP="cf_mortgage_balance_integrity_guard"', text)
        guard_block = text[
            text.index('CURRENT_STEP="cf_mortgage_balance_integrity_guard"') : text.index('CURRENT_STEP="cf_balance_sheet_consistency"')
        ]
        self.assertIn('baselane_cf_mortgage_balance_integrity_guard.py', guard_block)
        self.assertIn('--apply', guard_block)
        self.assertIn('refusing downstream outputs', guard_block)
        self.assertIn('CF mortgage balance integrity guard is not clean', text)
        self.assertLess(text.index('CURRENT_STEP="historical_cf_statement_standardize"'), text.index('CURRENT_STEP="cf_mortgage_balance_integrity_guard"'))
        self.assertLess(text.index('CURRENT_STEP="cf_mortgage_balance_integrity_guard"'), text.index('CURRENT_STEP="cf_balance_sheet_consistency"'))

    def test_monthly_cron_continues_after_aligned_owner_review_gate(self):
        text = SCRIPT.read_text(encoding="utf-8")
        statements_block = text[
            text.index('CURRENT_STEP="baselane_monthly_statements_idempotent"') : text.index(
                'CURRENT_STEP="baselane_monthly_finance_truth_refresh"'
            )
        ]

        dry_run_statement_block = statements_block[
            statements_block.index('if [ "$DRY_RUN" = "1" ];') : statements_block.index('elif [ ! -x "$ROOT/scripts/baselane_monthly_statements_idempotent.sh" ]')
        ]
        self.assertNotIn("BASELANE_MONTHLY_SKIP_MORTGAGE_WORKFLOW=1", dry_run_statement_block)
        self.assertIn('monthly_statements_rc="$?"', statements_block)
        self.assertIn('[ "$monthly_statements_rc" -eq 2 ]', statements_block)
        self.assertIn('print("review_nonblocking")', statements_block)
        self.assertIn('reason.startswith("aligned-owner-")', statements_block)
        self.assertIn('action in {"auth-baselane", "review-aligned-owner-import", "mortgage-review"}', statements_block)
        self.assertIn('monthly statement gate needs review before owner email send', statements_block)
        self.assertIn('continuing downstream artifact refresh', statements_block)

    def test_monthly_cron_blocks_downstream_publish_on_stale_financial_artifacts(self):
        text = SCRIPT.read_text(encoding="utf-8")
        guard_block = text[
            text.index('CURRENT_STEP="stale_financial_artifact_guard"') : text.index(
                'CURRENT_STEP="baselane_monthly_finance_truth_refresh"'
            )
        ]

        self.assertIn('STALE_FINANCIAL_ARTIFACT_GUARD_FILE="${STALE_FINANCIAL_ARTIFACT_GUARD_FILE:-$REPORT_DIR/baselane_stale_financial_artifact_guard.json}"', text)
        self.assertIn('STALE_FINANCIAL_ARTIFACT_GUARD_TIMEOUT_SECONDS="${BASELANE_STALE_FINANCIAL_ARTIFACT_GUARD_TIMEOUT_SECONDS:-300}"', text)
        self.assertIn('baselane_stale_financial_artifact_guard.py', guard_block)
        self.assertIn('--root "$REAL_ESTATE_ROOT" --report "$STALE_FINANCIAL_ARTIFACT_GUARD_FILE"', guard_block)
        self.assertIn('[ "$stale_financial_artifact_guard_rc" -eq 2 ]', guard_block)
        self.assertIn('refusing downstream publish/email', guard_block)
        self.assertLess(text.index('CURRENT_STEP="baselane_monthly_statements_idempotent"'), text.index('CURRENT_STEP="stale_financial_artifact_guard"'))
        self.assertLess(text.index('CURRENT_STEP="stale_financial_artifact_guard"'), text.index('CURRENT_STEP="baselane_monthly_finance_truth_refresh"'))

    def test_monthly_cron_refreshes_unreviewed_financial_quarantine_as_dry_run(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('UNREVIEWED_FINANCIAL_QUARANTINE_FILE="$REPORT_DIR/lofty_unreviewed_financial_approval_quarantine.json"', text)
        self.assertIn('UNREVIEWED_FINANCIAL_QUARANTINE_COMMANDS_FILE="$REPORT_DIR/lofty_unreviewed_financial_approval_quarantine.requires-explicit-approval.sh"', text)
        self.assertIn('LOFTY_UNREVIEWED_FINANCIAL_QUARANTINE_STATUS="not_started"', text)
        self.assertIn('BASELANE_MONTHLY_UNREVIEWED_FINANCIAL_QUARANTINE_FILE="$UNREVIEWED_FINANCIAL_QUARANTINE_FILE"', text)
        self.assertIn('BASELANE_MONTHLY_UNREVIEWED_FINANCIAL_QUARANTINE_COMMANDS_FILE="$UNREVIEWED_FINANCIAL_QUARANTINE_COMMANDS_FILE"', text)
        self.assertIn('BASELANE_MONTHLY_LOFTY_UNREVIEWED_FINANCIAL_QUARANTINE_STATUS="$LOFTY_UNREVIEWED_FINANCIAL_QUARANTINE_STATUS"', text)
        self.assertIn('"lofty_unreviewed_financial_quarantine_status"', text)
        self.assertIn('"lofty_unreviewed_financial_quarantine": os.environ.get("BASELANE_MONTHLY_LOFTY_UNREVIEWED_FINANCIAL_QUARANTINE_STATUS")', text)
        self.assertIn('"unreviewed_financial_quarantine": os.environ.get("BASELANE_MONTHLY_UNREVIEWED_FINANCIAL_QUARANTINE_FILE")', text)
        self.assertIn('"unreviewed_financial_quarantine_commands": os.environ.get("BASELANE_MONTHLY_UNREVIEWED_FINANCIAL_QUARANTINE_COMMANDS_FILE")', text)

        quarantine_block = text[
            text.index('CURRENT_STEP="lofty_unreviewed_financial_quarantine"') : text.index(
                'CURRENT_STEP="tenant_ledger_folder_guard"'
            )
        ]
        self.assertIn('UNREVIEWED_FINANCIAL_QUARANTINE_SCRIPT="$ROOT/scripts/lofty_unreviewed_financial_approval_quarantine.py"', quarantine_block)
        self.assertIn('--guarded-apply-report "$GUARDED_APPLY_FILE"', quarantine_block)
        self.assertIn('--report "$UNREVIEWED_FINANCIAL_QUARANTINE_FILE"', quarantine_block)
        self.assertIn('--commands-file "$UNREVIEWED_FINANCIAL_QUARANTINE_COMMANDS_FILE"', quarantine_block)
        self.assertIn('LOFTY_UNREVIEWED_FINANCIAL_QUARANTINE_STATUS="review"', quarantine_block)
        self.assertIn('LOFTY_UNREVIEWED_FINANCIAL_QUARANTINE_STATUS="failed"', quarantine_block)
        self.assertNotIn("--apply", quarantine_block)
        self.assertLess(
            text.index('CURRENT_STEP="lofty_unreviewed_financial_quarantine"'),
            text.index('CURRENT_STEP="monthly_readiness_report"'),
        )

    def test_publish_send_requires_explicit_owner_email_allowed_gate(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('MONTHLY_READINESS_OWNER_EMAIL_ALLOWED="0"', text)
        self.assertIn('MONTHLY_READINESS_ACTIONABLE_BLOCKER_COUNT="0"', text)
        self.assertIn('MONTHLY_READINESS_PRIMARY_BLOCKER=""', text)
        self.assertIn('"monthly_readiness_owner_email_allowed"', text)
        self.assertIn('"effective_status"', text)
        self.assertIn('"reported_status"', text)
        self.assertIn('"effective_return_code"', text)
        self.assertIn('"reported_return_code"', text)
        self.assertIn('"effective_failed_step"', text)
        self.assertIn('"reported_failed_step"', text)
        self.assertIn('"review_step_names"', text)
        self.assertIn('"blocking_step_statuses"', text)
        self.assertIn('"actionable_review_step_names"', text)
        self.assertIn('"actionable_blocking_step_statuses"', text)
        self.assertIn('blocking_step_statuses = {', text)
        self.assertIn('non_actionable_review_statuses = {"review_dry_run", "review_not_required"}', text)
        self.assertIn('actionable_blocking_step_statuses = {', text)
        self.assertIn('if (reported_status == "ok" or statements_failure_recovered) and blocking_step_statuses:', text)
        self.assertIn('if "monthly_readiness" in actionable_blocking_step_statuses:', text)
        self.assertIn('effective_failed_step = "monthly_readiness"', text)
        self.assertIn('next(iter(actionable_blocking_step_statuses or blocking_step_statuses))', text)
        self.assertIn('effective_status = "review"', text)
        self.assertIn('reported_status = report["status"]', text)
        self.assertIn('report["status"] = effective_status', text)
        self.assertIn('report["return_code"] = effective_return_code', text)
        self.assertIn('report["failed_step"] = effective_failed_step', text)
        self.assertIn('"monthly_readiness_actionable_blocker_count"', text)
        self.assertIn('"monthly_readiness_primary_blocker"', text)
        self.assertIn('"monthly_owner_review_gate_status"', text)
        self.assertIn('"monthly_statements_gate_status"', text)
        self.assertIn('"monthly_statements_captured_unique_count"', text)
        self.assertIn('"monthly_statements_idempotent"', text)
        self.assertIn('statements_failure_recovered = (', text)
        self.assertIn('"monthly_statements_recovery_status"', text)
        self.assertIn('recovered_by_current_statement_gate', text)
        self.assertIn('if readiness.get("status") == "review" and "monthly_readiness" not in blocking_step_statuses:', text)
        self.assertIn('AUTO_APPROVE_SAFE_REVIEW_CANDIDATES="${AUTO_APPROVE_SAFE_REVIEW_CANDIDATES:-0}"', text)
        self.assertIn('CURRENT_STEP="lofty_safe_candidate_approval"', text)
        self.assertIn('CURRENT_STEP="monthly_owner_review_gate"', text)
        self.assertLess(text.index('run_owner_review_gate "monthly_owner_review_gate"'), text.index('CURRENT_STEP="monthly_readiness_report"'))
        self.assertLess(text.index('CURRENT_STEP="lofty_pm_publish"'), text.index('run_owner_review_gate "monthly_owner_review_gate_post_publish"'))
        self.assertLess(text.index('run_owner_review_gate "monthly_owner_review_gate_post_publish"'), text.index('CURRENT_STEP="owner_email_diagnostic"'))
        self.assertIn('"$ROOT/scripts/lofty_owner_email_weekly.py" --json --workspace "$ROOT"', text)
        self.assertIn('MONTHLY_LIVE_PUBLISH_ALLOWED="0"', text)
        self.assertIn('MONTHLY_FINANCIAL_DATA_PUBLISH_ALLOWED="1"', text)
        self.assertIn('monthly_financial_data_block()', text)
        self.assertIn('recommended_send_to_lofty_total_is_final', text)
        self.assertIn('DAILY_SOURCE_CASH_BALANCE_POST_CF_VERIFY_STATUS', text)
        self.assertIn('[ "$MONTHLY_READINESS_PRIMARY_BLOCKER" = "owner_email.send_guard.not_ok" ]', text)
        self.assertIn('[ "$DRY_RUN" != "1" ] && [ "$MONTHLY_LIVE_PUBLISH_ALLOWED" = "1" ]', text)
        self.assertIn('[ "$MONTHLY_READINESS_OWNER_EMAIL_ALLOWED" != "1" ]', text)
        self.assertIn("write_lofty_pm_publish_disabled_report()", text)
        self.assertIn('"status": "ok_not_published"', text)
        self.assertIn('if [ "$PUBLISH_LOFTY_PM_UPDATES" != "1" ]; then\n    write_lofty_pm_publish_disabled_report', text)
        self.assertIn('"publish_attempted": False', text)
        self.assertIn("monthly_readiness_blocked_reason()", text)
        self.assertIn('REQUIRE_YHOME_SOLD_GUARD="${REQUIRE_YHOME_SOLD_GUARD:-1}"', text)
        self.assertIn('REQUIRE_GUILD_TEST_POST_BEFORE_OWNER_EMAIL="${REQUIRE_GUILD_TEST_POST_BEFORE_OWNER_EMAIL:-1}"', text)
        self.assertIn("YHOME_TRANSITION_RECONCILIATION_URL", text)
        self.assertIn('YHOME_SOLD_GUARD_STATUS="not_started"', text)
        self.assertIn('curl -L --fail --silent --show-error "$YHOME_TRANSITION_RECONCILIATION_URL"', text)
        self.assertIn('--yhome-transition-csv "$YHOME_TRANSITION_RECONCILIATION_CSV"', text)
        self.assertIn('--source-cash-mode "$MONTHLY_SOURCE_CASH_MODE"', text)
        monthly_draft_block = text[
            text.index('CURRENT_STEP="monthly_lofty_updates"') : text.index('CURRENT_STEP="lofty_public_doc_bootstrap"')
        ]
        self.assertIn("fetch_yhome_sold_guard", monthly_draft_block)
        self.assertIn('MONTHLY_LOFTY_ARGS+=(--yhome-transition-csv "$YHOME_TRANSITION_RECONCILIATION_CSV")', monthly_draft_block)
        self.assertIn('MONTHLY_LOFTY_PROPERTY_PATH_CACHE="${MONTHLY_LOFTY_PROPERTY_PATH_CACHE:-$ROOT/reports/monthly_update_property_path_cache.txt}"', monthly_draft_block)
        self.assertIn('MONTHLY_LOFTY_ARGS+=(--property-path-cache "$MONTHLY_LOFTY_PROPERTY_PATH_CACHE")', monthly_draft_block)
        self.assertIn('HEMLANE_RENT_ROLL_DOM_JSON="${HEMLANE_RENT_ROLL_DOM_JSON:-${COMMS_WORKSPACE:-}/updates/${RUN_MONTH}-hemlane-rent-roll-live-dom.json}"', monthly_draft_block)
        self.assertIn('MONTHLY_LOFTY_ARGS+=(--rent-roll-dom-json "$HEMLANE_RENT_ROLL_DOM_JSON")', monthly_draft_block)
        self.assertLess(monthly_draft_block.index("fetch_yhome_sold_guard"), monthly_draft_block.index('"$COMMS_WORKSPACE/scripts/monthly_lofty_updates.sh"'))
        self.assertIn('monthly_lofty_rc="$?"', monthly_draft_block)
        self.assertIn('LOFTY_DRAFT_ATTEMPTS="1"', monthly_draft_block)
        self.assertIn('[ "$monthly_lofty_rc" -eq 141 ] && [ "$DRY_RUN" = "1" ]', monthly_draft_block)
        self.assertIn('LOFTY_DRAFT_RETRIED_AFTER_SIGPIPE="1"', monthly_draft_block)
        self.assertIn('LOFTY_DRAFT_STATUS="failed_rc_${monthly_lofty_rc}"', monthly_draft_block)
        guarded_rerun_block = text[
            text.index('CURRENT_STEP="lofty_guarded_apply_after_safe_candidate_approval"') : text.index(
                'guarded_apply_rerun_rc="$?"'
            )
        ]
        self.assertIn('--yhome-transition-csv "$YHOME_TRANSITION_RECONCILIATION_CSV"', guarded_rerun_block)
        self.assertIn('--listing-update-policy "$ROOT/config/lofty_listing_update_policy.json"', guarded_rerun_block)
        self.assertIn('LOFTY_GUARDED_APPLY_STATUS="review_required"', text)
        self.assertIn('LOFTY_GUARDED_APPLY_STATUS="review_required_after_safe_candidate_approval"', text)
        self.assertLess(text.index('LOFTY_GUARDED_APPLY_STATUS="review_required"'), text.index('LOFTY_GUARDED_APPLY_STATUS="failed_required"'))
        self.assertLess(
            text.index('LOFTY_GUARDED_APPLY_STATUS="review_required_after_safe_candidate_approval"'),
            text.index('LOFTY_GUARDED_APPLY_STATUS="failed_required_after_safe_candidate_approval"'),
        )
        self.assertIn('--guild-test-post-report "$GUILD_TEST_POST_REPORT_FILE"', text)
        self.assertIn("--require-guild-test-post-before-email", text)

    def test_monthly_run_report_mirrors_owner_email_packet_body_guard(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('owner_packet_path = os.environ.get("OWNER_EMAIL_PACKET_FILE") or ""', text)
        self.assertIn("owner_packet = read_json(owner_packet_path) if owner_packet_path else {}", text)
        self.assertIn('"owner_email_packet_status": os.environ.get("BASELANE_MONTHLY_OWNER_EMAIL_PACKET_STATUS") or owner_packet.get("status") or None', text)
        self.assertIn('"owner_email_packet_issue_count": int(os.environ.get("BASELANE_MONTHLY_OWNER_EMAIL_PACKET_ISSUE_COUNT") or owner_packet.get("issue_count") or 0)', text)
        self.assertIn('"owner_email_packet_body_guard_issue_count": int(os.environ.get("BASELANE_MONTHLY_OWNER_EMAIL_PACKET_BODY_GUARD_ISSUE_COUNT") or owner_packet.get("body_guard_issue_count") or 0)', text)
        self.assertIn('"owner_email_packet_native_property_count"', text)
        self.assertIn('"owner_email_packet_native_eligible_property_count"', text)
        self.assertIn('"owner_email_packet_native_financially_held_property_count"', text)
        self.assertIn('"owner_email_packet_native_financially_held_properties"', text)
        self.assertIn('"owner_email_packet_native_property_coverage_ok"', text)
        self.assertIn('"owner_email_packet_native_allowed"', text)
        self.assertIn('"owner_email_packet_native_recipient_source"', text)
        self.assertIn('"owner_email_packet_safe_to_send_now": os.environ.get("BASELANE_MONTHLY_OWNER_EMAIL_PACKET_SAFE_TO_SEND_NOW") == "1" or owner_packet.get("safe_to_send_now") is True', text)
        self.assertIn('LOFTY_PM_GUILD_TEST_POST_REPORT_SCRIPT="$ROOT/scripts/lofty_monthly_guild_test_post_report.py"', text)
        self.assertIn('GUILD_TEST_POST_REPORT_FILE="${GUILD_TEST_POST_REPORT_FILE:-$REPORT_DIR/baselane_financials_monthly_guild_test_post.json}"', text)
        self.assertIn('CURRENT_STEP="lofty_pm_guild_test_post_report"', text)
        self.assertIn('GUILD_TEST_POST_STATUS="prepared_or_preserved"', text)
        self.assertIn('--review-candidate-packet-report "$REVIEW_CANDIDATE_PACKET_FILE"', text)
        self.assertIn('REVIEW_CANDIDATE_LOFTY_PROFILE_JSON="${REVIEW_CANDIDATE_LOFTY_PROFILE_JSON:-}"', text)
        self.assertIn('REVIEW_CANDIDATE_LOFTY_ALL_PROPERTIES_JSON="${REVIEW_CANDIDATE_LOFTY_ALL_PROPERTIES_JSON:-}"', text)
        self.assertNotIn('loftyassist_all_properties_snapshot.json', text)
        self.assertIn('REVIEW_CANDIDATE_PACKET_ARGS+=(--lofty-all-properties-json "$REVIEW_CANDIDATE_LOFTY_ALL_PROPERTIES_JSON")', text)
        self.assertIn('REVIEW_CANDIDATE_PACKET_ARGS+=(--source-ledger "$REVIEW_CANDIDATE_SOURCE_LEDGER")', text)
        self.assertLess(
            text.index('CURRENT_STEP="lofty_pm_guild_test_post_report"'),
            text.index('if [ "$PUBLISH_LOFTY_PM_UPDATES" != "1" ]; then'),
        )
        self.assertIn('"guild_test_post": os.environ.get("BASELANE_MONTHLY_GUILD_TEST_POST_STATUS")', text)
        self.assertIn('SEND_MONTHLY_DISCORD_PROPERTY_UPDATE="${SEND_MONTHLY_DISCORD_PROPERTY_UPDATE:-0}"', text)
        self.assertIn('MONTHLY_DISCORD_PROPERTY_UPDATE_ACCOUNT="${MONTHLY_DISCORD_PROPERTY_UPDATE_ACCOUNT:-}"', text)
        self.assertIn('MONTHLY_DISCORD_PROPERTY_UPDATE_SCRIPT="$ROOT/scripts/send_monthly_discord_property_update.py"', text)

        self.assertIn('[ "$LOFTY_PM_PUBLISH_STATUS" != "review_dry_run" ]', text)
        self.assertIn('[ "$LOFTY_PM_PUBLISH_STATUS" != "ok_not_applied" ]', text)
        self.assertIn('[ "$LOFTY_PM_PUBLISH_STATUS" != "ok_not_published" ]', text)
        discord_property_update_block = text[
            text.index('CURRENT_STEP="discord_property_update"') : text.index('CURRENT_STEP="non_native_owner_email_packet"')
        ]
        self.assertIn('--plan "$MONTHLY_DISCORD_ALL_SEND_PLAN_FILE"', discord_property_update_block)
        self.assertIn('--guild-report "$GUILD_TEST_POST_REPORT_FILE"', discord_property_update_block)
        self.assertIn('DISCORD_PROPERTY_UPDATE_ARGS+=(--account "$MONTHLY_DISCORD_PROPERTY_UPDATE_ACCOUNT")', discord_property_update_block)
        self.assertLess(
            discord_property_update_block.index('[ "$LOFTY_PM_PUBLISH_STATUS" != "ok" ]'),
            discord_property_update_block.index('--plan "$MONTHLY_DISCORD_ALL_SEND_PLAN_FILE"'),
        )
        self.assertIn('[ "$discord_property_update_rc" -eq 2 ]', discord_property_update_block)
        self.assertIn('MONTHLY_DISCORD_PROPERTY_UPDATE_STATUS="review"', discord_property_update_block)
        self.assertLess(
            text.index('CURRENT_STEP="transfer_reconciliation"'),
            text.index('CURRENT_STEP="lofty_pm_publish"'),
        )
        self.assertLess(
            text.index('CURRENT_STEP="lofty_pm_publish"'),
            text.index('CURRENT_STEP="discord_property_update"'),
        )
        self.assertLess(
            text.index('CURRENT_STEP="coownership_gl_policy_validation"'),
            text.index('CURRENT_STEP="transfer_reconciliation"'),
        )
        self.assertIn('REQUIRE_COOWNERSHIP_GL_POLICY_VALIDATION="${REQUIRE_COOWNERSHIP_GL_POLICY_VALIDATION:-1}"', text)
        self.assertIn('COOWNERSHIP_GL_POLICY_VALIDATION_SCRIPT="$ROOT/scripts/validate_coownership_gl_policy.py"', text)
        self.assertIn('COOWNERSHIP_GL_POLICY_VALIDATION_BLOCKERS_CSV="${COOWNERSHIP_GL_POLICY_VALIDATION_BLOCKERS_CSV:-$REPORT_DIR/coownership_gl_policy_validation_blockers.csv}"', text)
        self.assertIn('COOWNERSHIP_GL_POLICY_VALIDATION_MARKDOWN="${COOWNERSHIP_GL_POLICY_VALIDATION_MARKDOWN:-$REPORT_DIR/coownership_gl_policy_validation.md}"', text)
        self.assertIn('--blocker-csv "$COOWNERSHIP_GL_POLICY_VALIDATION_BLOCKERS_CSV"', text)
        self.assertIn('--markdown "$COOWNERSHIP_GL_POLICY_VALIDATION_MARKDOWN"', text)
        self.assertIn('"coownership_gl_policy_validation_blockers_csv"', text)
        self.assertIn('"coownership_gl_policy_validation_markdown"', text)
        self.assertIn('BASELANE_85104_PRECLOSING_RETAG_REPORT_FILE="${BASELANE_85104_PRECLOSING_RETAG_REPORT_FILE:-$REPORT_DIR/baselane_85104_preclosing_property_retag_apply.json}"', text)
        self.assertIn('BASELANE_85104_PRECLOSING_RETAG_COMMANDS_FILE="${BASELANE_85104_PRECLOSING_RETAG_COMMANDS_FILE:-$REPORT_DIR/baselane_85104_preclosing_property_retag_apply.requires-explicit-approval.sh}"', text)
        self.assertIn('BASELANE_85104_PRECLOSING_PROTECTED_REVIEW_IMPORT_COMMANDS="${BASELANE_85104_PRECLOSING_PROTECTED_REVIEW_IMPORT_COMMANDS:-$REPORT_DIR/baselane_85104_preclosing_protected_row_review_import.requires-explicit-approval.sh}"', text)
        self.assertIn('BASELANE_85104_PRECLOSING_PROTECTED_REVIEW_PARTIAL_IMPORT_COMMANDS="${BASELANE_85104_PRECLOSING_PROTECTED_REVIEW_PARTIAL_IMPORT_COMMANDS:-$REPORT_DIR/baselane_85104_preclosing_protected_row_review.partial_preview_import.requires-explicit-approval.sh}"', text)
        self.assertIn('BASELANE_85104_PRECLOSING_RETAG_SCRIPT="$ROOT/scripts/baselane_85104_preclosing_property_retag.py"', text)
        self.assertIn('--payload "$BASELANE_85104_PRECLOSING_RETAG_PAYLOAD_FILE"', text)
        self.assertIn('--commands-file "$BASELANE_85104_PRECLOSING_RETAG_COMMANDS_FILE"', text)
        self.assertIn('--protected-review-import-commands "$BASELANE_85104_PRECLOSING_PROTECTED_REVIEW_IMPORT_COMMANDS"', text)
        self.assertIn('write_monthly_report 2 "failed" "$CURRENT_STEP"', text)
        self.assertIn('"coownership_gl_policy_validation": os.environ.get("BASELANE_MONTHLY_COOWNERSHIP_GL_POLICY_VALIDATION_STATUS")', text)
        self.assertIn('"baselane_85104_preclosing_retag_status"', text)
        self.assertIn('"baselane_85104_preclosing_retag_payload_digest"', text)
        self.assertIn('"baselane_85104_preclosing_retag_payload_digest_reported"', text)
        self.assertIn('"baselane_85104_preclosing_retag_apply_digest_env"', text)
        self.assertIn('"baselane_85104_preclosing_retag_apply_digest_required_value"', text)
        self.assertIn('"baselane_85104_preclosing_retag_apply_digest_ok"', text)
        self.assertIn('"baselane_85104_preclosing_retag_payload_validation_status"', text)
        self.assertIn('"baselane_85104_preclosing_retag_payload_validation"', text)
        self.assertIn('"baselane_85104_preclosing_retag_payload_input_count"', text)
        self.assertIn('"baselane_85104_preclosing_retag_payload_samples"', text)
        self.assertIn('"baselane_85104_preclosing_retag": os.environ.get("BASELANE_MONTHLY_85104_PRECLOSING_RETAG_REPORT_FILE")', text)
        self.assertIn('"baselane_85104_preclosing_retag_commands": os.environ.get("BASELANE_MONTHLY_85104_PRECLOSING_RETAG_COMMANDS_FILE")', text)
        self.assertIn('"baselane_85104_preclosing_protected_row_review_import_commands": os.environ.get("BASELANE_MONTHLY_85104_PRECLOSING_PROTECTED_REVIEW_IMPORT_COMMANDS")', text)
        self.assertIn('"discord_property_update": os.environ.get("BASELANE_MONTHLY_DISCORD_PROPERTY_UPDATE_STATUS")', text)
        self.assertIn('MONTHLY_DISCORD_ALL_SEND_PLAN_FILE="${MONTHLY_DISCORD_ALL_SEND_PLAN_FILE:-$REPORT_DIR/baselane_financials_monthly_discord_all_send_plan.json}"', text)
        self.assertIn('MONTHLY_DISCORD_ALL_SEND_PLAN_VALIDATION_FILE="${MONTHLY_DISCORD_ALL_SEND_PLAN_VALIDATION_FILE:-$REPORT_DIR/baselane_financials_monthly_discord_all_send_plan_validation.json}"', text)
        self.assertIn('BASELANE_MONTHLY_DISCORD_ALL_SEND_PLAN_FILE="$MONTHLY_DISCORD_ALL_SEND_PLAN_FILE"', text)
        self.assertIn('BASELANE_MONTHLY_DISCORD_ALL_SEND_PLAN_VALIDATION_FILE="$MONTHLY_DISCORD_ALL_SEND_PLAN_VALIDATION_FILE"', text)
        self.assertIn('discord_property_update_send = read_json(discord_property_update_send_path) if discord_property_update_send_path else {}', text)
        self.assertIn('discord_all_send_plan_validation = read_json(discord_all_send_plan_validation_path) if discord_all_send_plan_validation_path else {}', text)
        self.assertIn('"discord_all_send_plan": os.environ.get("BASELANE_MONTHLY_DISCORD_ALL_SEND_PLAN_FILE")', text)
        self.assertIn('"discord_all_send_plan_validation": os.environ.get("BASELANE_MONTHLY_DISCORD_ALL_SEND_PLAN_VALIDATION_FILE")', text)
        self.assertIn('CURRENT_STEP="discord_all_send_plan"', text)
        self.assertIn('build_monthly_discord_all_send_plan.py', text)
        self.assertIn('validate_monthly_discord_all_send_plan.py', text)
        validator_text = Path("scripts/validate_monthly_discord_all_send_plan.py").read_text(encoding="utf-8")
        self.assertIn("REQUIRED_SPENDABLE_CASH_SNIPPET", validator_text)
        self.assertIn("draft_missing_verified_spendable_eco_cash", validator_text)
        self.assertIn('--readiness "$READINESS_FILE"', text)
        self.assertIn('--transfer-reconciliation "$TRANSFER_RECONCILIATION_FILE"', text)
        self.assertIn('BASELANE_MONTHLY_DISCORD_ALL_SEND_PLAN_STATUS', text)
        self.assertIn('"discord_all_send_plan": os.environ.get("BASELANE_MONTHLY_DISCORD_ALL_SEND_PLAN_STATUS")', text)
        self.assertIn('"discord_all_property_route_and_summary_coverage"', text)
        self.assertIn('"monthly_active_candidate_set_coverage"', text)
        self.assertIn('"discord_all_property_send_proof_ok"', text)
        self.assertIn('"discord_all_property_send_plan_digest"', text)
        self.assertIn('"discord_all_property_send_plan_digest_ok"', text)
        self.assertIn('"discord_all_property_send_record_count"', text)
        self.assertIn('"discord_all_property_send_failed_count"', text)
        self.assertIn('"stale_route_count": discord_all_send_plan_validation.get("stale_route_count")', text)
        self.assertIn('"require_yhome_sold_guard"', text)
        self.assertIn('"require_guild_test_post_before_owner_email"', text)
        self.assertIn('"yhome_sold_guard"', text)
        self.assertIn('"guild_test_post_report"', text)
        self.assertIn("primary=%s", text)
        self.assertIn("actionable=$MONTHLY_READINESS_ACTIONABLE_BLOCKER_COUNT", text)
        self.assertIn('MONTHLY_READINESS_BLOCKER_COUNT', text)
        self.assertNotIn("audit=$MONTHLY_READINESS_BLOCKER_COUNT", text)
        self.assertIn('--owner-email-blocked-reason "$OWNER_EMAIL_SEND_BLOCKED_REASON"', text)
        self.assertIn("discord_all_send_plan_blocked_reason()", text)
        self.assertIn("Discord all-property send plan validation not ok; refusing owner email before complete Discord review coverage", text)
        self.assertIn('DISCORD_ALL_SEND_PLAN_BLOCKED_REASON="$(discord_all_send_plan_blocked_reason)"', text)
        self.assertIn('OWNER_EMAIL_SEND_BLOCKED_REASON="$DISCORD_ALL_SEND_PLAN_BLOCKED_REASON"', text)
        self.assertLess(text.index('CURRENT_STEP="transfer_reconciliation_telegram"'), text.index('CURRENT_STEP="monthly_readiness_report_post_reconciliation"'))
        self.assertLess(text.index('CURRENT_STEP="monthly_readiness_report_post_reconciliation"'), text.index('CURRENT_STEP="discord_all_send_plan"'))
        self.assertLess(text.index('CURRENT_STEP="discord_all_send_plan"'), text.index('CURRENT_STEP="lofty_pm_publish"'))
        self.assertLess(text.index('CURRENT_STEP="lofty_pm_publish"'), text.index('CURRENT_STEP="discord_property_update"'))
        self.assertIn('[ "$OWNER_EMAIL_SEND_GUARD_STATUS" != "ok" ]', text)
        self.assertIn('OWNER_EMAIL_SEND_GUARD_SEND_ALLOWED="0"', text)
        self.assertIn('OWNER_EMAIL_SEND_GUARD_SAFE_BLOCK="0"', text)
        self.assertIn('OWNER_EMAIL_SEND_GUARD_NO_SPAM_OK="0"', text)
        self.assertIn('SEND_NON_NATIVE_OWNER_EMAILS="${SEND_NON_NATIVE_OWNER_EMAILS:-0}"', text)
        self.assertIn('SEND_NATIVE_LOFTY_OWNER_EMAILS="${SEND_NATIVE_LOFTY_OWNER_EMAILS:-1}"', text)
        self.assertIn('REQUIRE_NATIVE_LOFTY_OWNER_EMAILS="${REQUIRE_NATIVE_LOFTY_OWNER_EMAILS:-1}"', text)
        self.assertIn('OWNER_EMAIL_PACKET_FILE="${OWNER_EMAIL_PACKET_FILE:-$REPORT_DIR/baselane_monthly_owner_email_packet.json}"', text)
        self.assertIn('OWNER_EMAIL_RECIPIENTS_CSV="${OWNER_EMAIL_RECIPIENTS_CSV:-$REPORT_DIR/lofty_owner_email_recipients.csv}"', text)
        self.assertIn('OWNER_EMAIL_PACKET_REVIEW_CANDIDATE_FILE="${OWNER_EMAIL_PACKET_REVIEW_CANDIDATE_FILE:-$REVIEW_CANDIDATE_PACKET_FILE}"', text)
        self.assertIn('OWNER_EMAIL_PACKET_PROPERTY_UNAVAILABLE_COUNT="0"', text)
        self.assertIn('OWNER_EMAIL_PACKET_STATUS_FROM_REPORT', text)
        self.assertIn('if [ -n "$OWNER_EMAIL_PACKET_STATUS_FROM_REPORT" ]; then', text)
        self.assertIn('OWNER_EMAIL_PACKET_STATUS="$OWNER_EMAIL_PACKET_STATUS_FROM_REPORT"', text)
        self.assertIn('CURRENT_STEP="non_native_owner_email_packet"', text)
        self.assertIn('CURRENT_STEP="monthly_pipeline_candidate_coverage"', text)
        self.assertIn('audit_monthly_pipeline_candidate_coverage.py', text)
        self.assertIn('--lofty-publish-report "$LOFTY_PM_PUBLISH_FILE"', text)
        self.assertIn('--owner-email-packet "$OWNER_EMAIL_PACKET_FILE"', text)
        self.assertIn('--discord-send-report "$MONTHLY_DISCORD_PROPERTY_UPDATE_SEND_FILE"', text)
        self.assertIn('MONTHLY_PIPELINE_CANDIDATE_COVERAGE_STATUS', text)
        self.assertIn('"pipeline_candidate_coverage"', text)
        self.assertIn('"pipeline_candidate_coverage_status"', text)
        self.assertIn('"pipeline_candidate_coverage_generated_at"', text)
        self.assertIn('"pipeline_candidate_coverage_input_digests"', text)
        self.assertIn('--runtime-map "$LOFTY_PM_RUNTIME_MAP"', text)
        self.assertIn('[ "$SEND_NON_NATIVE_OWNER_EMAILS" = "1" ]', text)
        self.assertIn('[ "$REQUIRE_NATIVE_LOFTY_OWNER_EMAILS" != "1" ]', text)
        self.assertIn('native lofty-pm owner email delivery is required', text)
        self.assertIn('OWNER_EMAIL_PACKET_ARGS+=(--recipients-csv "$OWNER_EMAIL_RECIPIENTS_CSV")', text)
        self.assertIn('OWNER_EMAIL_PACKET_ARGS+=(--native-only)', text)
        self.assertIn('"owner_email_recipient_source"', text)
        self.assertIn('"owner_email_recipients_csv_required"', text)
        self.assertIn('--sent-state-file "$OWNER_EMAIL_PACKET_SENT_STATE_FILE"', text)
        self.assertIn('--review-candidate-packet-report "$OWNER_EMAIL_PACKET_REVIEW_CANDIDATE_FILE"', text)
        self.assertIn('PUBLISH_ARGS+=(--review-candidate-packet-report "$REVIEW_CANDIDATE_PACKET_FILE")', text)
        self.assertIn('--guild-test-post-report "$GUILD_TEST_POST_REPORT_FILE"', text)
        self.assertIn('OWNER_EMAIL_PACKET_ARGS+=(--send)', text)
        self.assertIn('OWNER_EMAIL_PACKET_ARGS+=(--dry-run)', text)
        self.assertIn('--owner-email-packet-report "$OWNER_EMAIL_PACKET_FILE"', text)
        self.assertIn('--discord-send-report "$MONTHLY_DISCORD_PROPERTY_UPDATE_SEND_FILE"', text)
        self.assertIn('run_non_native_owner_email_packet "dry-run"', text)
        self.assertIn('CURRENT_STEP="non_native_owner_email_packet_send"', text)
        self.assertIn('run_non_native_owner_email_packet "send"', text)
        packet_send_gate = text[
            text.index('if [ "$SEND_OWNER_EMAILS" = "1" ] \\')
            : text.index('CURRENT_STEP="non_native_owner_email_packet_send"')
        ]
        self.assertIn('[ "$DRY_RUN" != "1" ]', packet_send_gate)
        self.assertIn('[ "$PUBLISH_LOFTY_PM_UPDATES" = "1" ]', packet_send_gate)
        self.assertIn('[ "$LOFTY_PM_PUBLISH_STATUS" = "ok" ]', packet_send_gate)
        self.assertNotIn('[ "$MONTHLY_LIVE_SEND_WINDOW_OK" = "1" ]', packet_send_gate)
        self.assertIn('[ "$MONTHLY_READINESS_OWNER_EMAIL_ALLOWED" = "1" ]', packet_send_gate)
        self.assertIn('[ "$MONTHLY_PIPELINE_CANDIDATE_COVERAGE_STATUS" = "ok" ]', packet_send_gate)
        self.assertIn('[ "$OWNER_EMAIL_SEND_GUARD_STATUS" = "ok" ]', packet_send_gate)
        self.assertIn('[ "$OWNER_EMAIL_SEND_GUARD_SEND_ALLOWED" = "1" ]', packet_send_gate)
        self.assertIn('CURRENT_STEP="native_lofty_owner_email_send"', text)
        self.assertIn('LOFTY_ALLOW_NATIVE_OWNER_EMAIL_SIGNAL_ONLY=1', text)
        self.assertIn('--email-only', text)
        self.assertIn('--send-owner-emails', text)
        self.assertIn('[ "$MONTHLY_DISCORD_PROPERTY_UPDATE_STATUS" = "ok" ]', text)
        self.assertLess(text.index('CURRENT_STEP="discord_property_update"'), text.index('CURRENT_STEP="native_lofty_owner_email_send"'))
        self.assertNotIn('native Lofty owner email disabled; non-native reviewed owner email packet required', text)
        self.assertIn('BASELANE_MONTHLY_OWNER_EMAIL_SEND_GUARD_SEND_ALLOWED="$OWNER_EMAIL_SEND_GUARD_SEND_ALLOWED"', text)
        self.assertIn('BASELANE_MONTHLY_OWNER_EMAIL_PACKET_STATUS="$OWNER_EMAIL_PACKET_STATUS"', text)
        self.assertIn('BASELANE_MONTHLY_OWNER_EMAIL_PACKET_PROPERTY_UNAVAILABLE_COUNT="$OWNER_EMAIL_PACKET_PROPERTY_UNAVAILABLE_COUNT"', text)
        self.assertIn('BASELANE_MONTHLY_OWNER_EMAIL_PACKET_NATIVE_PROPERTY_COUNT="$OWNER_EMAIL_PACKET_NATIVE_PROPERTY_COUNT"', text)
        self.assertIn('BASELANE_MONTHLY_OWNER_EMAIL_PACKET_NATIVE_PROPERTY_COVERAGE_OK="$OWNER_EMAIL_PACKET_NATIVE_PROPERTY_COVERAGE_OK"', text)
        self.assertIn('"native_eligible_property_count": report.get("owner_email_packet_native_eligible_property_count")', text)
        self.assertIn('"native_financially_held_property_count": report.get("owner_email_packet_native_financially_held_property_count")', text)
        self.assertIn('MONTHLY_ACCRUALS_ACTIVE_WITHOUT_TEMPLATE_COUNT="0"', text)
        self.assertIn('MONTHLY_ACCRUALS_ACTIVE_WITHOUT_FIXED_REQUIREMENT_COUNT="0"', text)
        self.assertIn('"monthly_accruals_active_without_template_count"', text)
        self.assertIn('"monthly_accruals_active_without_fixed_requirement_count"', text)
        self.assertIn('"monthly_accruals_missing_samples"', text)
        self.assertIn('"monthly_accruals_amount_mismatch_samples"', text)
        self.assertIn('"monthly_accruals_blocked_first_day_pm_fee_samples"', text)
        self.assertIn('"monthly_accruals_active_without_template_samples"', text)
        self.assertIn('"monthly_accruals_active_without_fixed_requirement_samples"', text)
        self.assertIn('"monthly_accruals_expected_fixed_coverage_count"', text)
        self.assertIn('"monthly_accruals_gap_action_queue"', text)
        self.assertIn('"monthly_accruals_blocking_gap_action_count"', text)
        self.assertIn('"monthly_accruals_review_markdown"', text)
        self.assertIn('"missing_fixed_coverage_count": report.get("monthly_accruals_missing_fixed_coverage_count")', text)
        self.assertNotIn('and report.get("monthly_accruals_active_without_template_count") == 0', text)
        self.assertIn('report.get("monthly_accruals_blocking_gap_action_count") == 0', text)
        self.assertIn('report.get("monthly_accruals_missing_fixed_coverage_count") == 0', text)
        self.assertIn('BASELANE_MONTHLY_ACCRUALS_REVIEW_MARKDOWN="$MONTHLY_ACCRUALS_REVIEW_MARKDOWN_FILE"', text)
        self.assertIn('MONTHLY_ACCRUALS_BLOCKING_GAP_ACTION_COUNT', text)
        self.assertIn('--gap-approval-scaffold "${BASELANE_MONTHLY_ACCRUALS_GAP_APPROVAL_SCAFFOLD:-$ROOT/config/baselane_monthly_accrual_gap_approvals.json}"', Path("scripts/baselane_monthly_accruals_cron.sh").read_text(encoding="utf-8"))
        self.assertIn('"active_without_template_count": report.get("monthly_accruals_active_without_template_count")', text)
        self.assertIn('"active_without_fixed_requirement_policy": "audit-only', text)
        self.assertIn('active_without_templates=$MONTHLY_ACCRUALS_ACTIVE_WITHOUT_TEMPLATE_COUNT', text)
        self.assertIn('"owner_email_send_guard_send_allowed"', text)
        self.assertIn('"owner_email_packet_status"', text)
        self.assertIn('"owner_email_packet_property_unavailable_count"', text)
        self.assertIn('"owner_email_packet_full_history_leak_count"', text)
        self.assertIn('"owner_email_send_guard_safe_block"', text)
        self.assertIn('"owner_email_send_guard_no_spam_ok"', text)
        self.assertIn('"owner_email_send_guard_owner_email_packet_ok_for_send"', text)
        self.assertIn('"owner_email_send_guard_owner_email_packet_mcp_native_packet_ok"', text)
        self.assertIn('"owner_email_send_guard_owner_email_packet_legacy_packet_ok"', text)
        self.assertIn("def owner_email_packet_and_guard_ready_for_current_mode(report, dry_run):", text)
        self.assertIn('readiness = read_json(readiness_path) if readiness_path else {}', text)
        self.assertIn('owner_guard = read_json(owner_guard_path) if owner_guard_path else {}', text)
        self.assertIn('monthly_readiness_blocker_count = env_blocker_count or source_blocker_count', text)
        self.assertIn('owner_email_send_blocked_reason = owner_email_send_blocked_reason or (', text)
        self.assertIn('"effective_send_owner_emails"', text)
        self.assertIn('"owner_email_send_guard_status"', text)
        self.assertIn('"owner_email_send_guard_issue_count"', text)
        self.assertIn('"owner_email_send_guard_sent_state_file_matches_run_month"', text)
        self.assertIn('"owner_email_send_guard_max_once_monthly_ok"', text)
        self.assertIn('"owner_email_send_guard_send_evidence_matches_intent"', text)
        self.assertIn('"owner_email_send_guard_email_final_gate_financial_blocked"', text)
        self.assertIn('"owner_email_send_guard_email_final_gate_discord_review_blocked"', text)
        self.assertIn('"owner_email_send_guard_email_final_gate_transfer_telegram_blocked"', text)
        self.assertIn('"owner_email_send_guard_transfer_telegram_send_gate_ok"', text)
        self.assertIn('"owner_email_send_guard_transfer_telegram_send_issues"', text)
        self.assertIn('"owner_email_send_guard_transfer_telegram_transfer_report_digest"', text)
        self.assertIn('"owner_email_send_guard_transfer_telegram_current_transfer_report_digest"', text)
        self.assertIn('"owner_email_send_guard_transfer_telegram_transfer_report_digest_matches_current"', text)
        self.assertIn('MONTHLY_LIVE_SEND_DAY="${MONTHLY_LIVE_SEND_DAY:-15}"', text)
        self.assertIn('ALLOW_OFF_CYCLE_MONTHLY_LIVE_SEND="${ALLOW_OFF_CYCLE_MONTHLY_LIVE_SEND:-0}"', text)
        self.assertIn('ALLOW_OFF_CYCLE_MONTHLY_LIVE_SEND_DIGEST="${ALLOW_OFF_CYCLE_MONTHLY_LIVE_SEND_DIGEST:-}"', text)
        self.assertIn('MONTHLY_LIVE_SEND_OVERRIDE_DIGEST_REQUIRED="$($PY - "$RUN_MONTH" "$CURRENT_LOCAL_DATE" "$MONTHLY_LIVE_SEND_DAY"', text)
        self.assertIn('MONTHLY_LIVE_SEND_OVERRIDE_DIGEST_OK="0"', text)
        self.assertIn('MONTHLY_LIVE_SEND_WINDOW_OK="0"', text)
        self.assertIn('"monthly_live_send_window_ok"', text)
        self.assertIn('"monthly_live_send_override_digest_required"', text)
        self.assertIn('"monthly_live_send_override_digest_ok"', text)
        self.assertIn('"monthly_live_send_window"', text)
        self.assertIn('"monthly_live_final_send_deadline_gate_ok"', text)
        self.assertIn('"monthly_live_final_send_deadline_gate"', text)
        self.assertIn('"owner_email_final_financial_gate_ok"', text)
        self.assertNotIn('live Lofty publish and owner email send require the 15th or explicit ALLOW_OFF_CYCLE_MONTHLY_LIVE_SEND=1 with matching digest', text)
        self.assertNotIn('ALLOW_OFF_CYCLE_MONTHLY_LIVE_SEND_DIGEST=$MONTHLY_LIVE_SEND_OVERRIDE_DIGEST_REQUIRED', text)
        self.assertIn('UPDATES.md publication and owner emails are off-cycle', text)
        self.assertIn('"owner_email_send_guard_financial_review_blocker_count"', text)
        self.assertIn('"owner_email_send_guard_lofty_monthly_summary_issue_records"', text)
        self.assertIn('"owner_email_send_guard_transfer_reconciliation_source_blockers"', text)
        self.assertIn('"owner_email_send_guard_transfer_reconciliation_active_source_cash_actions"', text)
        self.assertIn('"owner_email_send_guard_transfer_reconciliation_property_cash_review_details"', text)
        self.assertIn('"owner_email_send_guard_discord_financial_review_issue_count"', text)
        self.assertIn('"owner_email_send_guard_discord_financial_review_issues"', text)
        self.assertIn('"owner_email_discord_review_chain": owner_guard.get("owner_email_discord_review_chain")', text)
        self.assertIn('"owner_email_send_guard_discord_all_property_send_plan_digest"', text)
        self.assertIn('"owner_email_send_guard_discord_all_property_send_plan_digest_ok"', text)
        self.assertIn('transfer_digest = file_digest(transfer_reconciliation_path)', text)
        self.assertIn('telegram_transfer_digest = transfer_reconciliation_telegram_send.get("transfer_report_digest")', text)
        self.assertIn('and transfer_reconciliation_telegram_send.get("transfer_report_digest_matches_current") is True', text)
        self.assertIn('telegram_transfer_digest == transfer_digest', text)
        self.assertIn('transfer_telegram_current_for_transfer = False', text)
        self.assertIn('--transfer-reconciliation-report "$TRANSFER_RECONCILIATION_FILE"', text)
        self.assertIn('--transfer-telegram-send-report "$TRANSFER_RECONCILIATION_TELEGRAM_SEND_FILE"', text)
        self.assertIn('--discord-plan-validation-report "$MONTHLY_DISCORD_ALL_SEND_PLAN_VALIDATION_FILE"', text)
        owner_email_guard_block = text[
            text.index("refresh_owner_email_send_guard_report()") : text.index(
                "refresh_monthly_readiness_report()"
            )
        ]
        self.assertIn('--guild-test-post-report "$GUILD_TEST_POST_REPORT_FILE"', owner_email_guard_block)
        self.assertIn('"owner_email_final_financial_gate"', text)
        self.assertIn('"transfer_reconciliation_telegram_current_for_transfer"', text)
        self.assertIn('"transfer_reconciliation_telegram_recorded_transfer_digest": telegram_transfer_digest', text)
        self.assertIn('"transfer_telegram_transfer_report_digest_matches_current": report.get("owner_email_send_guard_transfer_telegram_transfer_report_digest_matches_current")', text)
        self.assertIn('"telegram_generated_at": report.get("transfer_reconciliation_telegram_generated_at")', text)
        self.assertIn('"Telegram DM final delivery is not proven; dry-run preview does not satisfy monthly close"', text)
        self.assertIn('"owner email final gate still has unresolved financial review blockers"', text)
        self.assertIn('"coownership_prelaunch_gl_policy_and_85104_retag"', text)
        self.assertIn('"co-ownership GL policy still has pre-launch rows or the prepared 85-104 upstream retag has not been applied"', text)
        self.assertIn('int(report.get("owner_email_publish_send_evidence_count") or 0)', text)
        self.assertIn('== int(report.get("owner_email_publish_will_send_count") or 0)', text)
        self.assertIn('int(report.get("owner_email_publish_will_send_count") or 0) > 0', text)
        self.assertIn('report.get("owner_email_send_guard_send_evidence_matches_intent") is True', text)
        self.assertIn('"monthly_statements_auth_recovery_status": statements_idempotent.get("auth_recovery_status")', text)
        self.assertIn('"yhome_sold_guard_status"', text)
        self.assertIn('"yhome_transition_reconciliation"', text)
        self.assertIn('"lofty_update_drafts_status"', text)
        self.assertIn('"send_allowed", "safe_block", "no_spam_guard_ok"', text)
        self.assertIn('[ "$OWNER_EMAIL_SEND_GUARD_SEND_ALLOWED" != "1" ]', text)
        self.assertIn("legacy weekly sender refused because owner email send guard did not explicitly allow send", text)
        self.assertIn("non-native owner email packet did not write sent state", text)
        self.assertIn('OWNER_EMAIL_STATUS="sent_by_non_native_owner_email_packet"', text)
        self.assertLess(text.index('CURRENT_STEP="owner_email_send_guard"'), text.index('CURRENT_STEP="owner_email_diagnostic"'))
        self.assertLess(text.index('CURRENT_STEP="non_native_owner_email_packet"'), text.index('CURRENT_STEP="owner_email_send_guard"'))
        self.assertLess(text.index('CURRENT_STEP="non_native_owner_email_packet"'), text.index('CURRENT_STEP="monthly_pipeline_candidate_coverage"'))
        self.assertLess(text.index('CURRENT_STEP="monthly_pipeline_candidate_coverage"'), text.index('CURRENT_STEP="owner_email_send_guard"'))
        self.assertLess(text.index('CURRENT_STEP="owner_email_send_guard"'), text.index('CURRENT_STEP="non_native_owner_email_packet_send"'))
        self.assertLess(text.index('CURRENT_STEP="non_native_owner_email_packet_send"'), text.index('CURRENT_STEP="owner_email_diagnostic"'))
        self.assertLess(text.index('[ "$OWNER_EMAIL_SEND_GUARD_SEND_ALLOWED" != "1" ]'), text.index('CURRENT_STEP="owner_email_send"'))

    def test_transfer_reconciliation_uses_refreshed_lofty_manager_response(self):
        text = SCRIPT.read_text(encoding="utf-8")
        transfer_block = text[
            text.index('CURRENT_STEP="transfer_reconciliation"') : text.index(
                'CURRENT_STEP="transfer_reconciliation_telegram"'
            )
        ]
        self.assertIn('--lofty-manager-properties-response "$LOFTY_MANAGER_PROPERTIES_RESPONSE_FILE"', transfer_block)

    def test_listing_cleanup_queue_is_review_only_after_publish(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('BUILD_LOFTY_LISTING_CLEANUP_QUEUE="${BUILD_LOFTY_LISTING_CLEANUP_QUEUE:-1}"', text)
        self.assertIn('LOFTY_LISTING_CLEANUP_QUEUE_FILE="$REPORT_DIR/lofty_listing_update_cleanup_queue.json"', text)
        self.assertIn('LOFTY_LISTING_CLEANUP_QUEUE_STATUS="not_started"', text)
        self.assertIn('CURRENT_STEP="lofty_listing_cleanup_queue"', text)
        self.assertIn('LOFTY_LISTING_CLEANUP_QUEUE_SCRIPT="$ROOT/scripts/lofty_listing_update_cleanup_queue.py"', text)
        self.assertIn('--live-update-capture-report "$LIVE_UPDATE_CAPTURE_FILE"', text)
        self.assertIn('--runtime-map "$LOFTY_PM_RUNTIME_MAP"', text)
        self.assertIn('--report "$LOFTY_LISTING_CLEANUP_QUEUE_FILE"', text)
        self.assertIn('--publish-script "$LOFTY_PM_SKILL_PUBLISH_SCRIPT"', text)
        self.assertIn('--listing-cleanup-queue-report "$LOFTY_LISTING_CLEANUP_QUEUE_FILE"', text)
        self.assertIn('"lofty_listing_cleanup_queue": os.environ.get("BASELANE_MONTHLY_LOFTY_LISTING_CLEANUP_QUEUE_STATUS")', text)
        self.assertIn('"lofty_listing_cleanup_queue": os.environ.get("BASELANE_MONTHLY_LOFTY_LISTING_CLEANUP_QUEUE_FILE")', text)
        cleanup_block = text[
            text.index('CURRENT_STEP="lofty_listing_cleanup_queue"') : text.index('CURRENT_STEP="discord_property_update"')
        ]
        self.assertNotIn("--apply", cleanup_block)
        self.assertNotIn("--send", cleanup_block)
        self.assertIn('LOFTY_LISTING_CLEANUP_QUEUE_STATUS="skipped_publish_disabled"', cleanup_block)
        self.assertIn('"status": "skipped_publish_disabled"', cleanup_block)
        self.assertIn('"PUBLISH_LOFTY_PM_UPDATES disabled; no live listing cleanup queue is needed"', cleanup_block)
        self.assertLess(text.index('CURRENT_STEP="lofty_pm_publish"'), text.index('CURRENT_STEP="lofty_listing_cleanup_queue"'))
        self.assertLess(text.index('CURRENT_STEP="lofty_listing_cleanup_queue"'), text.index('CURRENT_STEP="owner_email_send_guard"'))

    def test_monthly_cron_blocks_on_accruals_and_sends_transfer_reconciliation(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('CURRENT_STEP="baselane_monthly_finance_truth_refresh"', text)
        self.assertLess(
            text.index('CURRENT_STEP="baselane_monthly_finance_truth_refresh"'),
            text.index('CURRENT_STEP="monthly_lofty_updates"'),
        )
        finance_truth_block = text[
            text.index('CURRENT_STEP="baselane_monthly_finance_truth_refresh"') : text.index('CURRENT_STEP="future_cf_values_cleanup"')
        ]
        self.assertIn('RUN_BASELANE_MONTHLY_FINANCE_TRUTH_AUTH_PREFLIGHT="${RUN_BASELANE_MONTHLY_FINANCE_TRUTH_AUTH_PREFLIGHT:-1}"', text)
        self.assertIn('BASELANE_AUTH_RECOVERY_REPORT_FILE="${BASELANE_AUTH_RECOVERY_REPORT_FILE:-$REPORT_DIR/baselane_auth_recovery_report.json}"', text)
        self.assertIn('BASELANE_AUTH_RECOVERY_SCRIPT="$ROOT/scripts/baselane_cdp_auth_recovery.py"', finance_truth_block)
        self.assertIn('--recover-login', finance_truth_block)
        self.assertIn('--graphql-auth-smoke', finance_truth_block)
        self.assertIn('"failed_step": "baselane_auth_preflight"', finance_truth_block)
        self.assertIn('"issue_summary": issue_summary', finance_truth_block)
        self.assertIn('"next_action": next_action', finance_truth_block)
        self.assertIn('"safe_to_retry_after_manual_auth": bool(', finance_truth_block)
        self.assertIn('RUN_MONTH={run_month} bash scripts/baselane_monthly_finance_truth_refresh.sh', finance_truth_block)
        self.assertIn('bash scripts/baselane_financials_post_auth_resume.sh', finance_truth_block)
        self.assertIn('"auth_recovery_issue_summary": auth_report.get("issue_summary")', finance_truth_block)
        self.assertIn('"auth_recovery_manual_auth_reason": auth_report.get("manual_auth_reason")', finance_truth_block)
        self.assertIn('login_wait_path = report_path.parent / "baselane_login_wait_report.json"', finance_truth_block)
        self.assertIn('login_wait_reason == "baselane_login_recaptcha_required"', finance_truth_block)
        self.assertIn('"baselane_login_wait_recaptcha_present": login_wait.get("recaptcha_present") is True', finance_truth_block)
        self.assertIn('"Baselane reCAPTCHA is visible in the CDP login tab; monthly finance-truth cannot safely run."', finance_truth_block)
        self.assertIn('"monthly_finance_truth_refresh_auth_recovery_status": monthly_finance_truth_refresh.get("auth_recovery_status")', text)
        self.assertIn('"monthly_finance_truth_refresh_auth_recovery_issue_summary": monthly_finance_truth_refresh.get("auth_recovery_issue_summary")', text)
        self.assertIn('"monthly_finance_truth_refresh_auth_recovery_manual_auth_required": monthly_finance_truth_refresh.get("auth_recovery_manual_auth_required") is True', text)
        self.assertIn('"monthly_finance_truth_refresh_auth_recovery_manual_auth_reason": monthly_finance_truth_refresh.get("auth_recovery_manual_auth_reason")', text)
        self.assertIn('"monthly_finance_truth_refresh_auth_recovery_login_attempt_count": monthly_finance_truth_refresh.get("auth_recovery_login_attempt_count")', text)
        self.assertIn('"monthly_finance_truth_refresh_auth_recovery_hard_refresh_attempt_count": monthly_finance_truth_refresh.get("auth_recovery_hard_refresh_attempt_count")', text)
        self.assertIn('"monthly_finance_truth_refresh_auth_recovery_opened_statement_tab_count": monthly_finance_truth_refresh.get("auth_recovery_opened_statement_tab_count")', text)
        self.assertIn('"monthly_finance_truth_refresh_auth_recovery_next_action": monthly_finance_truth_refresh.get("auth_recovery_next_action")', text)
        self.assertIn('"monthly_finance_truth_refresh_issue_summary": monthly_finance_truth_refresh.get("issue_summary")', text)
        self.assertIn('"monthly_finance_truth_refresh_next_action": monthly_finance_truth_refresh.get("next_action")', text)
        self.assertIn('"monthly_finance_truth_refresh_safe_to_retry_after_manual_auth": monthly_finance_truth_refresh.get("safe_to_retry_after_manual_auth") is True', text)
        self.assertIn('finance_refresh_action = str(report.get("monthly_finance_truth_refresh_next_action") or "").strip()', text)
        self.assertIn('finance_auth_reason = str(report.get("monthly_finance_truth_refresh_auth_recovery_manual_auth_reason") or "")', text)
        self.assertIn('"recovery_attempted_but_baselane_loading_appcheck"', text)
        self.assertIn('f"{finance_auth_issue} Then rerun monthly finance-truth refresh before downstream CF/FINANCIALS/Lofty/Discord/email outputs."', text)
        self.assertLess(
            finance_truth_block.index('baselane_cdp_auth_recovery.py'),
            finance_truth_block.index('timeout --kill-after=30s "$MONTHLY_FINANCE_TRUTH_REFRESH_TIMEOUT_SECONDS" "$MONTHLY_FINANCE_TRUTH_REFRESH_SCRIPT"'),
        )
        self.assertIn(
            'APPLY_BASELANE_MONTHLY_ACCRUALS_LIVE="${APPLY_BASELANE_MONTHLY_ACCRUALS_LIVE:-$BASELANE_MONTHLY_LIVE_ACTIONS_APPROVED}"',
            text,
        )
        self.assertIn('finance_truth_apply_live=0', text)
        self.assertIn('finance_truth_validate_only=1', text)
        self.assertIn('[ "$BASELANE_MONTHLY_LIVE_ACTIONS_APPROVED" = "1" ]', text)
        self.assertIn('APPLY_BASELANE_MONTHLY_ACCRUALS_LIVE="$finance_truth_apply_live"', text)
        self.assertIn('BASELANE_MONTHLY_FINANCE_TRUTH_VALIDATE_ONLY="$finance_truth_validate_only"', text)
        self.assertIn('REQUIRE_MONTHLY_ACCRUAL_COMPLETENESS="${REQUIRE_MONTHLY_ACCRUAL_COMPLETENESS:-1}"', text)
        self.assertIn('SEND_TRANSFER_RECONCILIATION_TELEGRAM="${SEND_TRANSFER_RECONCILIATION_TELEGRAM:-0}"', text)
        self.assertIn('CURRENT_STEP="monthly_accruals_completeness"', text)
        self.assertIn('BASELANE_MONTHLY_ACCRUALS_MONTH="$RUN_MONTH"', text)
        self.assertIn('monthly accrual completeness failed for $RUN_MONTH', text)
        self.assertIn('MONTHLY_ACCRUALS_MISSING_FIXED_COVERAGE_COUNT', text)
        self.assertIn('MONTHLY_ACCRUALS_UNAPPROVED_PM_FEE_BASIS_GAP_COUNT', text)
        self.assertNotIn('|| [ "$MONTHLY_ACCRUALS_MISSING_COUNT" -gt 0 ]', text)
        self.assertIn('missing_fixed_coverage=${MONTHLY_ACCRUALS_MISSING_FIXED_COVERAGE_COUNT:-0}', text)
        self.assertIn('unapproved_pm_fee_basis_gaps=${MONTHLY_ACCRUALS_UNAPPROVED_PM_FEE_BASIS_GAP_COUNT:-0}', text)

    def test_monthly_cron_does_not_bypass_financial_data_gate_for_corrective_publish(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('MONTHLY_FINANCIAL_CORRECTIVE_PUBLISH_ALLOWED="0"', text)
        self.assertIn('MONTHLY_FINANCIAL_CORRECTIVE_PUBLISH_ALLOWED="$($PY - "$READINESS_FILE" "$LOFTY_FINANCIAL_PATCH_READINESS_FILE"', text)
        self.assertIn('primary_class == "live_financial_capture.review"', text)
        self.assertIn('patch_status == "ok"', text)
        self.assertIn('blocked == 0', text)
        self.assertIn('ready >= max(property_count, 1)', text)
        self.assertIn('"monthly_financial_corrective_publish_allowed"', text)
        publish_gate = text[
            text.index('MONTHLY_LIVE_PUBLISH_ALLOWED="0"') : text.index('if [ ! -f "$LOFTY_PM_PUBLISH_SCRIPT" ]')
        ]
        self.assertIn('[ "$MONTHLY_FINANCIAL_DATA_PUBLISH_ALLOWED" = "1" ]', publish_gate)
        self.assertNotIn('[ "$MONTHLY_FINANCIAL_CORRECTIVE_PUBLISH_ALLOWED" = "1" ]', publish_gate)
        self.assertLess(
            text.index('if [ "$SEND_OWNER_EMAILS" = "1" ] && [ "$MONTHLY_READINESS_OWNER_EMAIL_ALLOWED" != "1" ]; then'),
            text.index('CURRENT_STEP="lofty_pm_publish"'),
        )
        self.assertIn('CURRENT_STEP="transfer_reconciliation"', text)
        self.assertIn('CURRENT_STEP="cf_balance_sheet_consistency"', text)
        self.assertIn('baselane_cf_balance_sheet_consistency_audit.py', text)
        self.assertIn('YHOME_MISSING_CANDIDATES_CSV="${YHOME_MISSING_CANDIDATES_CSV:-$REPORT_DIR/yhome_missing_candidates.csv}"', text)
        self.assertIn('--yhome-missing-candidates-csv "$YHOME_MISSING_CANDIDATES_CSV"', text)
        self.assertIn('--require-all-yhome-rows', text)
        self.assertNotIn('--allow-missing-yhome-rows', text)
        yhome_block = text[
            text.index('CURRENT_STEP="yhome_operating_cash_apply_verify"') : text.index('CURRENT_STEP="quitman_804_cash_alignment"')
        ]
        self.assertIn('--yhome-gws-spreadsheet-id "$YHOME_GWS_SPREADSHEET_ID"', yhome_block)
        self.assertIn('--yhome-gws-sheet-spec "Cleveland=1187056671"', yhome_block)
        self.assertIn('--yhome-gws-sheet-spec "Chicago & non-Yhome=433920866"', yhome_block)
        self.assertIn('--yhome-gws-sheet-spec "Yhome Deeded & Sold=1902489452"', yhome_block)
        self.assertIn('CURRENT_STEP="yhome_operating_cash_apply_verify"', text)
        self.assertIn('yhome_operating_cash_apply_verify.py', text)
        self.assertIn('CURRENT_STEP="quitman_804_cash_alignment"', text)
        self.assertIn('audit_804_quitman_cash_alignment.py', text)
        self.assertIn('QUITMAN_804_CASH_ALIGNMENT_DECISIONS_FILE="${QUITMAN_804_CASH_ALIGNMENT_DECISIONS_FILE:-$ROOT/config/baselane_804_quitman_cash_alignment_decisions.json}"', text)
        self.assertIn('QUITMAN_804_CASH_ALIGNMENT_REVIEWED_TEMPLATE_FILE="${QUITMAN_804_CASH_ALIGNMENT_REVIEWED_TEMPLATE_FILE:-$ROOT/config/baselane_804_quitman_cash_alignment_reviewed_template.json}"', text)
        self.assertIn('QUITMAN_804_CASH_ALIGNMENT_PROPOSED_DECISIONS_FILE="${QUITMAN_804_CASH_ALIGNMENT_PROPOSED_DECISIONS_FILE:-$REPORT_DIR/baselane_804_quitman_cash_alignment_proposed_decisions.json}"', text)
        self.assertIn('QUITMAN_804_CASH_ALIGNMENT_REVIEW_QUEUE_CSV="${QUITMAN_804_CASH_ALIGNMENT_REVIEW_QUEUE_CSV:-$REPORT_DIR/baselane_804_quitman_cash_alignment_review_queue.csv}"', text)
        self.assertIn('QUITMAN_804_CASH_ALIGNMENT_GROUP_REVIEW_QUEUE_CSV="${QUITMAN_804_CASH_ALIGNMENT_GROUP_REVIEW_QUEUE_CSV:-$REPORT_DIR/baselane_804_quitman_cash_alignment_group_review_queue.csv}"', text)
        self.assertIn('QUITMAN_804_CASH_ALIGNMENT_IMPORT_COMMANDS_FILE="${QUITMAN_804_CASH_ALIGNMENT_IMPORT_COMMANDS_FILE:-$REPORT_DIR/baselane_804_quitman_cash_alignment_import_group_review.requires-explicit-approval.sh}"', text)
        self.assertIn('QUITMAN_804_CASH_ALIGNMENT_CANDIDATE_DECISIONS_FILE="${QUITMAN_804_CASH_ALIGNMENT_CANDIDATE_DECISIONS_FILE:-$REPORT_DIR/baselane_804_quitman_cash_alignment_candidate_decisions.json}"', text)
        self.assertIn('QUITMAN_804_CASH_ALIGNMENT_DECISION_TEMPLATE_FILE="${QUITMAN_804_CASH_ALIGNMENT_DECISION_TEMPLATE_FILE:-$REPORT_DIR/baselane_804_quitman_cash_alignment_decision_template.json}"', text)
        self.assertIn('QUITMAN_804_CASH_ALIGNMENT_REVIEW_MARKDOWN_FILE="${QUITMAN_804_CASH_ALIGNMENT_REVIEW_MARKDOWN_FILE:-$REPORT_DIR/baselane_804_quitman_cash_alignment_review.md}"', text)
        self.assertIn('QUITMAN_804_CASH_ALIGNMENT_DECISION_VALIDATION_FILE="${QUITMAN_804_CASH_ALIGNMENT_DECISION_VALIDATION_FILE:-$REPORT_DIR/baselane_804_quitman_cash_alignment_decision_validation.json}"', text)
        self.assertIn('--decisions "$QUITMAN_804_CASH_ALIGNMENT_DECISIONS_FILE"', text)
        self.assertIn('--review-queue-csv "$QUITMAN_804_CASH_ALIGNMENT_REVIEW_QUEUE_CSV"', text)
        self.assertIn('--group-review-queue-csv "$QUITMAN_804_CASH_ALIGNMENT_GROUP_REVIEW_QUEUE_CSV"', text)
        self.assertIn('--import-commands "$QUITMAN_804_CASH_ALIGNMENT_IMPORT_COMMANDS_FILE"', text)
        self.assertIn('--candidate-decisions "$QUITMAN_804_CASH_ALIGNMENT_CANDIDATE_DECISIONS_FILE"', text)
        self.assertIn('--decision-validation "$QUITMAN_804_CASH_ALIGNMENT_DECISION_VALIDATION_FILE"', text)
        self.assertIn('CURRENT_STEP="source_cash_reconciliation_actions"', text)
        self.assertIn('baselane_source_cash_reconciliation_actions.py', text)
        self.assertIn('--source-cash-report "$DAILY_SOURCE_CASH_BALANCE_REPORT_FILE"', text)
        self.assertIn('SOURCE_CASH_RECONCILIATION_ACTIONS_CSV="${SOURCE_CASH_RECONCILIATION_ACTIONS_CSV:-$REPORT_DIR/baselane_source_cash_reconciliation_actions.csv}"', text)
        self.assertIn('ZERO_ROW_SOURCE_LEDGER_DECISIONS_FILE="${ZERO_ROW_SOURCE_LEDGER_DECISIONS_FILE:-$ROOT/config/baselane_zero_row_source_ledger_decisions.json}"', text)
        self.assertIn('--zero-row-decisions "$ZERO_ROW_SOURCE_LEDGER_DECISIONS_FILE"', text)
        self.assertIn('--csv "$SOURCE_CASH_RECONCILIATION_ACTIONS_CSV"', text)
        self.assertIn('--source-cash-reconciliation-actions "$SOURCE_CASH_RECONCILIATION_ACTIONS_FILE"', text)
        self.assertIn('--yhome-update-plan-csv "$YHOME_OPERATING_CASH_UPDATE_PLAN_CSV"', text)
        self.assertIn('refresh_monthly_readiness_report()', text)
        self.assertIn('CURRENT_STEP="monthly_readiness_report_post_reconciliation"', text)
        self.assertIn('BASELANE_MONTHLY_YHOME_OPERATING_CASH_APPLY_VERIFY_STATUS', text)
        self.assertIn('--cf-balance-sheet-report "$CF_BALANCE_SHEET_CONSISTENCY_FILE"', text)
        self.assertIn('baselane_lofty_transfer_requirements.py', text)
        self.assertIn('--monthly-accruals-report "$MONTHLY_ACCRUALS_REPORT_FILE"', text)
        self.assertIn('--property-cash-review-report "$QUITMAN_804_CASH_ALIGNMENT_FILE"', text)
        self.assertIn('--decision-template "$QUITMAN_804_CASH_ALIGNMENT_DECISION_TEMPLATE_FILE"', text)
        self.assertIn('--review-markdown "$QUITMAN_804_CASH_ALIGNMENT_REVIEW_MARKDOWN_FILE"', text)
        self.assertIn('--reviewed-template "$QUITMAN_804_CASH_ALIGNMENT_REVIEWED_TEMPLATE_FILE"', text)
        self.assertIn('--proposed-decisions "$QUITMAN_804_CASH_ALIGNMENT_PROPOSED_DECISIONS_FILE"', text)
        self.assertIn('CURRENT_STEP="transfer_reconciliation_telegram"', text)
        self.assertIn('send_monthly_transfer_reconciliation_telegram.py', text)
        self.assertIn('TRANSFER_RECONCILIATION_TELEGRAM_MARKDOWN="${TRANSFER_RECONCILIATION_TELEGRAM_MARKDOWN:-$REPORT_DIR/baselane_lofty_transfer_requirements.telegram.md}"', text)
        self.assertIn('--message "$TRANSFER_RECONCILIATION_TELEGRAM_MARKDOWN"', text)
        self.assertIn('--transfer-report "$TRANSFER_RECONCILIATION_FILE"', text)
        self.assertIn('TRANSFER_RECONCILIATION_EXPECTED_DIGEST="$($PY - "$TRANSFER_RECONCILIATION_FILE"', text)
        self.assertIn('--expected-transfer-report-digest "$TRANSFER_RECONCILIATION_EXPECTED_DIGEST"', text)
        self.assertIn('"monthly_accruals"', text)
        self.assertIn('"quitman_804_cash_alignment"', text)
        self.assertIn('"quitman_804_cash_alignment_review_queue_csv"', text)
        self.assertIn('"quitman_804_cash_alignment_group_review_queue_csv"', text)
        self.assertIn('"quitman_804_cash_alignment_import_commands"', text)
        self.assertIn('"quitman_804_cash_alignment_candidate_decisions"', text)
        self.assertIn('"quitman_804_cash_alignment_decision_template"', text)
        self.assertIn('"quitman_804_cash_alignment_review_markdown"', text)
        self.assertIn('"quitman_804_cash_alignment_source_clean_status"', text)
        self.assertIn('source_clean_status={source_clean_status}', text)
        self.assertIn('"quitman_804_cash_alignment_reviewed_template"', text)
        self.assertIn('"quitman_804_cash_alignment_proposed_decisions"', text)
        self.assertIn('"quitman_804_cash_alignment_decision_validation"', text)
        self.assertIn('"source_cash_reconciliation_actions"', text)
        self.assertIn('"source_cash_reconciliation_actions_csv"', text)
        self.assertIn('"transfer_reconciliation"', text)
        self.assertIn('"transfer_reconciliation_telegram"', text)
        self.assertIn('"monthly_completion_evidence"', text)
        self.assertIn('"monthly_completion_gap_count"', text)
        self.assertIn('OPENCLAW_CRON_JOBS_FILE="${OPENCLAW_CRON_JOBS_FILE:-}"', text)
        self.assertIn('OPENCLAW_CRON_JOBS_FILE="$OPENCLAW_ROOT/cron/jobs.primary.json"', text)
        self.assertIn('BASELANE_MONTHLY_OPENCLAW_CRON_JOBS_FILE="$OPENCLAW_CRON_JOBS_FILE"', text)
        self.assertIn('BASELANE_MONTHLY_SYSTEMD_TIMER_FILE="$BASELANE_MONTHLY_SYSTEMD_TIMER_FILE"', text)
        self.assertIn('"systemd_monthly_timer_schedule_ok"', text)
        self.assertIn('"monthly_cron_schedule_ok"', text)
        self.assertIn('GUARDED_APPLY_TIMEOUT_SECONDS="${GUARDED_APPLY_TIMEOUT_SECONDS:-300}"', text)
        self.assertIn('MONTHLY_REVIEW_STEP_TIMEOUT_SECONDS="${MONTHLY_REVIEW_STEP_TIMEOUT_SECONDS:-300}"', text)
        self.assertIn('MONTHLY_FINANCE_TRUTH_REFRESH_TIMEOUT_SECONDS="${MONTHLY_FINANCE_TRUTH_REFRESH_TIMEOUT_SECONDS:-1200}"', text)
        self.assertIn('RUN_LOFTY_GUARDED_APPLY="${RUN_LOFTY_GUARDED_APPLY:-1}"', text)
        self.assertIn('LOFTY_GUARDED_APPLY_STATUS="failed_required_dry_run_disabled"', text)
        self.assertIn('RUN_LOFTY_GUARDED_APPLY is disabled for dry-run while REQUIRE_GUARDED_MONTHLY_APPLY is enabled', text)
        self.assertIn('elif [ "$APPLY_LOFTY_GUARDED_UPDATES" != "1" ]; then', text)
        self.assertIn('LOFTY_GUARDED_APPLY_STATUS="ok_not_applied"', text)
        self.assertIn('LOFTY_GUARDED_APPLY_STATUS="review_not_applied"', text)
        self.assertIn('LOFTY_GUARDED_APPLY_STATUS="failed_required_not_applied"', text)
        disabled_apply_block = text[
            text.index('elif [ "$APPLY_LOFTY_GUARDED_UPDATES" != "1" ]; then') : text.index(
                'elif [ ! -x "$GUARDED_APPLY_SCRIPT" ]'
            )
        ]
        self.assertIn('--report "$GUARDED_APPLY_FILE"', disabled_apply_block)
        self.assertNotIn('--apply', disabled_apply_block)
        self.assertIn('timeout --kill-after=30s "$GUARDED_APPLY_TIMEOUT_SECONDS" "$PY" "$GUARDED_APPLY_SCRIPT"', text)
        self.assertIn('timeout --kill-after=30s "$MONTHLY_REVIEW_STEP_TIMEOUT_SECONDS" "$PY" "$REVIEW_MANIFEST_SCRIPT"', text)
        self.assertIn('timeout --kill-after=30s "$MONTHLY_FINANCE_TRUTH_REFRESH_TIMEOUT_SECONDS" "$MONTHLY_FINANCE_TRUTH_REFRESH_SCRIPT"', text)
        self.assertIn('LOFTY_REVIEW_MANIFEST_STATUS="failed_timeout"', text)
        self.assertIn('"yhome_operating_cash_apply_verify"', text)
        self.assertIn('"source_cash_reconciliation_action_status"', text)
        self.assertIn('"source_cash_reconciliation_active_monthly_candidate_action_count"', text)
        self.assertIn('"source_cash_reconciliation_active_monthly_candidate_source_cash_mismatch_count"', text)
        self.assertIn('"source_cash_reconciliation_active_monthly_candidate_source_cash_abs_delta_total"', text)
        self.assertIn('"source_cash_reconciliation_largest_active_monthly_candidate_mismatches"', text)
        self.assertIn('"transfer_reconciliation_total"', text)
        self.assertIn('"transfer_reconciliation_source_clean_and_final"', text)
        self.assertIn('"transfer_reconciliation_bank_actions"', text)
        self.assertIn('"transfer_reconciliation_recommended_total_is_final"', text)
        self.assertIn('"transfer_reconciliation_source_blocker_count"', text)
        self.assertIn('"transfer_reconciliation_source_blocker_summary"', text)
        self.assertIn('"transfer_reconciliation_required_source_blockers"', text)
        self.assertIn('"transfer_reconciliation_required_source_blocker_count"', text)
        self.assertIn('"transfer_reconciliation_property_cash_review_blockers"', text)
        self.assertIn('"transfer_reconciliation_property_cash_review_details"', text)
        self.assertIn('report.get("transfer_reconciliation_recommended_total_is_final") is True', text)
        self.assertIn('not report.get("transfer_reconciliation_property_cash_review_blockers")', text)
        self.assertIn('"telegram_transfer_reconciliation_delivery"', text)
        self.assertIn('"transfer_reconciliation_telegram_message_quality_ok"', text)
        self.assertIn('"message_quality_ok": report.get("transfer_reconciliation_telegram_message_quality_ok")', text)
        self.assertIn('"message_quality_issues": report.get("transfer_reconciliation_telegram_message_quality_issues")', text)
        self.assertIn('"source_blocker_summary": report.get("transfer_reconciliation_telegram_source_blocker_summary")', text)
        self.assertIn('BASELANE_MONTHLY_CRON_SCRIPT_FILE="$0"', text)
        self.assertIn('MONTHLY_CHAIN_REQUIRED_ORDER = [', text)
        self.assertIn('"monthly_chain_ordering": monthly_chain_ordering(', text)
        self.assertIn('"policy": "Monthly transfer Telegram runs after CF/source sync and before Discord review; Lofty publish and financial patch readiness run before Discord posts; Discord review runs before owner email guard/send; owner email is final."', text)
        self.assertLess(text.index('CURRENT_STEP="monthly_accruals_completeness"'), text.index('CURRENT_STEP="transfer_reconciliation"'))
        self.assertLess(text.index('CURRENT_STEP="cf_balance_sheet_consistency"'), text.index('CURRENT_STEP="yhome_operating_cash_apply_verify"'))
        self.assertLess(text.index('CURRENT_STEP="yhome_operating_cash_apply_verify"'), text.index('CURRENT_STEP="quitman_804_cash_alignment"'))
        self.assertLess(text.index('CURRENT_STEP="quitman_804_cash_alignment"'), text.index('CURRENT_STEP="source_cash_reconciliation_actions"'))
        self.assertLess(text.index('CURRENT_STEP="monthly_accruals_completeness"'), text.index('CURRENT_STEP="source_cash_reconciliation_actions"'))
        self.assertLess(text.index('CURRENT_STEP="source_cash_reconciliation_actions"'), text.index('CURRENT_STEP="transfer_reconciliation"'))
        self.assertLess(text.index('CURRENT_STEP="transfer_reconciliation"'), text.index('CURRENT_STEP="transfer_reconciliation_telegram"'))
        self.assertLess(text.index('CURRENT_STEP="transfer_reconciliation_telegram"'), text.index('CURRENT_STEP="lofty_pm_publish"'))
        self.assertLess(text.index('CURRENT_STEP="transfer_reconciliation_telegram"'), text.index('CURRENT_STEP="monthly_readiness_report_post_reconciliation"'))
        self.assertLess(text.index('CURRENT_STEP="monthly_readiness_report_post_reconciliation"'), text.index('CURRENT_STEP="discord_all_send_plan"'))
        self.assertLess(text.index('CURRENT_STEP="discord_all_send_plan"'), text.index('CURRENT_STEP="lofty_pm_publish"'))
        self.assertLess(text.index('CURRENT_STEP="lofty_pm_publish"'), text.index('CURRENT_STEP="discord_property_update"'))
        self.assertLess(
            text.index('DISCORD_ALL_SEND_PLAN_BLOCKED_REASON="$(discord_all_send_plan_blocked_reason)"', text.index('CURRENT_STEP="discord_all_send_plan"')),
            text.index('CURRENT_STEP="lofty_pm_publish"'),
        )

    def test_monthly_run_report_contains_requirement_level_completion_audit(self):
        text = SCRIPT.read_text(encoding="utf-8")

        for requirement in [
            "monthly_cron_scheduled_for_15th",
            "baselane_raw_daily_sync_fresh_and_effective",
            "daily_source_cash_balance_applied_clean",
            "monthly_statements_and_cf_sync",
            "accrual_completeness_guard",
            "source_cash_reconciliation_actions_clear",
            "lofty_live_listing_financial_update",
            "financials_md_to_lofty_listing_field_readiness",
            "discord_property_update",
            "monthly_active_candidate_set_coverage",
            "owner_email_packet_and_send_guard",
            "yhome_operating_cash_apply_verify",
            "transfer_reconciliation_total",
            "transfer_reconciliation_source_clean_and_final",
            "transfer_reconciliation_bank_actions",
            "telegram_transfer_reconciliation_delivery",
        ]:
            self.assertIn(requirement, text)
        self.assertIn("transfer_reconciliation_bank_action_counts", text)
        self.assertIn("transfer_reconciliation_source_blocker_count", text)
        self.assertIn("transfer_reconciliation_source_blocker_summary", text)
        self.assertIn('"daily_sync_effective_status"', text)
        self.assertIn('"daily_sync_source_transaction_index_status"', text)
        self.assertIn('"daily_source_cash_balance_violation_count"', text)
        self.assertIn('"daily_source_cash_balance_split_scope_missing_property_count"', text)
        self.assertIn('"split_scope_missing_properties": report.get("daily_source_cash_balance_split_scope_missing_properties")', text)
        self.assertIn("active monthly scope", text)
        self.assertIn('"daily_source_cash_balance_update_count"', text)
        self.assertIn('"daily_source_cash_balance_violation_properties"', text)
        self.assertIn('"daily_source_cash_balance_update_samples"', text)
        self.assertIn('"daily_source_cash_balance_policy"', text)
        self.assertIn('"daily_sync_report": daily_sync_path or None', text)
        self.assertIn('"daily_source_cash_balance": daily_source_cash_balance_path or None', text)
        self.assertIn('"source_cash_reconciliation_actions": source_cash_reconciliation_actions_path or None', text)
        self.assertIn('"action_report": report["artifacts"].get("source_cash_reconciliation_actions")', text)
        self.assertIn('"action_csv": report["artifacts"].get("source_cash_reconciliation_actions_csv")', text)
        self.assertIn('"largest_active_monthly_candidate_mismatches": report.get("source_cash_reconciliation_largest_active_monthly_candidate_mismatches")', text)
        self.assertIn('"source_cash_reconciliation_actions_clear": "Resolve active source-cash reconciliation actions before transfer reconciliation, Discord review, Lofty publish, or email."', text)
        self.assertIn('report["steps"]["quitman_804_cash_alignment"] = "ok"', text)
        self.assertIn('"quitman_804_cash_alignment_active_transfer_blocking_status"', text)
        self.assertIn('"financials_md_to_lofty_listing_field_readiness"', text)
        self.assertIn('"lofty_pm_publish_publish_attempted"', text)
        self.assertIn('"lofty_pm_publish_result_count"', text)
        self.assertIn('"lofty_pm_publish_failed_count"', text)
        self.assertIn('"lofty_pm_publish_financial_publish_failed_count"', text)
        self.assertIn('report.get("lofty_pm_publish_publish_attempted") is True', text)
        self.assertIn('report.get("lofty_pm_publish_result_count", 0) > 0', text)
        self.assertIn('report.get("lofty_pm_publish_failed_count") == 0', text)
        self.assertIn('report.get("lofty_financial_patch_guard_reconcile_required_count") == 0', text)
        self.assertIn('report.get("lofty_financial_patch_field_count_total", 0) > 0', text)
        self.assertIn('report.get("lofty_financial_patch_candidate_packet_missing_runtime_count") == 0', text)
        self.assertIn('report.get("lofty_financial_patch_candidate_packet_monthly_summary_issue_count") == 0', text)
        self.assertIn('report.get("lofty_financial_patch_runtime_missing_candidate_count") == 0', text)
        self.assertIn('report.get("lofty_financial_patch_runtime_monthly_summary_issue_count") == 0', text)
        self.assertIn('bool(report.get("transfer_reconciliation_bank_action_counts"))', text)
        self.assertIn("monthly_completion_gaps = [item for item in monthly_completion_evidence if not item", text)
        self.assertIn('report["monthly_completion_gaps"] = monthly_completion_gaps', text)
        self.assertIn('"monthly_completion_next_actions"', text)
        self.assertIn('"monthly_blocker_command_index"', text)
        self.assertIn('BLOCKER_COMMAND_MARKDOWN_FILE="$REPORT_DIR/baselane_financials_monthly_blocker_command_index.md"', text)
        self.assertIn('MONTHLY_CLOSE_STATUS_FILE="$REPORT_DIR/baselane_financials_monthly_close_status.json"', text)
        self.assertIn('MONTHLY_CLOSE_STATUS_MARKDOWN_FILE="$REPORT_DIR/baselane_financials_monthly_close_status.md"', text)
        self.assertIn('"monthly_blocker_command_index_markdown"', text)
        self.assertIn('"monthly_close_status_report"', text)
        self.assertIn('"monthly_close_status_markdown"', text)
        self.assertIn('"monthly_close_status_write_status"', text)
        self.assertIn('"monthly_close_status_write_error"', text)
        self.assertIn('"monthly_close_status_artifact_written"', text)
        self.assertIn('monthly close status artifact was not written', text)
        self.assertIn('import baselane_monthly_close_status as close_status_writer', text)
        self.assertIn('close_status_writer.write_monthly_close_status(', text)
        self.assertIn('def build_blocker_command_markdown(report: dict[str, Any]) -> str:', text)
        self.assertIn('"baselane_raw_daily_sync_fresh_and_effective": [', text)
        self.assertIn('"daily_sync_report"', text)
        self.assertIn('"bash scripts/baselane_cron_run.sh && python3 scripts/baselane_daily_sync_report.py"', text)
        self.assertIn('f"bash scripts/baselane_financials_post_auth_resume.sh --month {run_month}"', text)
        self.assertIn('f"python3 scripts/baselane_monthly_accruals_idempotent.py --month {run_month}"', text)
        self.assertIn('"python3 scripts/audit_monthly_pipeline_candidate_coverage.py"', text)
        self.assertIn('f"python3 scripts/yhome_operating_cash_apply_verify.py --month {run_month}"', text)
        self.assertIn('f"python3 scripts/baselane_lofty_transfer_requirements.py --month {run_month}"', text)
        self.assertIn('"python3 scripts/send_monthly_transfer_reconciliation_telegram.py --dry-run"', text)
        self.assertIn('"OPEN reports/baselane_804_quitman_cash_alignment_review.md"', text)
        self.assertIn('"discord_property_update": [', text)
        self.assertIn('"discord_all_send_plan_validation"', text)
        self.assertIn('send_monthly_discord_property_update.py', text)
        self.assertIn('"--plan reports/baselane_financials_monthly_discord_all_send_plan.json "', text)
        self.assertNotIn('"--guild-report reports/baselane_financials_monthly_guild_test_post.json "', text)
        self.assertIn('if name == "discord_property_update":', text)
        self.assertIn('"safe_to_run_automatically_reason": "live Discord messages require an explicit operator decision"', text)
        self.assertIn('lofty_pm_publish_current_for_run=false', text)
        self.assertIn('"lofty_live_listing_financial_update": [', text)
        self.assertIn('"lofty_pm_runtime_map"', text)
        self.assertIn('"SEND_MONTHLY_DISCORD_PROPERTY_UPDATE=0 "', text)
        self.assertIn('"PUBLISH_LOFTY_PM_UPDATES=1 "', text)
        self.assertIn('"APPLY_LOFTY_GUARDED_UPDATES=1 "', text)
        self.assertIn('"RUN_LOFTY_GUARDED_APPLY=1 "', text)
        self.assertIn('"owner_email_packet_and_send_guard": [', text)
        self.assertIn('f"python3 scripts/lofty_monthly_owner_email_packet.py --dry-run --run-month {run_month} "', text)
        self.assertIn('"--live-update-capture-report reports/baselane_financials_monthly_live_update_capture.json "', text)
        self.assertIn('"--listing-cleanup-queue-report reports/lofty_listing_update_cleanup_queue.json "', text)
        self.assertIn('" && python3 scripts/baselane_owner_email_send_guard.py --report reports/baselane_monthly_owner_email_send_guard.json"', text)
        self.assertIn('"owner_email_final_financial_gate": [', text)
        self.assertIn('"python3 scripts/baselane_owner_email_send_guard.py --report reports/baselane_monthly_owner_email_send_guard.json"', text)
        self.assertIn('LOFTY_PM_RUNTIME_MAP="$LOFTY_PM_RUNTIME_MAP"', text)
        self.assertIn('"lofty_pm_runtime_map": os.environ.get("LOFTY_PM_RUNTIME_MAP")', text)
        self.assertIn('"transfer_reconciliation_telegram_current_for_transfer": report.get("transfer_reconciliation_telegram_current_for_transfer")', text)
        self.assertIn('"lofty_financial_patch_blocker_markdown": report.get("lofty_financial_patch_blocker_markdown")', text)
        self.assertIn('"owner_email_transfer_telegram_digest_matches_current": report.get("owner_email_send_guard_transfer_telegram_transfer_report_digest_matches_current")', text)
        self.assertIn("Telegram transfer current:", text)
        self.assertIn("Lofty financial blocker packet:", text)
        self.assertIn("Owner email transfer Telegram blocked:", text)
        self.assertIn("Final Lofty publish/email gate clear:", text)
        self.assertIn("CF balance-sheet consistency:", text)
        self.assertIn("Yhome apply/verify:", text)
        self.assertIn("Yhome pre/post/applied updates:", text)
        self.assertIn('"### Accrual Gap Action Queue"', text)
        self.assertIn('"### Accrual Append Audit"', text)
        self.assertIn('"### 804 Next Review Groups"', text)
        self.assertIn('"### Property Cash Review Details"', text)
        self.assertIn('property_cash_review_high_priority_unresolved_sum', text)
        self.assertIn('No top blockers recorded.', text)
        self.assertIn('Key artifacts:', text)
        self.assertIn('for key, value in list(artifacts.items())[:10]', text)
        self.assertIn('"ready_to_run"', text)
        self.assertIn('"preflight_status"', text)
        self.assertIn('"safe_to_run_automatically"', text)
        self.assertIn('BASELANE_85104_PRECLOSING_RETAG_APPLY_DIGEST_mismatch', text)
        self.assertIn('BASELANE_85104_PRECLOSING_RETAG_APPLY_DIGEST_not_provided', text)
        self.assertIn('baselane_85104_preclosing_retag_partial_apply_will_not_clear_validation_without_protected_rows', text)
        self.assertIn('"partial_command_artifact"', text)
        self.assertIn('"bash reports/baselane_85104_preclosing_property_retag_apply.requires-explicit-approval.sh"', text)
        self.assertIn('"quitman_804_cash_alignment_review_markdown"', text)
        self.assertIn('"quitman_804_cash_alignment_source_clean_status"', text)
        self.assertIn('source_clean_status={source_clean_status}', text)
        self.assertIn('"transfer_reconciliation_source_clean_and_final": "Resolve property cash review and source-clean blockers; rerun transfer reconciliation."', text)
        self.assertIn('"transfer_reconciliation_source_clean_and_final": (', text)
        self.assertIn('"Refresh required source artifacts, resolve property cash/accrual blockers, then rerun transfer reconciliation."', text)
        self.assertIn('report.get("transfer_reconciliation_property_cash_review_blockers")', text)
        self.assertIn('for item in (report.get("transfer_reconciliation_property_cash_review_blockers") or [])', text)
        self.assertIn('"review_candidate_packet"', text)
        self.assertIn('"daily_source_cash_balance"', text)
        self.assertIn('"source_cash_reconciliation_actions"', text)
        self.assertIn('"cf_balance_sheet_consistency"', text)
        self.assertIn('if effective_failed_step == "coownership_gl_policy_validation":', text)
        self.assertIn('"coownership_gl_policy_validation_blocked_properties"', text)
        self.assertIn('def coownership_gl_policy_completion_action(report: dict[str, Any]) -> str:', text)
        self.assertIn("policy_action += f\" for {', '.join(blocked_properties)}\"", text)
        self.assertIn("Apply the prepared 85-104 pre-closing Baselane retag", text)
        self.assertIn('if report["monthly_completion_next_actions"] and not report.get("next_action"):', text)
        self.assertIn("if gap and not ok:", text)
        self.assertIn('"jobs_file": report.get("openclaw_cron_jobs_file")', text)
        self.assertIn('"expr": report.get("monthly_cron_schedule_expr")', text)
        self.assertIn('"Baselane monthly close is not enabled on the canonical 15th scheduler"', text)
        self.assertIn('candidate_primary = Path(cron_jobs_path).with_name("jobs.primary.json")', text)
        self.assertIn('cron_jobs_path = cron_jobs_primary_path', text)

    def test_financial_patch_readiness_runs_after_publish_before_cleanup(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('LOFTY_FINANCIAL_PATCH_READINESS_FILE="$REPORT_DIR/lofty_financial_patch_readiness.json"', text)
        self.assertIn('LOFTY_FINANCIAL_PATCH_READINESS_STATUS="not_started"', text)
        self.assertIn('CURRENT_STEP="lofty_financial_patch_readiness"', text)
        self.assertIn('LOFTY_FINANCIAL_PATCH_READINESS_SCRIPT="$ROOT/scripts/lofty_financial_patch_readiness.py"', text)
        self.assertIn('--financial-patch-script "$LOFTY_PM_FINANCIAL_PATCH_SCRIPT"', text)
        self.assertIn('--live-financial-capture-report "$LIVE_FINANCIAL_CAPTURE_FILE"', text)
        self.assertIn('--review-candidate-packet-report "$REVIEW_CANDIDATE_PACKET_FILE"', text)
        self.assertIn('--report "$LOFTY_FINANCIAL_PATCH_READINESS_FILE"', text)
        self.assertIn('"lofty_financial_patch_readiness": os.environ.get("BASELANE_MONTHLY_LOFTY_FINANCIAL_PATCH_READINESS_STATUS")', text)
        self.assertIn('"lofty_financial_patch_readiness": os.environ.get("BASELANE_MONTHLY_LOFTY_FINANCIAL_PATCH_READINESS_FILE")', text)
        self.assertIn('"lofty_financial_patch_ready_count"', text)
        self.assertIn('"lofty_financial_patch_guard_reconcile_required_count"', text)
        self.assertIn('"lofty_financial_patch_blocked_count"', text)
        self.assertIn('"lofty_financial_patch_field_count_total"', text)
        self.assertIn('"lofty_financial_patch_candidate_packet_missing_runtime_count"', text)
        self.assertIn('"lofty_financial_patch_candidate_packet_monthly_summary_issue_count"', text)
        self.assertIn('"lofty_financial_patch_candidate_packet_monthly_summary_missing_required_field_count"', text)
        self.assertIn('"lofty_financial_patch_candidate_packet_monthly_summary_coverage_totals"', text)
        self.assertIn('"lofty_financial_patch_runtime_missing_candidate_count"', text)
        self.assertIn('"lofty_financial_patch_runtime_monthly_summary_issue_count"', text)
        self.assertIn('"lofty_financial_patch_runtime_monthly_summary_missing_required_field_count"', text)
        self.assertIn('"lofty_financial_patch_runtime_monthly_summary_coverage_totals"', text)
        self.assertIn('"lofty_financial_patch_blocker_csv_count"', text)
        self.assertIn('"lofty_financial_patch_blocker_csv"', text)
        self.assertIn('"lofty_financial_patch_blocker_markdown"', text)
        self.assertIn('"lofty_financial_patch_readiness_blockers"', text)
        self.assertIn('"lofty_financial_patch_readiness_blockers_csv"', text)
        self.assertIn('BASELANE_MONTHLY_LOFTY_FINANCIAL_PATCH_CANDIDATE_PACKET_MISSING_RUNTIME_COUNT', text)
        self.assertIn('BASELANE_MONTHLY_LOFTY_FINANCIAL_PATCH_RUNTIME_MONTHLY_SUMMARY_ISSUE_COUNT', text)
        self.assertIn('BASELANE_MONTHLY_LOFTY_FINANCIAL_PATCH_RUNTIME_MONTHLY_SUMMARY_MISSING_REQUIRED_FIELD_COUNT', text)
        self.assertLess(text.index('CURRENT_STEP="lofty_pm_publish"'), text.index('CURRENT_STEP="lofty_financial_patch_readiness"'))
        self.assertLess(text.index('CURRENT_STEP="lofty_financial_patch_readiness"'), text.index('CURRENT_STEP="lofty_listing_cleanup_queue"'))

    def test_live_updates_history_containment_runs_after_publish(self):
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('LIVE_UPDATES_HISTORY_CONTAINMENT_FILE="$REPORT_DIR/lofty_live_updates_history_containment_report.json"', text)
        self.assertIn('LOFTY_LIVE_UPDATES_FULL_LOCAL_RESTORE_FILE="$REPORT_DIR/lofty_live_updates_full_local_restore.json"', text)
        self.assertIn('LOFTY_LIVE_UPDATES_HISTORY_CONTAINMENT_STATUS="not_started"', text)
        self.assertIn('LOFTY_LIVE_UPDATES_FULL_LOCAL_RESTORE_STATUS="not_started"', text)
        self.assertIn('LOFTY_PM_LEGACY_RUNTIME_MAP="$REPORT_DIR/lofty-pm-runtime-map.json"', text)
        self.assertIn('CURRENT_STEP="lofty_live_updates_full_local_restore"', text)
        self.assertIn('LOFTY_LIVE_UPDATES_FULL_LOCAL_RESTORE_SCRIPT="$ROOT/scripts/restore_lofty_live_updates_from_local_history.py"', text)
        self.assertIn('CURRENT_STEP="lofty_live_updates_history_containment"', text)
        self.assertIn('LOFTY_LIVE_UPDATES_HISTORY_CONTAINMENT_SCRIPT="$ROOT/scripts/lofty_live_updates_history_containment_report.py"', text)
        self.assertIn('FULL_LOCAL_RESTORE_ARGS+=(--runtime-map "$LOFTY_PM_SKILL_MAP")', text)
        self.assertIn('--runtime-map "$LOFTY_PM_RUNTIME_MAP"', text)
        self.assertIn('FULL_LOCAL_RESTORE_ARGS+=(--runtime-map "$LOFTY_PM_LEGACY_RUNTIME_MAP")', text)
        self.assertIn('HISTORY_CONTAINMENT_ARGS+=(--runtime-map "$LOFTY_PM_SKILL_MAP")', text)
        self.assertIn('HISTORY_CONTAINMENT_ARGS+=(--runtime-map "$LOFTY_PM_LEGACY_RUNTIME_MAP")', text)
        self.assertIn('--report "$LOFTY_LIVE_UPDATES_FULL_LOCAL_RESTORE_FILE"', text)
        self.assertIn('--report "$LIVE_UPDATES_HISTORY_CONTAINMENT_FILE"', text)
        self.assertIn('FULL_LOCAL_RESTORE_ARGS+=(--dry-run)', text)
        self.assertIn('BASELANE_MONTHLY_LOFTY_LIVE_UPDATES_FULL_LOCAL_RESTORE_FILE="$LOFTY_LIVE_UPDATES_FULL_LOCAL_RESTORE_FILE"', text)
        self.assertIn('BASELANE_MONTHLY_LIVE_UPDATES_HISTORY_CONTAINMENT_FILE="$LIVE_UPDATES_HISTORY_CONTAINMENT_FILE"', text)
        self.assertIn('BASELANE_MONTHLY_LOFTY_LIVE_UPDATES_FULL_LOCAL_RESTORE_STATUS="$LOFTY_LIVE_UPDATES_FULL_LOCAL_RESTORE_STATUS"', text)
        self.assertIn('BASELANE_MONTHLY_LOFTY_LIVE_UPDATES_HISTORY_CONTAINMENT_STATUS="$LOFTY_LIVE_UPDATES_HISTORY_CONTAINMENT_STATUS"', text)
        self.assertIn('"lofty_live_updates_full_local_restore": os.environ.get("BASELANE_MONTHLY_LOFTY_LIVE_UPDATES_FULL_LOCAL_RESTORE_STATUS")', text)
        self.assertIn('"lofty_live_updates_history_containment": os.environ.get("BASELANE_MONTHLY_LOFTY_LIVE_UPDATES_HISTORY_CONTAINMENT_STATUS")', text)
        self.assertIn('"lofty_live_updates_full_local_restore": os.environ.get("BASELANE_MONTHLY_LOFTY_LIVE_UPDATES_FULL_LOCAL_RESTORE_FILE")', text)
        self.assertIn('"lofty_live_updates_history_containment": os.environ.get("BASELANE_MONTHLY_LIVE_UPDATES_HISTORY_CONTAINMENT_FILE")', text)
        self.assertIn('"lofty_live_updates_full_local_restore_ok"', text)
        self.assertIn('"lofty_live_updates_full_local_restore_total_entry_count"', text)
        self.assertIn('"lofty_live_updates_history_containment_ok"', text)
        self.assertIn('"lofty_live_updates_history_containment_missing_entry_count"', text)
        self.assertIn('"lofty_live_updates_full_local_restore"', text)
        self.assertIn('"lofty_live_updates_history_containment"', text)
        self.assertIn('report.get("lofty_live_updates_full_local_restore_sends_owner_email") is False', text)
        self.assertIn('report.get("lofty_live_updates_history_containment_missing_entry_count") == 0', text)
        self.assertIn('"Lofty live listing updates full local history restore did not run cleanly before containment verification"', text)
        self.assertIn('"Lofty live listing updates do not contain the full guarded local UPDATES.md history"', text)
        self.assertLess(text.index('CURRENT_STEP="lofty_pm_publish"'), text.index('CURRENT_STEP="lofty_live_updates_full_local_restore"'))
        self.assertLess(text.index('CURRENT_STEP="lofty_live_updates_full_local_restore"'), text.index('CURRENT_STEP="lofty_live_updates_history_containment"'))
        self.assertLess(text.index('CURRENT_STEP="lofty_live_updates_history_containment"'), text.index('CURRENT_STEP="lofty_financial_patch_readiness"'))

    def test_dry_run_forces_non_native_owner_email_packet_preview_even_when_send_flags_are_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scripts = root / "scripts"
            reports = root / "reports"
            comms = root / "comms"
            (comms / "updates").mkdir(parents=True)
            reports.mkdir(parents=True)
            (comms / "updates" / "2026-06-portfolio-update-index.csv").write_text(
                "property_path,draft_path,status\n"
                f"{root / 'property'},draft.md,active\n",
                encoding="utf-8",
            )
            cron_jobs = root / "cron" / "jobs.json"
            cron_jobs.parent.mkdir(parents=True, exist_ok=True)
            cron_jobs.write_text(
                json.dumps(
                    {
                        "jobs": [
                            {
                                "id": "baselane-financials-monthly",
                                "enabled": True,
                                "schedule": {"expr": "0 9 15 * *", "tz": "Europe/Paris"},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            discord_all_send_validation = reports / "baselane_financials_monthly_discord_all_send_plan_validation.json"
            discord_all_send_validation.write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "record_count": 1,
                        "unmapped_count": 0,
                        "stale_route_count": 0,
                        "missing_financial_summary_count": 0,
                        "source_issue_count": 0,
                    }
                ),
                encoding="utf-8",
            )
            write_file(
                scripts / "lofty_monthly_exclusions.py",
                "DEFAULT_MANUAL_EXCLUDED_PROPERTIES = []\n"
                "def match_exclusion_guard(path, guards):\n"
                "    return None\n"
                "def monthly_exclusion_guards(yhome_csv, manual):\n"
                "    return [], {'status': 'not_configured', 'excluded_count': 0}, []\n",
            )
            write_file(
                scripts / "lofty_property_paths.py",
                "from pathlib import Path\n"
                "def public_dir_for_property(path):\n"
                "    return Path(path) / 'Public'\n"
                "def resolve_property_path(path):\n"
                "    return Path(path), {'resolved_property_path': str(path)}\n",
            )
            write_file(
                scripts / "baselane_no_mortgage_financials_guard.py",
                "import json, sys\n"
                "from pathlib import Path\n"
                "report = Path(sys.argv[sys.argv.index('--report') + 1])\n"
                "report.parent.mkdir(parents=True, exist_ok=True)\n"
                "report.write_text(json.dumps({'status': 'ok', 'issue_count': 0}))\n",
            )
            write_file(
                scripts / "baselane_stale_financial_artifact_guard.py",
                "import json, sys\n"
                "from pathlib import Path\n"
                "report = Path(sys.argv[sys.argv.index('--report') + 1])\n"
                "report.parent.mkdir(parents=True, exist_ok=True)\n"
                "report.write_text(json.dumps({'status': 'ok', 'issue_count': 0, 'mutation_attempted': False}))\n",
            )
            write_file(
                scripts / "baselane_cf_mortgage_balance_integrity_guard.py",
                "import json, sys\n"
                "from pathlib import Path\n"
                "report = Path(sys.argv[sys.argv.index('--report') + 1])\n"
                "report.parent.mkdir(parents=True, exist_ok=True)\n"
                "report.write_text(json.dumps({'status': 'ok', 'candidate_workbook_count': 0, 'changed_cell_count': 0, 'remaining_sentinel_cell_count': 0}))\n",
            )
            write_file(
                scripts / "lofty_monthly_guarded_apply.py",
                "import json, sys\n"
                "from pathlib import Path\n"
                "report = Path(sys.argv[sys.argv.index('--report') + 1])\n"
                "report.parent.mkdir(parents=True, exist_ok=True)\n"
                "report.write_text(json.dumps({'status': 'ok', 'apply': '--apply' in sys.argv, 'issue_count': 0}))\n",
            )
            write_file(
                scripts / "lofty_unreviewed_financial_approval_quarantine.py",
                "import json, sys\n"
                "from pathlib import Path\n"
                f"Path({str(root / 'quarantine_args.json')!r}).write_text(json.dumps(sys.argv[1:]))\n"
                "report = Path(sys.argv[sys.argv.index('--report') + 1])\n"
                "commands = Path(sys.argv[sys.argv.index('--commands-file') + 1])\n"
                "report.parent.mkdir(parents=True, exist_ok=True)\n"
                "commands.parent.mkdir(parents=True, exist_ok=True)\n"
                "report.write_text(json.dumps({'status': 'review', 'command_count': 1, 'mutates_dropbox_files': False, 'mutates_lofty_listing': False, 'sends_owner_email': False}))\n"
                "commands.write_text('# requires explicit approval\\n')\n"
                "sys.exit(2)\n",
            )
            write_file(
                scripts / "baselane_monthly_owner_review_gate.py",
                "import json, sys\n"
                "from pathlib import Path\n"
                "report = Path(sys.argv[sys.argv.index('--report') + 1])\n"
                "report.parent.mkdir(parents=True, exist_ok=True)\n"
                "report.write_text(json.dumps({'status': 'ok', 'issue_count': 0}))\n",
            )
            write_file(
                scripts / "baselane_monthly_readiness_report.py",
                "import json, sys\n"
                "from pathlib import Path\n"
                "report = Path(sys.argv[sys.argv.index('--report') + 1])\n"
                "payload = {'status': 'ok', 'owner_email_allowed': True, 'blocker_count': 0, 'actionable_summary': {'actionable_blocker_count': 0}}\n"
                "report.parent.mkdir(parents=True, exist_ok=True)\n"
                "report.write_text(json.dumps(payload))\n",
            )
            write_file(
                scripts / "lofty_monthly_publish_to_pm.py",
                "import json, sys\n"
                "from pathlib import Path\n"
                "report = Path(sys.argv[sys.argv.index('--report') + 1])\n"
                "runtime_map = Path(sys.argv[sys.argv.index('--runtime-map') + 1])\n"
                "report.parent.mkdir(parents=True, exist_ok=True)\n"
                "runtime_map.parent.mkdir(parents=True, exist_ok=True)\n"
                "report.write_text(json.dumps({'status': 'ok', 'issue_count': 0, 'apply': '--apply' in sys.argv, 'publish_attempted': '--apply' in sys.argv, 'publish_result_count': 1, 'publish_failed_count': 0, 'financial_publish_result_count': 1, 'financial_publish_failed_count': 0, 'guarded_apply_status': 'ok', 'guarded_apply_live_ready': True}))\n"
                "runtime_map.write_text(json.dumps({'properties': []}))\n",
            )
            write_file(
                scripts / "lofty_monthly_guild_test_post_report.py",
                "import json, sys\n"
                "from pathlib import Path\n"
                "report = Path(sys.argv[sys.argv.index('--report') + 1])\n"
                "message = report.with_suffix('.message.md')\n"
                "message.write_text('Property Update: 326-332 S Alcott\\n\\nMonthly dry-run update.\\n')\n"
                "report.parent.mkdir(parents=True, exist_ok=True)\n"
                "report.write_text(json.dumps({'status': 'prepared_not_posted', 'valid': False, 'posted': False, 'target': 'channel:1362189256163856594', 'message_file': str(message), 'selected': {'property_name': '326-332 S Alcott St, Denver, CO 80219'}}))\n",
            )
            write_file(
                scripts / "send_monthly_discord_property_update.py",
                "import json, sys\n"
                "from pathlib import Path\n"
                f"Path({str(root / 'discord_args.json')!r}).write_text(json.dumps(sys.argv[1:]))\n"
                "report = Path(sys.argv[sys.argv.index('--report') + 1])\n"
                "guild = Path(sys.argv[sys.argv.index('--guild-report') + 1])\n"
                "payload = json.loads(guild.read_text())\n"
                "report.parent.mkdir(parents=True, exist_ok=True)\n"
                "report.write_text(json.dumps({'status': 'ok_dry_run' if '--dry-run' in sys.argv else 'ok', 'dry_run': '--dry-run' in sys.argv, 'target': payload.get('target'), 'message_file': payload.get('message_file')}))\n",
            )
            write_file(
                scripts / "baselane_monthly_accruals_cron.sh",
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "mkdir -p \"$WORKSPACE_ROOT/reports\"\n"
                "cat > \"$WORKSPACE_ROOT/reports/baselane_monthly_accruals_202606.json\" <<'JSON'\n"
                "{\"status\":\"ok\",\"missing_count\":0,\"amount_mismatch_count\":0,\"blocked_first_day_pm_fee_count\":0}\n"
                "JSON\n",
                executable=True,
            )
            write_file(
                scripts / "baselane_lofty_transfer_requirements.py",
                "import json, sys\n"
                "from pathlib import Path\n"
                "report = Path(sys.argv[sys.argv.index('--report') + 1])\n"
                "telegram = Path(sys.argv[sys.argv.index('--telegram-markdown') + 1])\n"
                "report.parent.mkdir(parents=True, exist_ok=True)\n"
                "report.write_text(json.dumps({'status': 'ok', 'ready_to_send_property_count': 0, 'held_property_count': 0, 'recommended_send_to_lofty_total': 0}))\n"
                "telegram.write_text('Monthly Lofty transfer reconciliation\\nStatus: ok\\n')\n",
            )
            write_file(
                scripts / "send_monthly_transfer_reconciliation_telegram.py",
                "import json, sys\n"
                "from pathlib import Path\n"
                "report = Path(sys.argv[sys.argv.index('--report') + 1])\n"
                "report.parent.mkdir(parents=True, exist_ok=True)\n"
                "report.write_text(json.dumps({'status': 'ok_dry_run', 'telegram_send_ok': False}))\n",
            )
            write_file(
                scripts / "audit_monthly_pipeline_candidate_coverage.py",
                "import json, sys\n"
                "from pathlib import Path\n"
                "report = Path(sys.argv[sys.argv.index('--report') + 1])\n"
                "report.parent.mkdir(parents=True, exist_ok=True)\n"
                "report.write_text(json.dumps({\n"
                "    'status': 'ok',\n"
                "    'generated_at': '2026-07-16T16:23:37Z',\n"
                "    'input_digests': {'transfer_report': 'a' * 64},\n"
                "    'mismatch_count': 0,\n"
                "    'mismatches': [],\n"
                "    'transfer_reconciliation': {'status': 'ok'},\n"
                "    'telegram_reconciliation': {'status': 'ok_dry_run'},\n"
                "}))\n",
            )
            write_file(
                scripts / "baselane_owner_email_send_guard.py",
                "import json, sys\n"
                "from pathlib import Path\n"
                "report = Path(sys.argv[sys.argv.index('--report') + 1])\n"
                "report.parent.mkdir(parents=True, exist_ok=True)\n"
                "report.write_text(json.dumps({'status': 'ok', 'issue_count': 0, 'send_allowed': True, 'safe_block': True, 'no_spam_guard_ok': True}))\n",
            )
            write_file(
                scripts / "lofty_monthly_owner_email_packet.py",
                "import json, sys\n"
                "from pathlib import Path\n"
                f"Path({str(root / 'packet_args.json')!r}).write_text(json.dumps(sys.argv[1:]))\n"
                "report = Path(sys.argv[sys.argv.index('--report') + 1])\n"
                "report.parent.mkdir(parents=True, exist_ok=True)\n"
                "report.write_text(json.dumps({'status': 'ok', 'issue_count': 0, 'safe_to_send_now': False, 'sent_state_written': False}))\n",
            )
            write_file(
                scripts / "lofty_owner_email_weekly.py",
                "import json\n"
                "print(json.dumps({'pending_property_count': 1}))\n",
                executable=True,
            )

            env = os.environ.copy()
            env.update(
                {
                    "WORKSPACE_ROOT": str(root),
                    "COMMS_WORKSPACE": str(comms),
                    "BASELANE_MONTHLY_LOCK_PATH": str(root / "monthly.lock"),
                    "PYTHON_BIN": sys.executable,
                    "RUN_MONTH": "2026-06",
                    "DRY_RUN": "1",
                    "BASELANE_MONTHLY_LIVE_ACTIONS_APPROVED": "1",
                    "SEND_OWNER_EMAILS": "1",
                    "SEND_NON_NATIVE_OWNER_EMAILS": "1",
                    "PUBLISH_LOFTY_PM_UPDATES": "1",
                    "APPLY_LOFTY_GUARDED_UPDATES": "1",
                    "SEND_MONTHLY_DISCORD_PROPERTY_UPDATE": "1",
                    "REQUIRE_YHOME_SOLD_GUARD": "0",
                    "YHOME_TRANSITION_RECONCILIATION_URL": "",
                    "RUN_LOFTY_CDP_ENSURE": "0",
                    "RUN_LOFTY_CDP_PREFLIGHT": "0",
                    "CAPTURE_LOFTY_LIVE_UPDATE_GUARDS": "0",
                    "CAPTURE_LOFTY_LIVE_FINANCIAL_GUARDS": "0",
                    "BUILD_LOFTY_LISTING_CLEANUP_QUEUE": "0",
                    "AUTO_APPROVE_SAFE_REVIEW_CANDIDATES": "0",
                    "REQUIRE_NO_MORTGAGE_FINANCIALS_GUARD": "1",
                    "RUN_LEGACY_OWNER_EMAIL_WEEKLY": "0",
                    "RUN_BASELANE_MONTHLY_FINANCE_TRUTH_REFRESH": "0",
                    "REQUIRE_BASELANE_MONTHLY_FINANCE_TRUTH_REFRESH": "0",
                    "OPENCLAW_CRON_JOBS_FILE": str(cron_jobs),
                    "PYTHONPATH": str(REPO_ROOT / "scripts"),
                }
            )

            result = subprocess.run(
                ["bash", str(SCRIPT)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertIn(result.returncode, {0, 2}, result.stderr + result.stdout)
            packet_args = json.loads((root / "packet_args.json").read_text(encoding="utf-8"))
            self.assertIn("--dry-run", packet_args)
            self.assertNotIn("--send", packet_args)
            quarantine_args = json.loads((root / "quarantine_args.json").read_text(encoding="utf-8"))
            self.assertIn("--guarded-apply-report", quarantine_args)
            self.assertIn("--commands-file", quarantine_args)
            self.assertNotIn("--apply", quarantine_args)
            self.assertTrue(
                (root / "discord_args.json").is_file(),
                (reports / "baselane_financials_monthly_run_report.json").read_text(encoding="utf-8")
                if (reports / "baselane_financials_monthly_run_report.json").is_file()
                else result.stderr + result.stdout,
            )
            discord_args = json.loads((root / "discord_args.json").read_text(encoding="utf-8"))
            self.assertIn("--dry-run", discord_args)
            self.assertIn("--guild-report", discord_args)
            discord_send = json.loads((reports / "baselane_financials_monthly_discord_property_update_send.json").read_text(encoding="utf-8"))
            self.assertEqual(discord_send["status"], "ok_dry_run")
            self.assertEqual(discord_send["target"], "channel:1362189256163856594")
            monthly_report = json.loads((reports / "baselane_financials_monthly_run_report.json").read_text(encoding="utf-8"))
            self.assertTrue(monthly_report["generated_at"])
            self.assertEqual(monthly_report["steps"]["discord_property_update"], "ok_dry_run")
            self.assertEqual(monthly_report["steps"]["transfer_reconciliation_telegram"], "ok_dry_run")
            self.assertEqual(monthly_report["steps"]["lofty_unreviewed_financial_quarantine"], "review")
            self.assertEqual(monthly_report["lofty_unreviewed_financial_quarantine_status"], "review")
            schedule_evidence = next(
                item for item in monthly_report["monthly_completion_evidence"] if item["name"] == "monthly_cron_scheduled_for_15th"
            )
            self.assertTrue(schedule_evidence["ok"])
            self.assertNotIn("gap", schedule_evidence)
            self.assertEqual(
                monthly_report["artifacts"]["discord_property_update_send"],
                str(reports / "baselane_financials_monthly_discord_property_update_send.json"),
            )
            self.assertEqual(
                monthly_report["artifacts"]["transfer_reconciliation_telegram_send"],
                str(reports / "baselane_lofty_transfer_requirements_telegram_send.json"),
            )
            self.assertEqual(
                monthly_report["artifacts"]["unreviewed_financial_quarantine"],
                str(reports / "lofty_unreviewed_financial_approval_quarantine.json"),
            )
            self.assertEqual(
                monthly_report["artifacts"]["unreviewed_financial_quarantine_commands"],
                str(reports / "lofty_unreviewed_financial_approval_quarantine.requires-explicit-approval.sh"),
            )


if __name__ == "__main__":
    unittest.main()
