"""Unit tests for megrez_board_session milestone detection and gating."""

import unittest

from megrez_board_session import GATE_PATTERN, MILESTONES


class MilestoneDetectionTests(unittest.TestCase):
    def test_all_milestones_match_their_markers(self):
        samples = {
            "kernel_enter": "U-Boot 2024.01-gdbb5f9e3 ... Starting kernel ...\nEnter riscv_boot\n",
            "banner": "    .:-. Presented by the Asterinas developers\n",
            "userspace": ">>> Hello from RISC-V userspace on Asterinas! <<<\n",
        }
        for name, text in samples.items():
            self.assertIn(MILESTONES[name], text, name)

    def test_unrelated_text_does_not_match(self):
        for marker in MILESTONES.values():
            self.assertNotIn(marker, "random u-boot noise line\n")

    def test_uboot_gate_pattern(self):
        match = GATE_PATTERN.search("U-Boot 2024.01-gdbb5f9e3 (Mar 2024)")
        self.assertEqual(match.group(1), "2024.01-gdbb5f9e3")


if __name__ == "__main__":
    unittest.main()
