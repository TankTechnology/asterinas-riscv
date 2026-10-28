#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Guard the checked-in host-test baseline manifest."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from tools.riscv import host_test_runner


BASELINE = Path(__file__).with_name("expected_host_tests.json")

class HostTestBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.value = json.loads(BASELINE.read_text(encoding="utf-8"))

    def test_manifest_is_complete_and_internally_consistent(self) -> None:
        self.assertEqual(self.value["schema_version"], 1)
        self.assertIsInstance(self.value["commit"], str)
        self.assertEqual(self.value["modules"], len(self.value["module_names"]))
        self.assertEqual(self.value["tests"], len(self.value["test_ids"]))
        self.assertTrue(self.value["module_names"])
        self.assertTrue(self.value["test_ids"])
        self.assertTrue(self.value["skip_reasons"])

    def test_ids_are_canonical_and_unique(self) -> None:
        test_ids = self.value["test_ids"]
        self.assertEqual(len(test_ids), len(set(test_ids)))
        for test_id in test_ids:
            self.assertTrue(
                test_id.startswith(host_test_runner.MODULE_PREFIX), test_id
            )

    def test_baseline_modules_are_still_discovered(self) -> None:
        discovered = set(host_test_runner.discover_modules())
        self.assertLessEqual(set(self.value["module_names"]), discovered)

    def test_skip_reasons_are_explicit(self) -> None:
        for reason in self.value["skip_reasons"]:
            self.assertIsInstance(reason, str)
            self.assertTrue(reason.strip())
