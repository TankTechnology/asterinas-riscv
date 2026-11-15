#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from tools.riscv.ltp.ltp_suite import main, suite_by_name, suite_names


REPO = Path(__file__).resolve().parents[3]


class LtpSuiteTests(unittest.TestCase):
    def test_named_suites_have_closed_count_contracts(self) -> None:
        self.assertEqual(
            suite_names(), ("syscalls", "arch-riscv64", "fs", "mm", "sched")
        )

        syscalls = suite_by_name(REPO, "syscalls")
        self.assertEqual(syscalls.expected_selected, 768)
        self.assertEqual(syscalls.expected_unavailable, 12)

        arch = suite_by_name(REPO, "arch-riscv64")
        self.assertEqual(arch.expected_selected, 158)
        self.assertEqual(arch.expected_unavailable, 0)
        self.assertEqual(
            arch.enabled,
            REPO / "tools/riscv/ltp/manifests/arch-riscv64.txt",
        )

        fs = suite_by_name(REPO, "fs")
        self.assertEqual(fs.expected_selected, 20)
        self.assertEqual(fs.expected_unavailable, 0)
        self.assertEqual(fs.runtest, "runtest/fs")

        mm = suite_by_name(REPO, "mm")
        self.assertEqual(mm.expected_selected, 19)
        self.assertEqual(mm.expected_unavailable, 2)
        self.assertEqual(mm.runtest, "runtest/mm")

        sched = suite_by_name(REPO, "sched")
        self.assertEqual(sched.expected_selected, 8)
        self.assertEqual(sched.expected_unavailable, 0)
        self.assertEqual(sched.runtest, "runtest/sched")

        with self.assertRaisesRegex(ValueError, "unknown LTP suite"):
            suite_by_name(REPO, "arbitrary")

    def test_describe_cli_emits_shell_line_fields_from_the_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                status = main(
                    [
                        "describe",
                        "--repo",
                        str(repo),
                        "--suite",
                        "arch-riscv64",
                    ]
                )

        self.assertEqual(status, 0)
        self.assertEqual(
            output.getvalue().splitlines(),
            [
                str(repo / "tools/riscv/ltp/manifests/arch-riscv64.txt"),
                "158",
                "0",
                "runtest/syscalls",
                "testcases/kernel/syscalls",
            ],
        )


if __name__ == "__main__":
    unittest.main()
