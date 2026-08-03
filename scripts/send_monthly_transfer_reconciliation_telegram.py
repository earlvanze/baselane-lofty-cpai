#!/usr/bin/env python3
"""Send the monthly transfer reconciliation markdown to Telegram."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import baselane_eod_telegram_report as telegram
from transfer_report_digest import stable_transfer_report_digest


ROOT = Path(__file__).absolute().parents[1]
DEFAULT_MESSAGE = ROOT / "reports" / "baselane_lofty_transfer_requirements.telegram.md"
DEFAULT_REPORT = ROOT / "reports" / "baselane_lofty_transfer_requirements_telegram_send.json"
DEFAULT_TRANSFER_REPORT = ROOT / "reports" / "baselane_lofty_transfer_requirements.json"
DEFAULT_SENT_STATE = ROOT / "reports" / "baselane_lofty_transfer_requirements_telegram_send_state.json"


def iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def transfer_report_current_for_run(transfer_report: dict[str, object], started_at: str | None) -> bool | None:
    """Return whether the report was generated during the caller's run."""
    if not started_at:
        return None
    try:
        generated = datetime.fromisoformat(str(transfer_report.get("generated_at") or "").replace("Z", "+00:00"))
        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return generated.astimezone(timezone.utc) >= started.astimezone(timezone.utc)


