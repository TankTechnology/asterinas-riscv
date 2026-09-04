#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from ltp_suite import main, suite_by_name, suite_names


REPO = Path(__file__).resolve().parents[3]


class LtpSuiteTests(unittest.TestCase):
    def test_named_suites_have_closed_count_contracts(self) -> None:
        self.assertEqual(suite_names(), ("syscalls", "arch-riscv64"))

        syscalls = suite_by_name(REPO, "syscalls")
        self.assertEqual(syscalls.expected_selected, 767)
        self.assertEqual(syscalls.expected_unavailable, 12)

        arch = suite_by_name(REPO, "arch-riscv64")
        self.assertEqual(arch.expected_selected, 154)
        self.assertEqual(arch.expected_unavailable, 0)
        self.assertEqual(
            arch.enabled,
            REPO / "tools/riscv/ltp/manifests/arch-riscv64.txt",
        )

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
                "154",
                "0",
            ],
        )


if __name__ == "__main__":
    unittest.main()
