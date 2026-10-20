# SPDX-License-Identifier: MPL-2.0

import unittest

from tools.riscv import megrez_menu_board


class RecoveryEpochTests(unittest.TestCase):
    def test_initial_firmware_prefix_is_not_recovery_evidence(self):
        initial = "OpenSBI v1.5\nU-Boot 2024.10\n=> "
        checkpoint = len(initial)
        self.assertFalse(
            megrez_menu_board._ordered_recovery_epoch(initial[checkpoint:])
        )

    def test_recovery_requires_a_complete_ordered_epoch(self):
        self.assertFalse(
            megrez_menu_board._ordered_recovery_epoch("OpenSBI v1.5\nU-Boot 2024.10\n")
        )
        self.assertFalse(
            megrez_menu_board._ordered_recovery_epoch("U-Boot 2024.10\n=> ")
        )
        self.assertTrue(
            megrez_menu_board._ordered_recovery_epoch(
                "OpenSBI v1.5\nU-Boot 2024.10\n=> "
            )
        )

    def test_recovery_boundary_accepts_automatic_rockos_boot(self):
        transcript = "OpenSBI v1.5\nU-Boot 2024.10\nDebian GNU/Linux rockos-eswin login: "
        self.assertEqual(
            megrez_menu_board._ordered_recovery_boundary(transcript),
            "rockos-login",
        )


if __name__ == "__main__":
    unittest.main()
