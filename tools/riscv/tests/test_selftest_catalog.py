#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""The subsystem catalog is the contract the selftest runner consumes."""

from __future__ import annotations

import unittest

from tools.riscv.selftests import catalog


class SelftestCatalogTests(unittest.TestCase):
    def test_subsystems_are_unique_and_have_cases(self) -> None:
        names = [subsystem.name for subsystem in catalog.SUBSYSTEMS]
        self.assertEqual(len(names), len(set(names)))
        for subsystem in catalog.SUBSYSTEMS:
            with self.subTest(subsystem=subsystem.name):
                self.assertTrue(subsystem.cases)
                case_names = [case.name for case in subsystem.cases]
                self.assertEqual(len(case_names), len(set(case_names)))

    def test_catalog_order_puts_boot_before_graphical(self) -> None:
        targets = [subsystem.target for subsystem in catalog.SUBSYSTEMS]
        self.assertEqual(targets, sorted(targets, key=lambda t: t != catalog.MULTI_USER))

    def test_select_preserves_catalog_order(self) -> None:
        selected = catalog.select(["drm", "console"])
        self.assertEqual([s.name for s in selected], ["console", "drm"])

    def test_select_rejects_unknown_subsystem(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown subsystem"):
            catalog.select(["console", "no-such-subsystem"])

    def test_boot_target_follows_the_selection(self) -> None:
        self.assertEqual(
            catalog.boot_target(catalog.select(["console", "net"])),
            catalog.MULTI_USER,
        )
        self.assertEqual(
            catalog.boot_target(catalog.select(["console", "browser"])),
            catalog.GRAPHICAL,
        )


if __name__ == "__main__":
    unittest.main()
