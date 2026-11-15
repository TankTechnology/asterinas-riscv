#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the bounded host-test runner."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.riscv import host_test_runner as runner


class DiscoveryTests(unittest.TestCase):
    def test_discovers_sorted_test_modules_and_ignores_bytecode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tests = root / "tools/riscv/tests"
            tests.mkdir(parents=True)
            (tests / "test_b.py").write_text("")
            (tests / "test_a.py").write_text("")
            subpackage = tests / "sub"
            subpackage.mkdir()
            (subpackage / "test_c.py").write_text("")
            (tests / "__pycache__").mkdir()
            (tests / "__pycache__/test_ignored.py").write_text("")

            modules = runner.discover_modules(tests, root=root)

        self.assertEqual(
            modules,
            [
                "tools.riscv.tests.sub.test_c",
                "tools.riscv.tests.test_a",
                "tools.riscv.tests.test_b",
            ],
        )

    def test_shards_are_deterministic_and_non_overlapping(self) -> None:
        modules = [f"m{index}" for index in range(10)]

        shards = [
            runner.shard_modules(modules, index, 3) for index in range(3)
        ]

        self.assertEqual(
            sorted(item for shard in shards for item in shard), sorted(modules)
        )
        self.assertEqual(sum(len(shard) for shard in shards), len(modules))
        self.assertEqual(shards[0], ["m0", "m3", "m6", "m9"])

    def test_invalid_shard_arguments_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            runner.shard_modules(["m"], 0, 0)
        with self.assertRaises(ValueError):
            runner.shard_modules(["m"], 1, 1)


class ResultTests(unittest.TestCase):
    def test_module_failure_covers_failure_error_timeout_and_import_error(self) -> None:
        self.assertTrue(runner._module_failed({"failures": [{"test": "x"}]}))
        self.assertTrue(runner._module_failed({"errors": [{"test": "x"}]}))
        self.assertTrue(runner._module_failed({"timed_out": True}))
        self.assertTrue(runner._module_failed({"error": "ImportError"}))
        self.assertTrue(runner._module_failed({"returncode": 1}))
        self.assertFalse(runner._module_failed({"returncode": 0, "skipped": []}))

    def test_duplicate_ids_are_reported_once(self) -> None:
        self.assertEqual(
            runner.duplicate_ids(["b", "a", "b", "a", "c"]), ["a", "b"]
        )
        self.assertEqual(runner.duplicate_ids(["a", "b"]), [])

    def test_baseline_rejects_missing_ids_and_unexplained_skips(self) -> None:
        expected = {
            "tests": 2,
            "test_ids": ["a", "b"],
            "skip_reasons": ["known"],
        }
        self.assertEqual(
            runner.baseline_disagreements(expected, {"a", "b"}, ["known"]), []
        )

        problems = runner.baseline_disagreements(expected, {"a"}, ["new"])
        self.assertEqual(len(problems), 3)
        self.assertTrue(any("missing" in problem for problem in problems))
        self.assertTrue(any("below baseline" in problem for problem in problems))
        self.assertTrue(any("unexpected skip" in problem for problem in problems))

    def test_import_error_is_reported_by_the_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = runner.run_module(
                "tools.riscv.tests.missing_runner_fixture",
                timeout=30.0,
                log_dir=Path(temporary),
            )

        self.assertTrue(result["error"])
        self.assertTrue(runner._module_failed(result))

    def test_timeout_is_reported_by_the_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = runner.run_module(
                "tools.riscv.tests.test_host_test_runner",
                timeout=0.0001,
                log_dir=Path(temporary),
            )

        self.assertTrue(result["timed_out"])
        self.assertTrue(runner._module_failed(result))

    def test_iter_tests_flattens_nested_suites(self) -> None:
        class Example(unittest.TestCase):
            def test_one(self) -> None:
                pass

            def test_two(self) -> None:
                pass

        suite = unittest.TestSuite(
            [Example("test_one"), unittest.TestSuite([Example("test_two")])]
        )

        ids = [runner.test_id(test) for test in runner.iter_tests(suite)]
        self.assertEqual(len(ids), 2)
        self.assertTrue(ids[0].endswith(".Example.test_one"))
        self.assertTrue(ids[1].endswith(".Example.test_two"))
