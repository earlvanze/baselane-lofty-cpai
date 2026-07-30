import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import baselane_statements_operator as operator


class BaselaneStatementsOperatorTest(unittest.TestCase):
    def test_dest_rules_do_not_route_to_legacy_lfty_folders(self):
        for _, rel in operator.DEST_RULES:
            with self.subTest(rel=rel):
                self.assertFalse(operator.has_legacy_lfty_folder(Path(rel)))

    def test_resolve_dest_uses_canonical_property_folder(self):
        base = Path('/real-estate')
        dest = operator.resolve_dest(
            base,
            'BASELANE_BLACK CANNON LFTY0300 DAO LLC - 1315 E 114TH ST_'
            '1315 E 114TH ST OPERATIONS_MAY_2026_STATEMENT.pdf',
            2026,
        )
        self.assertEqual(
            dest,
            base
            / 'OH/1315 E 114th St, Cleveland, OH 44106/Public/07 - P&L & Owner Statements/Bank Statements/2026',
        )

    def test_madison_dest_rules_use_public_owner_statement_folder(self):
        base = Path('/real-estate')
        for address in ('84 MADISON AVE', '86 MADISON AVE', '88 MADISON AVE', '90 MADISON AVE'):
            with self.subTest(address=address):
                dest = operator.resolve_dest(
                    base,
                    f'BASELANE_TEST DAO LLC - {address}_{address} OPERATIONS_JUN_2026_STATEMENT.pdf',
                    2026,
                )

                self.assertIn('/Public/07 - P&L & Owner Statements/Bank Statements/2026', dest.as_posix())
                self.assertNotIn('/NY/88 Madison Ave Albany, NY 12202/07 - P&L', dest.as_posix())

    def test_migrate_legacy_lfty_statement_folders_moves_and_deletes_empty_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = (
                root
                / 'TX/LFTY0142 5401 Odom Ave Fort Worth, TX 76114/Public/07 - P&L & Owner Statements/'
                'Bank Statements/2026/BASELANE_BEAR LFTY0142 DAO LLC - 5401 ODOM AVE_'
                '5401 ODOM AVE OPERATIONS_MAY_2026_STATEMENT.pdf'
            )
            source.parent.mkdir(parents=True)
            source.write_bytes(b'statement')

            moved, deleted = operator.migrate_legacy_lfty_statement_folders(root)

            target = (
                root
                / 'TX/5401 Odom Ave Fort Worth, TX 76114/Public/07 - P&L & Owner Statements/'
                'Bank Statements/2026/BASELANE_BEAR LFTY0142 DAO LLC - 5401 ODOM AVE_'
                '5401 ODOM AVE OPERATIONS_MAY_2026_STATEMENT.pdf'
            )
            self.assertEqual(moved, [target])
            self.assertTrue(target.exists())
            self.assertFalse(source.exists())
            self.assertFalse(source.parent.exists())
            self.assertIn(
                root / 'TX/LFTY0142 5401 Odom Ave Fort Worth, TX 76114',
                deleted,
            )

    def test_migrate_legacy_lfty_statement_folders_keeps_non_empty_legacy_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy_root = root / 'TX/LFTY0142 5401 Odom Ave Fort Worth, TX 76114'
            source = (
                legacy_root
                / 'Public/07 - P&L & Owner Statements/Bank Statements/2026/'
                'BASELANE_BEAR LFTY0142 DAO LLC - 5401 ODOM AVE_5401 ODOM AVE OPERATIONS_MAY_2026_STATEMENT.pdf'
            )
            source.parent.mkdir(parents=True)
            source.write_bytes(b'statement')
            keep = legacy_root / 'Public/03 - LLC Documents/Operating Agreement.pdf'
            keep.parent.mkdir(parents=True)
            keep.write_bytes(b'keep')

            operator.migrate_legacy_lfty_statement_folders(root)

            self.assertTrue(legacy_root.exists())
            self.assertTrue(keep.exists())
            self.assertFalse(source.parent.exists())


if __name__ == '__main__':
    unittest.main()
