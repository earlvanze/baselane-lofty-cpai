"""Guarded cash transfers between accounts inside one Baselane workspace."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Callable, Iterator
import uuid


TRANSFER_ACCOUNTS_QUERY = """
query BankAccountsActive($isConnectedAccount: Boolean, $accountStatus: BankAccountStatus, $isTransferable: Boolean) {
  bankAccounts(
    input: {isConnectedAccount: $isConnectedAccount, accountStatus: $accountStatus, isTransferable: $isTransferable}
  ) {
    accountStatus
    ...ActiveBankAccountsObj
    limits {
      ...AccountLimitObj
      __typename
    }
    subAccounts(
      input: {isConnectedAccount: $isConnectedAccount, accountStatus: $accountStatus, isTransferable: $isTransferable}
    ) {
      ...ActiveBankAccountsObj
      limits {
        ...AccountLimitObj
        __typename
      }
      __typename
    }
    __typename
  }
}

fragment ActiveBankAccountsObj on BankAccount {
  id
  transferAccountId
  availableBalance
  isExternal
  accountSubType
  institutionName
  nickName
  accountName
  accountNumber
  isBankConnected
  connectionState
  provider
  __typename
}

fragment AccountLimitObj on AccountLimits {
  dailyCreditLimit
  dailyCreditTotal
  monthlyCreditLimit
  monthlyCreditTotal
  dailyDebitLimit
  dailyDebitTotal
  monthlyDebitLimit
  monthlyDebitTotal
  __typename
}
""".strip()


CREATE_TRANSFER_MUTATION = """
mutation createTransfer($input: CreateTransferInput!) {
  createTransfer(input: $input) {
    amount
    createdAt
    expectedArrivalDate
    fromTransferAccountId
    id
    note
    status
    toTransferAccountId
    transferDate
    type
    typeName
    userId
    __typename
  }
}
""".strip()


class TransferError(RuntimeError):
    """Base exception for guarded transfer failures."""


class TransferValidationError(TransferError):
    """The requested transfer is invalid or failed a preflight check."""


class TransferStateError(TransferError):
    """A prior transfer attempt is incomplete and must be reconciled."""


class TransferAuthenticationRequired(TransferStateError):
    """Baselane rejected the transfer before cash movement pending bank MFA."""

    def __init__(
        self,
        message: str,
        *,
        bank_account_id: int | None,
        confirmation_token: str,
    ) -> None:
        super().__init__(message)
        self.bank_account_id = bank_account_id
        self.confirmation_token = confirmation_token
        self.cash_movement_may_require_reconciliation = False


GraphQLRunner = Callable[[dict[str, Any]], dict[str, Any]]


def _parse_positive_id(value: int | str, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TransferValidationError(f"{field} must be a positive integer") from exc
    if parsed <= 0:
        raise TransferValidationError(f"{field} must be a positive integer")
    return parsed


def _parse_amount(value: int | float | str | Decimal) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TransferValidationError("amount must be a valid decimal number") from exc
    if not amount.is_finite() or amount <= 0:
        raise TransferValidationError("amount must be greater than zero")
    if amount.as_tuple().exponent < -2:
        raise TransferValidationError("amount may have at most two decimal places")
    return amount.quantize(Decimal("0.01"))


def _parse_transfer_date(value: str | None) -> str:
    raw = value or date.today().isoformat()
    try:
        parsed = date.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise TransferValidationError("transfer_date must use YYYY-MM-DD") from exc
    if parsed < date.today():
        raise TransferValidationError("transfer_date cannot be in the past")
    return parsed.isoformat()


def build_transfer_plan(
    *,
    from_transfer_account_id: int | str,
    to_transfer_account_id: int | str,
    amount: int | float | str | Decimal,
    bookkeeping_note: str,
    property_id: int | str,
    tag_id: int | str = 24,
    transfer_date: str | None = None,
    same_day: bool = True,
) -> dict[str, Any]:
    """Normalize a transfer request and derive its exact confirmation token."""
    source_id = _parse_positive_id(from_transfer_account_id, "from_transfer_account_id")
    destination_id = _parse_positive_id(to_transfer_account_id, "to_transfer_account_id")
    if source_id == destination_id:
        raise TransferValidationError("source and destination transfer accounts must differ")

    normalized_amount = _parse_amount(amount)
    normalized_date = _parse_transfer_date(transfer_date)
    if same_day and normalized_date != date.today().isoformat():
        raise TransferValidationError("same_day=true requires today's transfer_date")

    note = " ".join(str(bookkeeping_note).split())
    if not note:
        raise TransferValidationError("bookkeeping_note is required")
    if len(note) > 255:
        raise TransferValidationError("bookkeeping_note must be 255 characters or fewer")

    normalized_tag_id = _parse_positive_id(tag_id, "tag_id")
    if normalized_tag_id != 24:
        raise TransferValidationError(
            "internal cash transfers must use tag_id 24 (Transfers Between Accounts)"
        )

    plan = {
        "from_transfer_account_id": source_id,
        "to_transfer_account_id": destination_id,
        "amount": format(normalized_amount, ".2f"),
        "bookkeeping_note": note,
        "type": "INTERNAL_TRANSFER",
        "transfer_date": normalized_date,
        "tag_id": str(normalized_tag_id),
        "property_id": str(_parse_positive_id(property_id, "property_id")),
        "same_day": bool(same_day),
    }
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    plan["confirmation_token"] = "BASELANE-TRANSFER-" + hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    return plan


def _mask_account_number(value: Any) -> str | None:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return f"••••{digits[-4:]}" if digits else None


def _flatten_accounts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    body = payload.get("data") or {}
    parents = body.get("bankAccounts") or []
    flattened: list[dict[str, Any]] = []
    for parent in parents:
        if not isinstance(parent, dict):
            continue
        parent_id = parent.get("id")
        flattened.append({**parent, "_parent_bank_account_id": parent_id})
        for subaccount in parent.get("subAccounts") or []:
            if isinstance(subaccount, dict):
                flattened.append(
                    {**subaccount, "_parent_bank_account_id": parent_id}
                )
    return flattened


def _safe_account(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "transfer_account_id": account.get("transferAccountId"),
        "bank_account_id": account.get("id"),
        "mfa_bank_account_id": account.get("_parent_bank_account_id"),
        "account_name": account.get("accountName"),
        "nickname": account.get("nickName"),
        "institution": account.get("institutionName"),
        "account_subtype": account.get("accountSubType"),
        "account_number": _mask_account_number(account.get("accountNumber")),
        "available_balance": account.get("availableBalance"),
        "is_external": account.get("isExternal"),
        "is_bank_connected": account.get("isBankConnected"),
        "connection_state": account.get("connectionState"),
        "eligible_for_internal_transfer": (
            account.get("isExternal") is False
            and account.get("isBankConnected") is True
            and account.get("transferAccountId") is not None
        ),
    }


def list_active_transfer_accounts(graphql_runner: GraphQLRunner) -> list[dict[str, Any]]:
    """Return eligible in-workspace accounts without routing or full account numbers."""
    response = graphql_runner(
        {
            "operationName": "BankAccountsActive",
            "variables": {
                "isConnectedAccount": True,
                "accountStatus": "Open",
            },
            "query": TRANSFER_ACCOUNTS_QUERY,
        }
    )
    if response.get("errors"):
        raise TransferError("Baselane rejected the transferable-account query")
    accounts = [
        _safe_account(account)
        for account in _flatten_accounts(response)
        if (
            account.get("transferAccountId") is not None
            and account.get("isExternal") is False
            and account.get("isBankConnected") is True
        )
    ]
    return sorted(
        accounts,
        key=lambda account: (
            str(account.get("institution") or ""),
            str(account.get("nickname") or account.get("account_name") or ""),
        ),
    )


def run_graphql_via_cdp(
    payload: dict[str, Any],
    *,
    bridge_path: Path,
    workspace_root: Path,
    timeout: int = 90,
) -> dict[str, Any]:
    """Execute one GraphQL operation using the authenticated Baselane session."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".json",
        prefix="baselane-transfer-",
        delete=False,
    ) as handle:
        json.dump(payload, handle, separators=(",", ":"))
        input_path = Path(handle.name)
    try:
        result = subprocess.run(
            ["node", str(bridge_path), str(input_path)],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={
                **os.environ,
                "OPENCLAW_WORKSPACE_ROOT": str(workspace_root),
            },
        )
    except subprocess.TimeoutExpired as exc:
        raise TransferStateError(
            "Baselane GraphQL timed out; do not retry a transfer until its status is reconciled"
        ) from exc
    finally:
        input_path.unlink(missing_ok=True)

    if result.returncode != 0:
        message = result.stderr or result.stdout or "GraphQL bridge failed"
        normalized = " ".join(message.split())
        if (
            "OTP_REQUIRED" in normalized
            or (
                "Otp for user" in normalized
                and "has not been completed" in normalized
            )
        ):
            raise TransferValidationError(
                "Baselane bank OTP has not been completed"
            )
        raise TransferError(normalized[-800:])
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise TransferStateError(
            "Baselane returned an unreadable transfer response; reconcile before retrying"
        ) from exc
    if response.get("errors"):
        messages = []
        for error in response["errors"][:3]:
            if isinstance(error, dict) and error.get("message"):
                messages.append(" ".join(str(error["message"]).split())[:300])
        detail = "; ".join(messages) or "unspecified GraphQL error"
        raise TransferValidationError(
            f"Baselane rejected the GraphQL operation: {detail}"
        )
    return response


