from decimal import Decimal

import baselane_restore_madison_90_2024_manual_mortgage_components as repair


def test_statement_components_reconcile_to_each_composite_root():
    for payment in repair.PAYMENTS:
        component_total = sum(
            (repair.money(component[1]) for component in payment["components"]),
            Decimal("0.00"),
        )
        assert component_total == -repair.money(payment["root"])


def test_only_statement_backed_curtailments_are_dao_principal():
    amounts = [
        repair.money(component[1])
        for payment in repair.PAYMENTS
        for component in payment["components"]
        if component[0] == "principal-curtailment"
    ]
    assert amounts == [
        Decimal("-550.00"),
        Decimal("-2950.00"),
        Decimal("-1150.00"),
        Decimal("-800.00"),
    ]
