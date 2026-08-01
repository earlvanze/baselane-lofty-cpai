from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MADISON_90_CURTAILMENT_CONFIG = ROOT / "config" / "madison_90_principal_curtailments.json"
MADISON_90_CURTAILMENT_MARKER = "AOPS-90-CURTAILMENT"

P_AND_I_DAO_PROPERTIES = {
    "84 Madison Ave",
    "804 S Quitman St",
    "9 Country Club Ln N",
}

NO_DAO_MORTGAGE_PROPERTIES = {
    "85-104 Alawa Pl",
    "86 Madison Ave",
    "88 Madison Ave",
    "90 Madison Ave",
    "724 3rd Ave",
}

YHOME_STOLEN_DEED_MORTGAGE_PROPERTIES = {
    "1845 W 48th St",
    "3139 West Blvd",
    "4318 Clybourne Ave",
}

NO_DAO_MORTGAGE_STATES = {"IL", "OH", "TN"}


def normalize_policy_key(value: Any) -> str:
    text = str(value or "").strip().lower().replace("&", " and ")
    text = re.sub(r",?\s+(al|ar|ca|co|fl|ga|hi|ia|il|mi|mo|ny|oh|sc|tn|tx|ut|wa)\s+\d{5}(?:-\d{4})?", " ", text)
    text = re.sub(r",?\s+(al|ar|ca|co|fl|ga|hi|ia|il|mi|mo|ny|oh|sc|tn|tx|ut|wa)\s*$", " ", text)
    text = re.sub(r"\b(public|dao|llc|lfty\d+)\b", " ", text)
    replacements = {
        "avenue": "ave",
        "street": "st",
        "road": "rd",
        "lane": "ln",
        "drive": "dr",
        "place": "pl",
        "circle": "cir",
        "north": "n",
        "south": "s",
        "east": "e",
        "west": "w",
    }
    for source, target in replacements.items():
        text = re.sub(rf"\b{source}\b", target, text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


NO_DAO_MORTGAGE_PROPERTY_KEYS = tuple(sorted(normalize_policy_key(name) for name in NO_DAO_MORTGAGE_PROPERTIES))
P_AND_I_DAO_PROPERTY_KEYS = tuple(sorted(normalize_policy_key(name) for name in P_AND_I_DAO_PROPERTIES))
YHOME_STOLEN_DEED_MORTGAGE_PROPERTY_KEYS = tuple(
    sorted(normalize_policy_key(name) for name in YHOME_STOLEN_DEED_MORTGAGE_PROPERTIES)
)


def is_no_dao_mortgage_property(value: Any) -> bool:
    haystack = normalize_policy_key(value)
    return any(key and key in haystack for key in NO_DAO_MORTGAGE_PROPERTY_KEYS)


def is_no_dao_mortgage_property_or_state(value: Any) -> bool:
    text = str(value or "")
    if is_yhome_stolen_deed_mortgage_property(text):
        return False
    if is_no_dao_mortgage_property(text):
        return True
    return bool(re.search(r"(?:^|[\s,\/\\])(?:IL|OH|TN)(?:[\s,\/\\]|$)", text, re.IGNORECASE))


def is_yhome_stolen_deed_mortgage_property(value: Any) -> bool:
    haystack = normalize_policy_key(value)
    return any(key and key in haystack for key in YHOME_STOLEN_DEED_MORTGAGE_PROPERTY_KEYS)


def is_p_and_i_dao_property(value: Any) -> bool:
    haystack = normalize_policy_key(value)
    return any(key and key in haystack for key in P_AND_I_DAO_PROPERTY_KEYS)


def madison_90_curtailment_policy() -> dict[str, Any]:
    return json.loads(MADISON_90_CURTAILMENT_CONFIG.read_text(encoding="utf-8"))


def madison_90_curtailment_for_month(month: str) -> Decimal:
    for entry in madison_90_curtailment_policy()["recognition_schedule"]:
        if entry["month"] == month:
            return Decimal(entry["amount"])
    return Decimal("0.00")


def is_approved_madison_90_curtailment(row: dict[str, Any]) -> bool:
    """Accept only an exact configured transaction or a deterministic marker.

    Narrative words such as "principal" or "curtailment" are insufficient.
    The marker must carry a configured recognition month and exact amount.
    """
    policy = madison_90_curtailment_policy()
    configured = {
        str(entry.get("transaction_id")): Decimal(entry["amount"])
        for entry in policy["recognition_schedule"]
        if entry.get("transaction_id") and Decimal(entry["amount"]) != 0
    }
    row_id = str(row.get("id") or "")
    row_amount = abs(Decimal(str(row.get("amount") or 0))).quantize(Decimal("0.01"))
    if row_id in configured:
        return row_amount == configured[row_id]

    note = row.get("note")
    note = str(note.get("text") or "") if isinstance(note, dict) else str(note or "")
    match = re.search(
        rf"{re.escape(policy['marker'])}\|recognition=(\d{{4}}-\d{{2}})\|amount=(\d+\.\d{{2}})",
        note,
    )
    if not match:
        return False
    return (
        row_amount == madison_90_curtailment_for_month(match.group(1))
        and row_amount == Decimal(match.group(2))
        and row_amount != 0
    )