def run_graphql_batch_via_cdp(
    operations: list[dict[str, Any]],
    *,
    bridge_path: Path,
    workspace_root: Path,
    timeout: int = 90,
) -> list[dict[str, Any]]:
    """Execute multiple GraphQL operations through one bridge process/session."""
    if not operations:
        return []
    response = run_graphql_via_cdp(
        {"batchOperations": operations},
        bridge_path=bridge_path,
        workspace_root=workspace_root,
        timeout=timeout,
    )
    results = response.get("batchResults")
    if results is None and len(operations) == 1:
        results = [response]
    if not isinstance(results, list) or len(results) != len(operations):
        raise TransferStateError(
            "Baselane returned an incomplete GraphQL batch response; reconcile before retrying"
        )
    for result in results:
        if not isinstance(result, dict):
            raise TransferStateError(
                "Baselane returned an unreadable GraphQL batch item; reconcile before retrying"
            )
        if result.get("errors"):
            messages = [
                " ".join(str(error.get("message") or "").split())[:300]
                for error in result["errors"][:3]
                if isinstance(error, dict) and error.get("message")
            ]
            detail = "; ".join(messages) or "unspecified GraphQL error"
            raise TransferValidationError(
                f"Baselane rejected a batched GraphQL operation: {detail}"
            )
    return results


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "transfers": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TransferStateError(f"cannot read transfer state at {path}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("transfers"), dict):
        raise TransferStateError(f"invalid transfer state at {path}")
    return payload


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=path.name + ".",
        dir=path.parent,
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary_path = Path(handle.name)
    os.replace(temporary_path, path)


def get_transfer_state(
    *, confirmation_token: str, state_path: Path
) -> dict[str, Any]:
    """Return the durable state for one exact transfer without exposing secrets."""
    if not confirmation_token.startswith("BASELANE-TRANSFER-"):
        raise TransferValidationError("confirmation_token is invalid")
    with _state_lock(state_path):
        transfer = _load_state(state_path)["transfers"].get(confirmation_token)
    if transfer is None:
        return {
            "status": "not_found",
            "confirmation_token": confirmation_token,
            "cash_movement_may_require_reconciliation": False,
        }
    status = transfer.get("status")
    return {
        "status": status,
        "confirmation_token": confirmation_token,
        "cash_movement_may_require_reconciliation": status
        in {"submitting", "verification_failed"},
        "retry_safe": status in {"authentication_challenge", "rejected"},
        "challenge_type": transfer.get("challenge_type"),
        "mfa_bank_account_id": transfer.get("mfa_bank_account_id"),
        "plan": transfer.get("plan"),
        "receipt": transfer.get("receipt"),
    }


@contextmanager
def _state_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _account_by_transfer_id(
    accounts: list[dict[str, Any]], transfer_account_id: int
) -> dict[str, Any] | None:
    for account in accounts:
        try:
            candidate = int(account["transfer_account_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if candidate == transfer_account_id:
            return account
    return None


def _safe_transfer_receipt(transfer: dict[str, Any]) -> dict[str, Any]:
    return {
        "transfer_id": transfer.get("id"),
        "status": transfer.get("status"),
        "amount": transfer.get("amount"),
        "created_at": transfer.get("createdAt"),
        "transfer_date": transfer.get("transferDate"),
        "expected_arrival_date": transfer.get("expectedArrivalDate"),
        "from_transfer_account_id": transfer.get("fromTransferAccountId"),
        "to_transfer_account_id": transfer.get("toTransferAccountId"),
        "type": transfer.get("type"),
        "type_name": transfer.get("typeName"),
    }


def execute_transfer(
    *,
    plan: dict[str, Any],
    confirmation_token: str,
    graphql_runner: GraphQLRunner,
    state_path: Path,
) -> dict[str, Any]:
    """Preflight and submit one exactly confirmed Baselane internal transfer."""
    expected_token = plan.get("confirmation_token")
    if not confirmation_token or confirmation_token != expected_token:
        raise TransferValidationError(
            "confirmation_token does not match this exact transfer preview"
        )

    with _state_lock(state_path):
        state = _load_state(state_path)
        prior = state["transfers"].get(expected_token)
        if prior and prior.get("status") == "completed":
            return {
                "status": "already_completed",
                "idempotent": True,
                "receipt": prior.get("receipt"),
            }
        if prior and prior.get("status") in {"submitting", "verification_failed"}:
            raise TransferStateError(
                "a prior submission may have moved cash; reconcile it before retrying"
            )

        accounts = list_active_transfer_accounts(graphql_runner)
        source = _account_by_transfer_id(
            accounts, plan["from_transfer_account_id"]
        )
        destination = _account_by_transfer_id(
            accounts, plan["to_transfer_account_id"]
        )
        if source is None:
            raise TransferValidationError(
                "source is not an eligible internal Baselane workspace account"
            )
        if destination is None:
            raise TransferValidationError(
                "destination is not an eligible internal Baselane workspace account"
            )
        if source.get("is_external") is not False:
            raise TransferValidationError(
                "source must be an internal account in this Baselane workspace"
            )
        if destination.get("is_external") is not False:
            raise TransferValidationError(
                "destination must be an internal account in this Baselane workspace"
            )
        if source.get("is_bank_connected") is not True:
            raise TransferValidationError(
                "source must be a connected Baselane workspace account"
            )
        if destination.get("is_bank_connected") is not True:
            raise TransferValidationError(
                "destination must be a connected Baselane workspace account"
            )

        try:
            available = Decimal(str(source.get("available_balance")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise TransferValidationError(
                "source account available balance could not be verified"
            ) from exc
        amount = Decimal(plan["amount"])
        if available < amount:
            raise TransferValidationError(
                f"insufficient available balance: {format(available, '.2f')} available"
            )

        state["transfers"][expected_token] = {
            "status": "submitting",
            "plan": plan,
        }
        _write_state(state_path, state)

        variables = {
            "input": {
                "fromTransferAccountId": plan["from_transfer_account_id"],
                "toTransferAccountId": plan["to_transfer_account_id"],
                "amount": float(amount),
                "bookKeepingNote": plan["bookkeeping_note"],
                "type": plan["type"],
                "transferDate": plan["transfer_date"],
                "tagId": plan["tag_id"],
                "propertyId": plan["property_id"],
                "sameDay": plan["same_day"],
            }
        }
        try:
            response = graphql_runner(
                {
                    "operationName": "createTransfer",
                    "requestHeaders": {
                        "x-idempotency-key": str(
                            uuid.uuid5(uuid.NAMESPACE_URL, expected_token)
                        )
                    },
                    "variables": variables,
                    "query": CREATE_TRANSFER_MUTATION,
                }
            )
        except Exception as exc:
            normalized_error = str(exc).upper()
            if "OTP_REQUIRED" in normalized_error or (
                "BANK OTP HAS NOT BEEN COMPLETED" in normalized_error
            ) or (
                "OTP FOR USER" in normalized_error
                and "HAS NOT BEEN COMPLETED" in normalized_error
            ):
                # Baselane rejects createTransfer before creating a transfer
                # when its SMS gate is incomplete. The CDP bridge may surface
                # this as either a validation error or a generic transfer error.
                state["transfers"][expected_token] = {
                    "status": "authentication_challenge",
                    "challenge_type": "bank_sms_otp",
                    "mfa_bank_account_id": source.get("mfa_bank_account_id"),
                    "plan": plan,
                }
                _write_state(state_path, state)
                raise TransferAuthenticationRequired(
                    "Baselane bank SMS OTP is required; complete MFA and retry "
                    "this exact confirmation token",
                    bank_account_id=source.get("mfa_bank_account_id"),
                    confirmation_token=expected_token,
                ) from exc
            if "REQUEST IS ALREADY IN PROGRESS" in normalized_error:
                state["transfers"][expected_token] = {
                    "status": "verification_failed",
                    "plan": plan,
                    "error": str(exc),
                }
                _write_state(state_path, state)
                raise TransferStateError(
                    "Baselane reports this idempotent request is already in progress; "
                    "reconcile before retrying"
                ) from exc
            if isinstance(exc, TransferValidationError):
                state["transfers"][expected_token] = {
                    "status": "rejected",
                    "plan": plan,
                    "error": str(exc),
                }
                _write_state(state_path, state)
                raise
            state["transfers"][expected_token] = {
                "status": "verification_failed",
                "plan": plan,
                "error": str(exc),
            }
            _write_state(state_path, state)
            raise TransferStateError(
                "transfer submission outcome is unknown; reconcile before retrying"
            ) from exc

        transfer = (response.get("data") or {}).get("createTransfer")
        if not isinstance(transfer, dict):
            raise TransferStateError(
                "Baselane did not return a transfer receipt; reconcile before retrying"
            )
        receipt = _safe_transfer_receipt(transfer)
        verified = (
            str(receipt.get("from_transfer_account_id"))
            == str(plan["from_transfer_account_id"])
            and str(receipt.get("to_transfer_account_id"))
            == str(plan["to_transfer_account_id"])
            and _parse_amount(receipt.get("amount")) == amount
            and receipt.get("type") == "INTERNAL_TRANSFER"
            and receipt.get("transfer_date") == plan["transfer_date"]
            and bool(receipt.get("transfer_id"))
        )
        if not verified:
            state["transfers"][expected_token] = {
                "status": "verification_failed",
                "plan": plan,
                "receipt": receipt,
            }
            _write_state(state_path, state)
            raise TransferStateError(
                "Baselane receipt did not match the requested transfer; reconcile before retrying"
            )

        state["transfers"][expected_token] = {
            "status": "completed",
            "plan": plan,
            "receipt": receipt,
        }
        _write_state(state_path, state)
        return {
            "status": "completed",
            "idempotent": False,
            "source": source,
            "destination": destination,
            "receipt": receipt,
        }
