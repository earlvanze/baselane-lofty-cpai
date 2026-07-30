import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from baselane_mcp.transfers import (  # noqa: E402
    TransferError,
    TransferStateError,
    TransferValidationError,
    build_transfer_plan,
    execute_transfer,
    list_active_transfer_accounts,
    run_graphql_batch_via_cdp,
    run_graphql_via_cdp,
)


def account_response():
    return {
        "data": {
            "bankAccounts": [
                {
                    "id": 101,
                    "transferAccountId": 1001,
                    "availableBalance": 1000.00,
                    "accountName": "Operations",
                    "nickName": "Grape",
                    "institutionName": "Baselane",
                    "accountNumber": "123456789",
                    "accountSubType": "CHECKING",
                    "isExternal": False,
                    "isBankConnected": True,
                    "connectionState": "CONNECTED",
                    "subAccounts": [
                        {
                            "id": 102,
                            "transferAccountId": 1002,
                            "availableBalance": 250.00,
                            "accountName": "Operations",
                            "nickName": "ECO",
                            "institutionName": "Baselane",
                            "accountNumber": "987654321",
                            "accountSubType": "CHECKING",
                            "isExternal": False,
                            "isBankConnected": True,
                            "connectionState": "CONNECTED",
                        }
                    ],
                }
            ]
        }
    }


class TransferPlanTests(unittest.TestCase):
    def test_confirmation_token_is_stable_for_equivalent_amounts(self):
        base = {
            "from_transfer_account_id": 1001,
            "to_transfer_account_id": 1002,
            "bookkeeping_note": "724 | DAO fee | 2025-07",
            "property_id": 33594,
        }
        first = build_transfer_plan(amount="62.50", **base)
        second = build_transfer_plan(amount=62.5, **base)

        self.assertEqual(first["amount"], "62.50")
        self.assertEqual(first["confirmation_token"], second["confirmation_token"])

    def test_rejects_same_account_and_subcent_amounts(self):
        with self.assertRaises(TransferValidationError):
            build_transfer_plan(
                from_transfer_account_id=1001,
                to_transfer_account_id=1001,
                amount="62.50",
                bookkeeping_note="monthly fee",
                property_id=33594,
            )
        with self.assertRaises(TransferValidationError):
            build_transfer_plan(
                from_transfer_account_id=1001,
                to_transfer_account_id=1002,
                amount="62.501",
                bookkeeping_note="monthly fee",
                property_id=33594,
            )

    def test_same_day_requires_today(self):
        with self.assertRaises(TransferValidationError):
            build_transfer_plan(
                from_transfer_account_id=1001,
                to_transfer_account_id=1002,
                amount="62.50",
                bookkeeping_note="monthly fee",
                property_id=33594,
                transfer_date="2099-01-01",
                same_day=True,
            )

    def test_rejects_cash_flow_category_on_internal_transfer(self):
        with self.assertRaisesRegex(
            TransferValidationError,
            "must use tag_id 24",
        ):
            build_transfer_plan(
                from_transfer_account_id=1001,
                to_transfer_account_id=1002,
                amount="62.50",
                bookkeeping_note="monthly PM fee settlement",
                property_id=33594,
                tag_id=80,
            )


