#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""The subsystem catalog is the contract the selftest runner consumes."""

from __future__ import annotations

import unittest

from tools.riscv.selftests import catalog, runner


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


class SelftestRunnerTests(unittest.TestCase):
    def test_parse_results_reads_pass_fail_and_skip(self) -> None:
        text = (
            "noise\n"
            "SELFTEST console identity PASS host=board os=Debian\n"
            "SELFTEST net http SKIP no SELFTEST_HTTP_URL\n"
            "SELFTEST block space FAIL avail_kb=1\n"
            "SELFTEST_DONE\n"
        )
        self.assertEqual(
            runner.parse_results(text),
            [
                {"subsystem": "console", "case": "identity", "status": "PASS", "detail": "host=board os=Debian"},
                {"subsystem": "net", "case": "http", "status": "SKIP", "detail": "no SELFTEST_HTTP_URL"},
                {"subsystem": "block", "case": "space", "status": "FAIL", "detail": "avail_kb=1"},
            ],
        )

    def test_plan_uses_one_boot_target_for_the_selection(self) -> None:
        plan = runner.build_plan(["console", "browser"], "browser-web")
        self.assertEqual(plan["target"], catalog.GRAPHICAL)
        self.assertEqual(
            [item["name"] for item in plan["subsystems"]],
            ["console", "browser"],
        )

    def test_missing_results_are_reported_per_case(self) -> None:
        plan = runner.build_plan(["console", "display"], "browser-web")
        results = runner.parse_results(
            "SELFTEST console identity PASS host=board os=Debian\n"
            "SELFTEST_DONE\n"
        )
        missing = runner.missing_cases(plan, results)
        self.assertIn(("console", "clock"), missing)
        self.assertIn(("display", "xorg"), missing)
        self.assertNotIn(("console", "identity"), missing)

    def test_a_complete_plan_has_no_missing_cases(self) -> None:
        plan = runner.build_plan(["power"], "browser-web")
        results = runner.parse_results(
            "SELFTEST power reboot SKIP terminal case\n"
            "SELFTEST power poweroff SKIP terminal case\n"
            "SELFTEST_DONE\n"
        )
        self.assertEqual(runner.missing_cases(plan, results), [])


if __name__ == "__main__":
    unittest.main()
