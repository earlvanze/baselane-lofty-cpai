from __future__ import annotations

import baselane_audit_ny_mortgage_graph as audit


def test_state_query_conflict_uses_authoritative_transaction_lookup(monkeypatch):
    stale_active = {"id": "42", "hidden": False, "isDeleted": False, "pending": False}
    stale_deleted = {"id": "42", "hidden": False, "isDeleted": True, "pending": False}
    authoritative = {"id": "42", "hidden": False, "isDeleted": False, "pending": False}
    monkeypatch.setattr(
        audit, "query_transactions_by_id", lambda ids: [authoritative] if ids == ["42"] else []
    )

    assert audit.resolve_state_query_duplicates([stale_active, stale_deleted]) == [
        authoritative
    ]


def test_identical_state_duplicates_do_not_trigger_live_lookup(monkeypatch):
    row = {"id": "42", "hidden": False, "isDeleted": False, "pending": False}

    def unexpected_lookup(_ids):
        raise AssertionError("identical state copies must not trigger a lookup")

    monkeypatch.setattr(audit, "query_transactions_by_id", unexpected_lookup)
    assert audit.resolve_state_query_duplicates([row, dict(row)]) == [row]