class TransferExecutionTests(unittest.TestCase):
    def test_graphql_batch_uses_one_bridge_process_and_preserves_order(self):
        completed = type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "batchResults": [
                            {"data": {"property": [{"id": "1"}]}},
                            {"data": {"tag": [{"id": "2"}]}},
                        ]
                    }
                ),
                "stderr": "",
            },
        )()
        operations = [
            {"operationName": "PropertyList"},
            {"operationName": "TagList"},
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch(
                "baselane_mcp.transfers.subprocess.run", return_value=completed
            ) as run:
                results = run_graphql_batch_via_cdp(
                    operations,
                    bridge_path=Path(temporary_directory) / "bridge.js",
                    workspace_root=Path(temporary_directory),
                )

        self.assertEqual(results[0]["data"]["property"][0]["id"], "1")
        self.assertEqual(results[1]["data"]["tag"][0]["id"], "2")
        self.assertEqual(run.call_count, 1)

    def test_graphql_batch_rejects_incomplete_response(self):
        with patch(
            "baselane_mcp.transfers.run_graphql_via_cdp",
            return_value={"batchResults": [{"data": {}}]},
        ):
            with self.assertRaisesRegex(TransferStateError, "incomplete"):
                run_graphql_batch_via_cdp(
                    [{"operationName": "One"}, {"operationName": "Two"}],
                    bridge_path=Path("/tmp/bridge.js"),
                    workspace_root=Path("/tmp"),
                )

    def test_cdp_bridge_otp_required_is_a_definite_retryable_rejection(self):
        completed = type(
            "Completed",
            (),
            {
                "returncode": 1,
                "stdout": "",
                "stderr": (
                    "GRAPHQL_ERRORS: [{\"message\":\"Otp for user redacted "
                    "has not been completed\",\"extensions\":{\"code\":"
                    "\"OTP_REQUIRED\"}}]"
                ),
            },
        )()
        with tempfile.TemporaryDirectory() as temporary_directory:
            bridge = Path(temporary_directory) / "bridge.js"
            with patch("baselane_mcp.transfers.subprocess.run", return_value=completed):
                with self.assertRaisesRegex(
                    TransferValidationError,
                    "bank OTP has not been completed",
                ):
                    run_graphql_via_cdp(
                        {"operationName": "createTransfer"},
                        bridge_path=bridge,
                        workspace_root=Path(temporary_directory),
                    )

    def test_account_listing_masks_sensitive_numbers(self):
        accounts = list_active_transfer_accounts(lambda _: account_response())

        self.assertEqual(len(accounts), 2)
        self.assertEqual(accounts[0]["account_number"], "••••4321")
        self.assertNotIn("routing_number", accounts[0])
        self.assertNotIn("plaid_account_id", accounts[0])
        self.assertTrue(all(account["eligible_for_internal_transfer"] for account in accounts))

    def test_external_recipient_is_excluded_and_never_submitted(self):
        response = account_response()
        response["data"]["bankAccounts"][0]["subAccounts"][0]["isExternal"] = True
        plan = build_transfer_plan(
            from_transfer_account_id=1001,
            to_transfer_account_id=1002,
            amount="62.50",
            bookkeeping_note="monthly fee",
            property_id=33594,
        )
        operations = []

        def runner(payload):
            operations.append(payload["operationName"])
            if payload["operationName"] == "BankAccountsActive":
                return response
            self.fail("createTransfer must not run for an external recipient")

        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(
                TransferValidationError, "internal Baselane workspace account"
            ):
                execute_transfer(
                    plan=plan,
                    confirmation_token=plan["confirmation_token"],
                    graphql_runner=runner,
                    state_path=Path(temporary_directory) / "state.json",
                )

        self.assertEqual(operations, ["BankAccountsActive"])

    def test_execute_is_balance_checked_and_idempotent(self):
        plan = build_transfer_plan(
            from_transfer_account_id=1001,
            to_transfer_account_id=1002,
            amount="62.50",
            bookkeeping_note="724 | DAO registration/admin fee | 2025-07",
            property_id=33594,
        )
        calls = []

        def runner(payload):
            calls.append(payload["operationName"])
            if payload["operationName"] == "BankAccountsActive":
                return account_response()
            return {
                "data": {
                    "createTransfer": {
                        "id": "transfer-1",
                        "status": "COMPLETED",
                        "amount": 62.5,
                        "createdAt": "2026-07-27T05:43:11.000Z",
                        "transferDate": date.today().isoformat(),
                        "expectedArrivalDate": None,
                        "fromTransferAccountId": 1001,
                        "toTransferAccountId": 1002,
                        "type": "INTERNAL_TRANSFER",
                        "typeName": "Internal Transfer",
                    }
                }
            }

        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "transfer-state.json"
            first = execute_transfer(
                plan=plan,
                confirmation_token=plan["confirmation_token"],
                graphql_runner=runner,
                state_path=state_path,
            )
            second = execute_transfer(
                plan=plan,
                confirmation_token=plan["confirmation_token"],
                graphql_runner=runner,
                state_path=state_path,
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(first["status"], "completed")
        self.assertFalse(first["idempotent"])
        self.assertEqual(second["status"], "already_completed")
        self.assertTrue(second["idempotent"])
        self.assertEqual(calls, ["BankAccountsActive", "createTransfer"])
        self.assertEqual(
            state["transfers"][plan["confirmation_token"]]["status"], "completed"
        )

    def test_wrong_confirmation_token_never_calls_graphql(self):
        plan = build_transfer_plan(
            from_transfer_account_id=1001,
            to_transfer_account_id=1002,
            amount="62.50",
            bookkeeping_note="monthly fee",
            property_id=33594,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(TransferValidationError):
                execute_transfer(
                    plan=plan,
                    confirmation_token="wrong",
                    graphql_runner=lambda _: self.fail("GraphQL must not run"),
                    state_path=Path(temporary_directory) / "state.json",
                )

    def test_failed_submission_is_left_blocked_for_reconciliation(self):
        plan = build_transfer_plan(
            from_transfer_account_id=1001,
            to_transfer_account_id=1002,
            amount="62.50",
            bookkeeping_note="monthly fee",
            property_id=33594,
        )
        mutation_calls = 0

        def runner(payload):
            nonlocal mutation_calls
            if payload["operationName"] == "BankAccountsActive":
                return account_response()
            mutation_calls += 1
            raise TransferError("transport failed")

        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "state.json"
            with self.assertRaises(TransferStateError):
                execute_transfer(
                    plan=plan,
                    confirmation_token=plan["confirmation_token"],
                    graphql_runner=runner,
                    state_path=state_path,
                )
            with self.assertRaises(TransferStateError):
                execute_transfer(
                    plan=plan,
                    confirmation_token=plan["confirmation_token"],
                    graphql_runner=runner,
                    state_path=state_path,
                )

        self.assertEqual(mutation_calls, 1)

    def test_definite_graphql_rejection_is_recorded_as_retryable(self):
        plan = build_transfer_plan(
            from_transfer_account_id=1001,
            to_transfer_account_id=1002,
            amount="62.50",
            bookkeeping_note="monthly fee",
            property_id=33594,
        )
        mutation_calls = 0

        def runner(payload):
            nonlocal mutation_calls
            if payload["operationName"] == "BankAccountsActive":
                return account_response()
            mutation_calls += 1
            if mutation_calls == 1:
                raise TransferValidationError("Baselane rejected the transfer")
            return {
                "data": {
                    "createTransfer": {
                        "id": "transfer-2",
                        "status": "COMPLETED",
                        "amount": 62.5,
                        "createdAt": "2026-07-27T05:43:11.000Z",
                        "transferDate": date.today().isoformat(),
                        "expectedArrivalDate": None,
                        "fromTransferAccountId": 1001,
                        "toTransferAccountId": 1002,
                        "type": "INTERNAL_TRANSFER",
                        "typeName": "Internal Transfer",
                    }
                }
            }

        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "state.json"
            with self.assertRaises(TransferValidationError):
                execute_transfer(
                    plan=plan,
                    confirmation_token=plan["confirmation_token"],
                    graphql_runner=runner,
                    state_path=state_path,
                )
            rejected = json.loads(state_path.read_text(encoding="utf-8"))
            result = execute_transfer(
                plan=plan,
                confirmation_token=plan["confirmation_token"],
                graphql_runner=runner,
                state_path=state_path,
            )

        self.assertEqual(
            rejected["transfers"][plan["confirmation_token"]]["status"], "rejected"
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(mutation_calls, 2)

    def test_otp_required_is_recorded_as_resumable_authentication_challenge(self):
        plan = build_transfer_plan(
            from_transfer_account_id=1001,
            to_transfer_account_id=1002,
            amount="62.50",
            bookkeeping_note="monthly fee",
            property_id=33594,
        )
        mutation_calls = 0

        def runner(payload):
            nonlocal mutation_calls
            if payload["operationName"] == "BankAccountsActive":
                return account_response()
            mutation_calls += 1
            if mutation_calls == 1:
                raise TransferError(
                    'GRAPHQL_ERRORS: [{"extensions":{"code":"OTP_REQUIRED"}}]'
                )
            return {
                "data": {
                    "createTransfer": {
                        "id": "transfer-after-otp",
                        "status": "COMPLETED",
                        "amount": 62.5,
                        "createdAt": "2026-07-29T05:43:11.000Z",
                        "transferDate": date.today().isoformat(),
                        "expectedArrivalDate": None,
                        "fromTransferAccountId": 1001,
                        "toTransferAccountId": 1002,
                        "type": "INTERNAL_TRANSFER",
                        "typeName": "Internal Transfer",
                    }
                }
            }

        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "state.json"
            with self.assertRaisesRegex(TransferStateError, "SMS OTP is required"):
                execute_transfer(
                    plan=plan,
                    confirmation_token=plan["confirmation_token"],
                    graphql_runner=runner,
                    state_path=state_path,
                )
            challenged = json.loads(state_path.read_text(encoding="utf-8"))
            result = execute_transfer(
                plan=plan,
                confirmation_token=plan["confirmation_token"],
                graphql_runner=runner,
                state_path=state_path,
            )

        self.assertEqual(
            challenged["transfers"][plan["confirmation_token"]]["status"],
            "authentication_challenge",
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(mutation_calls, 2)


if __name__ == "__main__":
    unittest.main()