def write_report(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str | None:
    return stable_transfer_report_digest(path)


def canonical_ledger_evidence_issues(transfer_report: dict[str, object]) -> list[str]:
    evidence = transfer_report.get("canonical_ledger_evidence")
    if not isinstance(evidence, dict):
        return ["canonical_ledger_evidence_missing"]
    sources = evidence.get("sources")
    expected_fingerprint = str(evidence.get("fingerprint_sha256") or "")
    if not isinstance(sources, list) or not sources or not expected_fingerprint:
        return ["canonical_ledger_evidence_incomplete"]

    current: list[tuple[str, str]] = []
    issues: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            return ["canonical_ledger_evidence_incomplete"]
        path = Path(str(source.get("path") or "")).expanduser()
        expected_digest = str(source.get("sha256") or "")
        if not expected_digest:
            return ["canonical_ledger_evidence_incomplete"]
        if not path.is_file():
            issues.append(f"canonical_ledger_source_missing={path}")
            continue
        current_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if current_digest != expected_digest:
            issues.append(f"canonical_ledger_source_digest_mismatch={path}")
        current.append((str(path.resolve(strict=False)), current_digest))
    fingerprint_text = "\n".join(f"{path}\0{digest}" for path, digest in sorted(current, key=lambda item: item[0].casefold()))
    current_fingerprint = hashlib.sha256(fingerprint_text.encode("utf-8")).hexdigest() if not issues else None
    if not issues and current_fingerprint != expected_fingerprint:
        issues.append("canonical_ledger_fingerprint_mismatch")
    return issues


def read_json(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"status": "missing", "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreadable", "path": str(path), "error": f"{type(exc).__name__}: {exc}"}
    return payload if isinstance(payload, dict) else {"status": "unreadable", "path": str(path), "error": "root is not object"}


def matching_sent_state(
    sent_state: dict[str, object],
    *,
    message_sha256: str,
    transfer_report_digest: str | None,
) -> bool:
    if sent_state.get("status") not in {"ok", "ok_previous"}:
        return False
    if sent_state.get("telegram_send_ok") is not True:
        return False
    if str(sent_state.get("message_sha256") or "") != message_sha256:
        return False
    if not transfer_report_digest:
        return False
    return str(sent_state.get("transfer_report_digest") or "") == transfer_report_digest


def sent_state_payload(payload: dict[str, object], *, text: str) -> dict[str, object]:
    return {
        "status": "ok",
        "sent_at": iso_z(),
        "telegram_send_ok": True,
        "message": payload.get("message"),
        "message_sha256": payload.get("message_sha256"),
        "message_bytes": payload.get("message_bytes"),
        "message_text": text,
        "transfer_report": payload.get("transfer_report"),
        "transfer_report_digest": payload.get("transfer_report_digest"),
        "transfer_report_digest_scheme": payload.get("transfer_report_digest_scheme", "stable_transfer_report_v1"),
        "transfer_report_generated_at": payload.get("transfer_report_generated_at"),
        "chunk_count": payload.get("chunk_count"),
        "telegram_http_statuses": payload.get("telegram_http_statuses"),
    }


def message_quality_issues(text: str, transfer_report: dict[str, object], message_path: Path) -> list[str]:
    issues: list[str] = []
    if "Monthly Lofty transfer reconciliation" not in text:
        issues.append("missing_transfer_reconciliation_title")
    if "As of:" not in text and "Reporting month:" not in text:
        issues.append("missing_reporting_month_line")
    if "Final transfer amounts:" not in text:
        issues.append("missing_final_transfer_amounts_line")
    if "Ready to send to Lofty:" not in text and "Approved to send to Lofty now:" not in text:
        issues.append("missing_approved_or_ready_to_send_line")
    if "Evidence:" not in text:
        issues.append("missing_evidence_line")
    report_status = str(transfer_report.get("status") or "")
    if report_status in {"missing", "unreadable", ""}:
        issues.append(f"transfer_report_{report_status or 'missing'}")
    telegram_summary = str(transfer_report.get("telegram_summary") or "")
    if telegram_summary:
        summary_path = Path(telegram_summary).expanduser()
        if summary_path.resolve() != message_path.expanduser().resolve():
            issues.append("message_path_mismatch_transfer_report_telegram_summary")
        elif not summary_path.is_file():
            issues.append("transfer_report_telegram_summary_missing")
        else:
            try:
                summary_text = summary_path.read_text(encoding="utf-8").strip()
            except Exception:  # noqa: BLE001
                summary_text = ""
            if sha256_text(summary_text) != sha256_text(text):
                issues.append("message_digest_mismatch_transfer_report_telegram_summary")
    if int(transfer_report.get("candidate_packet_record_count") or 0) <= 0:
        issues.append("candidate_packet_record_count_zero")
    if transfer_report.get("recommended_send_to_lofty_total") is None and transfer_report.get(
        "combined_reserve_shortfall_total",
        transfer_report.get("eco_cash_shortfall_total"),
    ) is None:
        issues.append("no_transfer_total_or_shortfall_available")
    bank_actions_final = transfer_report.get("bank_transfer_actions_final")
    if bank_actions_final is None:
        bank_actions_final = transfer_report.get("recommended_send_to_lofty_total_is_final")
    if bank_actions_final is not True and "Final transfer amounts: no" not in text:
        issues.append("non_final_transfer_amounts_not_disclosed")
    if (
        transfer_report.get("recommended_send_to_lofty_total_is_final") is True
        and transfer_report.get("bank_transfer_actions_final") is False
        and "ready send_to_lofty rows are final" not in text
    ):
        issues.append("missing_partial_final_ready_send_disclaimer")
    if transfer_report.get("eco_operating_cash_full_balance_total") is not None:
        if "Full ECO Operating Cash balance:" not in text:
            issues.append("missing_full_eco_operating_cash_balance_line")
        if "not the Lofty send amount" not in text:
            issues.append("missing_full_eco_balance_not_send_amount_disclaimer")
        if "ECO cash basis:" not in text or "full DAO-attributed Column E sum" not in text:
            issues.append("missing_full_column_e_cash_basis_line")
        if "Reporting month policy:" not in text or "does not limit ECO Operating Cash rows" not in text:
            issues.append("missing_reporting_month_not_cash_scope_policy")
    if int(transfer_report.get("active_dao_cash_balance_property_count") or 0) > 0:
        if "Active DAO cash balances:" not in text:
            issues.append("missing_active_dao_cash_balance_line")
        if not isinstance(transfer_report.get("active_dao_cash_balance_rows"), list):
            issues.append("missing_active_dao_cash_balance_rows")
    if int(transfer_report.get("missing_bank_action_count") or 0) > 0:
        issues.append(f"missing_bank_action_count={int(transfer_report.get('missing_bank_action_count') or 0)}")
    if not transfer_report.get("bank_action_counts"):
        issues.append("missing_bank_action_counts")
    rows = transfer_report.get("rows")
    if isinstance(rows, list) and rows:
        missing_as_of_rows = [
            str(row.get("property") or index)
            for index, row in enumerate(rows, start=1)
            if isinstance(row, dict)
            and not str(row.get("eco_gl_column_e_reporting_month") or row.get("eco_gl_column_e_as_of_month") or "").strip()
        ]
        if missing_as_of_rows:
            issues.append(f"row_reporting_month_missing_count={len(missing_as_of_rows)}")
    return issues


def review_report_delivery_allowed(transfer_report: dict[str, object]) -> bool:
    """Return whether a clean non-final reconciliation may be delivered for review."""
    source_blockers = transfer_report.get("source_blockers")
    source_blockers = source_blockers if isinstance(source_blockers, list) else []
    property_cash_review_blockers = transfer_report.get("property_cash_review_blockers")
    property_cash_review_blockers = (
        property_cash_review_blockers if isinstance(property_cash_review_blockers, list) else []
    )
    property_cash_review_details = transfer_report.get("property_cash_review_details")
    property_cash_review_details = (
        property_cash_review_details if isinstance(property_cash_review_details, list) else []
    )
    return bool(
        str(transfer_report.get("status") or "") == "review"
        and transfer_report.get("recommended_send_to_lofty_total_is_final") is True
        and transfer_report.get("bank_transfer_actions_final") is False
        and not source_blockers
        and not property_cash_review_blockers
        and not property_cash_review_details
        and int(transfer_report.get("missing_bank_action_count") or 0) == 0
    )


def informational_report_delivery_allowed(transfer_report: dict[str, object]) -> bool:
    """Allow delivery of a clean report without authorizing held transfers."""
    source_blockers = transfer_report.get("source_blockers")
    source_blockers = source_blockers if isinstance(source_blockers, list) else []
    property_cash_review_blockers = transfer_report.get("property_cash_review_blockers")
    property_cash_review_blockers = (
        property_cash_review_blockers if isinstance(property_cash_review_blockers, list) else []
    )
    property_cash_review_details = transfer_report.get("property_cash_review_details")
    property_cash_review_details = (
        property_cash_review_details if isinstance(property_cash_review_details, list) else []
    )
    return bool(
        str(transfer_report.get("status") or "") == "ok"
        and transfer_report.get("recommended_send_to_lofty_total_is_final") is True
        and transfer_report.get("recommended_send_to_lofty_total") is not None
        and not source_blockers
        and not property_cash_review_blockers
        and not property_cash_review_details
        and int(transfer_report.get("missing_bank_action_count") or 0) == 0
    )


def blocked_report_delivery_allowed(transfer_report: dict[str, object]) -> bool:
    """Allow a blocked-source notice without authorizing any transfer."""
    source_blockers = transfer_report.get("source_blockers")
    source_blockers = source_blockers if isinstance(source_blockers, list) else []
    return bool(
        str(transfer_report.get("status") or "") == "blocked_source_not_clean"
        and source_blockers
        and int(transfer_report.get("candidate_packet_record_count") or 0) > 0
        and transfer_report.get("recommended_send_to_lofty_total") is None
        and transfer_report.get("recommended_send_to_lofty_total_is_final") is not True
        and transfer_report.get("bank_transfer_actions_final") is not True
    )


def final_send_blockers(
    transfer_report: dict[str, object], *, allow_review_report: bool = False, allow_informational_report: bool = False,
    allow_blocked_report: bool = False,
) -> list[str]:
    blockers: list[str] = []
    status = str(transfer_report.get("status") or "")
    source_blockers = transfer_report.get("source_blockers")
    source_blockers = source_blockers if isinstance(source_blockers, list) else []
    property_cash_review_blockers = transfer_report.get("property_cash_review_blockers")
    property_cash_review_blockers = (
        property_cash_review_blockers if isinstance(property_cash_review_blockers, list) else []
    )
    property_cash_review_details = transfer_report.get("property_cash_review_details")
    property_cash_review_details = (
        property_cash_review_details if isinstance(property_cash_review_details, list) else []
    )
    partial_final_report = bool(
        status == "review"
        and transfer_report.get("recommended_send_to_lofty_total_is_final") is True
        and not source_blockers
        and not property_cash_review_blockers
        and not property_cash_review_details
    )
    blocked_delivery_allowed = allow_blocked_report and blocked_report_delivery_allowed(transfer_report)
    if status != "ok" and not partial_final_report and not blocked_delivery_allowed:
        blockers.append(f"transfer_report_status={status or 'missing'}")
    if transfer_report.get("recommended_send_to_lofty_total_is_final") is not True and not blocked_delivery_allowed:
        blockers.append("recommended_send_to_lofty_total_not_final")
    if transfer_report.get("recommended_send_to_lofty_total") is None and not blocked_delivery_allowed:
        blockers.append("recommended_send_to_lofty_total_missing")
    review_delivery_allowed = allow_review_report and review_report_delivery_allowed(transfer_report)
    informational_delivery_allowed = allow_informational_report and informational_report_delivery_allowed(transfer_report)
    if (
        transfer_report.get("bank_transfer_actions_final") is False
        and not review_delivery_allowed
        and not informational_delivery_allowed
        and not blocked_delivery_allowed
    ):
        blockers.append("bank_transfer_actions_not_final")
    if source_blockers and not blocked_delivery_allowed:
        blockers.append(f"source_blocker_count={len(source_blockers)}")
        blockers.extend(f"source_blocker={blocker}" for blocker in source_blockers[:25])
    if property_cash_review_blockers and not blocked_delivery_allowed:
        blockers.append(f"property_cash_review_blocker_count={len(property_cash_review_blockers)}")
        blockers.extend(f"property_cash_review_blocker={blocker}" for blocker in property_cash_review_blockers[:25])
    if property_cash_review_details and not blocked_delivery_allowed:
        blockers.append(f"property_cash_review_detail_count={len(property_cash_review_details)}")
        for detail in property_cash_review_details[:10]:
            if isinstance(detail, dict):
                property_name = detail.get("property") or detail.get("property_name") or detail.get("property_key")
                if property_name:
                    blockers.append(f"property_cash_review_detail={property_name}")
    if int(transfer_report.get("missing_bank_action_count") or 0) > 0 and not blocked_delivery_allowed:
        blockers.append(f"missing_bank_action_count={int(transfer_report.get('missing_bank_action_count') or 0)}")
    return blockers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message", type=Path, default=DEFAULT_MESSAGE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--transfer-report", type=Path, default=DEFAULT_TRANSFER_REPORT)
    parser.add_argument("--sent-state-file", type=Path, default=DEFAULT_SENT_STATE)
    parser.add_argument("--expected-transfer-report-digest")
    parser.add_argument(
        "--current-run-started-at",
        help="Require the transfer report generated_at to be at or after this UTC run start for live sends.",
    )
    parser.add_argument(
        "--allow-review-report",
        action="store_true",
        help="Allow delivery of a clean review report when transfer actions are not final; never authorizes money movement.",
    )
    parser.add_argument(
        "--allow-informational-report",
        action="store_true",
        help="Allow delivery of a clean ok report with held transfers; never authorizes money movement.",
    )
    parser.add_argument(
        "--allow-blocked-report",
        action="store_true",
        help="Allow delivery of a blocked-source notice; never authorizes money movement.",
    )
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    payload: dict[str, object] = {
        "job": "monthly-transfer-reconciliation-telegram",
        "generated_at": iso_z(),
        "message": str(args.message),
        "report": str(args.report),
        "transfer_report": str(args.transfer_report),
        "sent_state_file": str(args.sent_state_file),
        "send_requested": bool(args.send),
        "dry_run": bool(args.dry_run or not args.send),
        "telegram_send_ok": False,
        "telegram_http_statuses": [],
    }
    if not args.message.is_file():
        payload.update({"status": "failed", "issue": f"message file missing: {args.message}"})
        write_report(args.report, payload)
        return 1

    text = args.message.read_text(encoding="utf-8").strip()
    payload["message_bytes"] = len(text.encode("utf-8"))
    payload["message_sha256"] = sha256_text(text)
    if not text:
        payload.update({"status": "failed", "issue": f"message file empty: {args.message}"})
        write_report(args.report, payload)
        return 1
    transfer_report = read_json(args.transfer_report)
    transfer_report_current = transfer_report_current_for_run(transfer_report, args.current_run_started_at)
    quality_issues = message_quality_issues(text, transfer_report, args.message)
    review_delivery_allowed = bool(args.allow_review_report and review_report_delivery_allowed(transfer_report))
    informational_delivery_allowed = bool(
        args.allow_informational_report and informational_report_delivery_allowed(transfer_report)
    )
    blocked_delivery_allowed = bool(args.allow_blocked_report and blocked_report_delivery_allowed(transfer_report))
    send_blockers = final_send_blockers(
        transfer_report,
        allow_review_report=bool(args.allow_review_report),
        allow_informational_report=bool(args.allow_informational_report),
        allow_blocked_report=bool(args.allow_blocked_report),
    )
    if args.current_run_started_at and transfer_report_current is not True:
        send_blockers.append("transfer_report_not_current_for_run")
    payload["transfer_report_status"] = transfer_report.get("status")
    payload["transfer_report_path"] = str(args.transfer_report)
    payload["transfer_report_generated_at"] = transfer_report.get("generated_at")
    payload["current_run_started_at"] = args.current_run_started_at
    payload["transfer_report_current_for_run"] = transfer_report_current
    payload["transfer_report_digest"] = stable_transfer_report_digest(args.transfer_report)
    payload["transfer_report_digest_scheme"] = "stable_transfer_report_v1"
    payload["current_transfer_report_digest"] = payload["transfer_report_digest"]
    payload["transfer_report_digest_matches_current"] = payload["transfer_report_digest"] is not None
    payload["expected_transfer_report_digest"] = args.expected_transfer_report_digest
    payload["transfer_report_digest_matches_expected"] = (
        bool(args.expected_transfer_report_digest)
        and payload["transfer_report_digest"] == args.expected_transfer_report_digest
    )
    if args.send and not args.dry_run and not args.expected_transfer_report_digest:
        quality_issues.append("expected_transfer_report_digest_required_for_live_send")
    if args.expected_transfer_report_digest and not payload["transfer_report_digest_matches_expected"]:
        quality_issues.append("transfer_report_digest_mismatch_expected")
    ledger_evidence_issues = canonical_ledger_evidence_issues(transfer_report)
    payload["canonical_ledger_evidence_issues"] = ledger_evidence_issues
    payload["canonical_ledger_evidence_current"] = not ledger_evidence_issues
    if args.send and not args.dry_run:
        quality_issues.extend(ledger_evidence_issues)
    payload["transfer_report_candidate_packet_record_count"] = int(transfer_report.get("candidate_packet_record_count") or 0)
    payload["transfer_report_recommended_send_to_lofty_total"] = transfer_report.get("recommended_send_to_lofty_total")
    payload["transfer_report_recommended_send_to_lofty_total_is_final"] = transfer_report.get("recommended_send_to_lofty_total_is_final")
    payload["transfer_report_bank_transfer_actions_final"] = transfer_report.get("bank_transfer_actions_final")
    payload["transfer_report_bank_transfer_actions_final_policy"] = transfer_report.get("bank_transfer_actions_final_policy")
    payload["transfer_report_eco_operating_cash_full_balance_total"] = transfer_report.get("eco_operating_cash_full_balance_total")
    payload["transfer_report_recommended_send_to_lofty_total_is_cash_balance"] = transfer_report.get("recommended_send_to_lofty_total_is_cash_balance")
    payload["transfer_report_eco_cash_shortfall_total"] = transfer_report.get("eco_cash_shortfall_total")
    payload["transfer_report_combined_reserve_shortfall_total"] = transfer_report.get(
        "combined_reserve_shortfall_total",
        transfer_report.get("eco_cash_shortfall_total"),
    )
    payload["transfer_report_source_blockers"] = transfer_report.get("source_blockers") if isinstance(transfer_report.get("source_blockers"), list) else []
    payload["transfer_report_source_blocker_count"] = len(payload["transfer_report_source_blockers"])
    payload["transfer_report_property_cash_review_blockers"] = (
        transfer_report.get("property_cash_review_blockers")
        if isinstance(transfer_report.get("property_cash_review_blockers"), list)
        else []
    )
    payload["transfer_report_property_cash_review_blocker_count"] = len(
        payload["transfer_report_property_cash_review_blockers"]
    )
    payload["transfer_report_property_cash_review_detail_count"] = len(
        transfer_report.get("property_cash_review_details")
        if isinstance(transfer_report.get("property_cash_review_details"), list)
        else []
    )
    payload["transfer_report_source_blocker_summary"] = (
        transfer_report.get("source_blocker_summary")
        if isinstance(transfer_report.get("source_blocker_summary"), dict)
        else {}
    )
    payload["transfer_report_telegram_summary"] = transfer_report.get("telegram_summary")
    telegram_summary = str(transfer_report.get("telegram_summary") or "")
    if telegram_summary and Path(telegram_summary).expanduser().is_file():
        payload["transfer_report_telegram_summary_sha256"] = sha256_text(Path(telegram_summary).expanduser().read_text(encoding="utf-8").strip())
    payload["message_matches_transfer_report_telegram_summary"] = (
        bool(telegram_summary)
        and Path(telegram_summary).expanduser().resolve() == args.message.expanduser().resolve()
        and payload.get("transfer_report_telegram_summary_sha256") == payload["message_sha256"]
    )
    payload["message_quality_issues"] = quality_issues
    payload["message_quality_ok"] = not quality_issues
    payload["send_blockers"] = send_blockers
    payload["send_safe"] = not quality_issues and not send_blockers
    payload["review_report_delivery_requested"] = bool(args.allow_review_report)
    payload["review_report_delivery_allowed"] = review_delivery_allowed
    payload["informational_report_delivery_requested"] = bool(args.allow_informational_report)
    payload["informational_report_delivery_allowed"] = informational_delivery_allowed
    payload["blocked_report_delivery_requested"] = bool(args.allow_blocked_report)
    payload["blocked_report_delivery_allowed"] = blocked_delivery_allowed
    payload["transfer_actions_safe_to_move"] = transfer_report.get("bank_transfer_actions_final") is True

    live_send = bool(args.send and not args.dry_run)
    token, chat_id = telegram.telegram_config() if live_send else ("", "")
    payload["telegram_config_checked"] = live_send
    payload["telegram_token_present"] = bool(token)
    payload["telegram_chat_id_present"] = bool(chat_id)
    if live_send:
        if quality_issues:
            payload.update({"status": "failed", "issue": "message quality/report validation failed"})
            write_report(args.report, payload)
            return 1
        if send_blockers:
            payload.update({"status": "failed", "issue": "transfer reconciliation is not final/safe to send"})
            write_report(args.report, payload)
            return 1
        if not token or not chat_id:
            payload.update({"status": "failed", "issue": "missing telegram bot token or chat id"})
            write_report(args.report, payload)
            return 1
        sent_state = read_json(args.sent_state_file)
        if matching_sent_state(
            sent_state,
            message_sha256=str(payload["message_sha256"]),
            transfer_report_digest=str(payload["transfer_report_digest"] or "") or None,
        ):
            payload.update(
                {
                    "status": "ok_previous",
                    "telegram_send_ok": True,
                    "previous_sent_state_file": str(args.sent_state_file),
                    "previous_sent_at": sent_state.get("sent_at"),
                    "chunk_count": sent_state.get("chunk_count"),
                    "telegram_http_statuses": sent_state.get("telegram_http_statuses") or [],
                    "idempotent_reuse": True,
                }
            )
            write_report(args.report, payload)
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        chunks = telegram.chunk_message(text, telegram.TELEGRAM_SAFE_MESSAGE_LIMIT)
        results = [telegram.post_telegram(token, chat_id, chunk) for chunk in chunks]
        payload["telegram_http_statuses"] = [result.get("http_status") for result in results]
        payload["telegram_send_ok"] = all(bool(result.get("body", {}).get("ok")) for result in results)
        payload["chunk_count"] = len(chunks)
        payload["status"] = "ok" if payload["telegram_send_ok"] else "failed"
        if payload["telegram_send_ok"]:
            write_report(args.sent_state_file, sent_state_payload(payload, text=text))
    else:
        payload.update({"status": "ok_dry_run", "chunk_count": len(telegram.chunk_message(text, telegram.TELEGRAM_SAFE_MESSAGE_LIMIT))})

    write_report(args.report, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] in {"ok", "ok_dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
