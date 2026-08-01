import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import baselane_no_dao_mortgage_cash_basis as cash_basis
from coownership_mortgage_policy import madison_90_curtailment_for_month


class NoDaoMortgageCashBasisTests(unittest.TestCase):
    def row(self, **updates):
        row = {
            "id": "1",
            "date": "2026-07-01",
            "amount": -100,
            "merchantName": "FREEDOM",
            "propertyId": "33594",
            "tagId": "33",
            "note": None,
            "isManual": False,
            "hidden": False,
            "isDeleted": False,
        }
        row.update(updates)
        return row

    def test_real_mortgage_cash_is_reclassified(self):
        self.assertEqual(cash_basis.classify_action(self.row()), "reclassify_transfer")

    def test_standalone_manual_escrow_is_deleted(self):
        row = self.row(
            merchantName="Escrow - 86 Madison Ave",
            propertyId="63162",
            isManual=True,
            parentId=None,
        )
        self.assertEqual(cash_basis.classify_action(row), "delete_manual_escrow")

    def test_native_split_escrow_child_is_preserved(self):
        row = self.row(
            merchantName="724 3rd Ave Mortgage Escrow - Insurance",
            tagId="8",
            isManual=True,
            parentId="parent-1",
        )
        self.assertIsNone(cash_basis.classify_action(row))

    def test_approved_90_curtailment_is_preserved(self):
        row = self.row(
            id="137322433",
            date="2025-02-04",
            amount=-350,
            merchantName="Citadel Servicing",
            propertyId="31525",
            tagId="20",
            parentId="136380083",
        )
        self.assertIsNone(cash_basis.classify_action(row))

    def test_unapproved_90_principal_is_reclassified(self):
        row = self.row(
            id="ordinary-principal",
            date="2025-02-04",
            amount=-190.87,
            merchantName="Citadel Servicing principal",
            propertyId="31525",
            tagId="20",
            parentId="136380083",
        )
        self.assertEqual(cash_basis.classify_action(row), "reclassify_transfer")

    def test_july_2025_has_no_recognized_curtailment(self):
        self.assertEqual(
            str(madison_90_curtailment_for_month("2025-07")),
            "0.00",
        )

    def test_alawa_child_is_not_reclassified_by_generic_policy(self):
        parent = self.row(
            id="parent-1",
            merchantName="loanDepot.com, L",
            propertyId="73461",
            isSplit=True,
        )
        child = self.row(
            id="child-1",
            merchantName="85-104 Alawa Pl Mortgage Principal",
            propertyId="37648",
            tagId="20",
            parentId="parent-1",
            isManual=True,
        )
        actions = cash_basis.build_actions([parent, child])
        self.assertEqual(actions, [])

    def test_alawa_siblings_are_not_reclassified_by_generic_policy(self):
        escrow_child = self.row(
            id="escrow-child",
            merchantName="85-104 Alawa Pl Mortgage Escrow - Insurance",
            propertyId="73461",
            tagId="8",
            parentId="omitted-parent",
            isManual=True,
        )
        principal_child = self.row(
            id="principal-child",
            merchantName="loanDepot principal",
            propertyId="37648",
            tagId="20",
            parentId="omitted-parent",
            isManual=True,
        )
        actions = cash_basis.build_actions([escrow_child, principal_child])
        self.assertEqual(actions, [])

    def test_actual_tax_bill_is_untouched(self):
        row = self.row(merchantName="CUYAHOGA COUNTY TREASURER", tagId="15")
        self.assertIsNone(cash_basis.classify_action(row))

    def test_alawa_components_are_owned_by_dedicated_cleanup(self):
        parent = self.row(
            id="alawa-parent",
            merchantName="loanDepot.com, L",
            propertyId="73461",
            isSplit=True,
        )
        child = self.row(
            id="alawa-principal",
            merchantName="85-104 Alawa Pl Mortgage Principal",
            propertyId="37648",
            tagId="20",
            parentId="alawa-parent",
        )
        self.assertEqual(cash_basis.build_actions([parent, child]), [])

    def test_digest_is_order_independent_after_build(self):
        first = cash_basis.build_actions([self.row(id="2"), self.row(id="1")])
        second = cash_basis.build_actions([self.row(id="1"), self.row(id="2")])
        self.assertEqual(cash_basis.action_digest(first), cash_basis.action_digest(second))


if __name__ == "__main__":
    unittest.main()
