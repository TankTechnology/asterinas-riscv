"""The console's SysRq keys are a fixed, tiny set.

A break arms the kernel's SysRq window; the key that follows decides whether
the board reboots, powers off, or does nothing. The tool side must not invent
keys: anything outside {b, o, h} has to be refused before a break is sent, so
a typo cannot reboot the board.
"""

from __future__ import annotations

import unittest

from tools.riscv.board.megrez_console import sysrq_command


class SysRqKeyTest(unittest.TestCase):
    def test_the_three_kernel_keys_pass(self) -> None:
        for key in ("b", "o", "h"):
            self.assertEqual(sysrq_command(key), key)

    def test_anything_else_is_refused(self) -> None:
        for key in ("x", "", "bb", "B", "!", "\x04", "restart"):
            self.assertIsNone(sysrq_command(key), repr(key))

    def test_surrounding_whitespace_is_trimmed(self) -> None:
        self.assertEqual(sysrq_command(" b\n"), "b")


if __name__ == "__main__":
    unittest.main()
