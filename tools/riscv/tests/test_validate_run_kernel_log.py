#!/usr/bin/env python3

# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import unittest
from pathlib import Path

from tools.riscv.validate_run_kernel_log import ValidationError, validate_transcript


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class ValidateRunKernelLogTests(unittest.TestCase):
    def test_accepts_complete_smp4_icache_regression(self) -> None:
        validate_transcript(
            "\n".join(
                (
                    "boot output",
                    "riscv_flush_icache cross-hart passed: "
                    "cpus=4 local=0 remotes=1,2,3 generations=1024",
                    "All regression tests passed.",
                )
            ),
            mode="regression",
            require_riscv_icache_smp4=True,
        )

    def test_rejects_skip_duplicate_cpus_or_wrong_topology(self) -> None:
        cases = (
            "riscv_flush_icache cross-hart skipped: fewer than two CPUs",
            "riscv_flush_icache cross-hart passed: "
            "cpus=4 local=0 remotes=1,1,3 generations=1024",
            "riscv_flush_icache cross-hart passed: "
            "cpus=2 local=0 remotes=1 generations=1024",
        )
        for evidence in cases:
            with self.subTest(evidence=evidence), self.assertRaises(ValidationError):
                validate_transcript(
                    f"{evidence}\nAll regression tests passed.\n",
                    mode="regression",
                    require_riscv_icache_smp4=True,
                )

    def test_rejects_missing_or_duplicate_terminal_marker(self) -> None:
        for transcript in (
            "boot output\n",
            "All regression tests passed.\nAll regression tests passed.\n",
        ):
            with self.subTest(transcript=transcript), self.assertRaises(ValidationError):
                validate_transcript(transcript, mode="regression")

    def test_rejects_fatal_before_or_after_success(self) -> None:
        for transcript in (
            "Kernel panic - not syncing\nAll regression tests passed.\n",
            "All regression tests passed.\nSBI remote fence.i to hart 3 failed\n",
        ):
            with self.subTest(transcript=transcript), self.assertRaises(ValidationError):
                validate_transcript(transcript, mode="regression")

    def test_icache_contract_is_regression_only(self) -> None:
        with self.assertRaises(ValidationError):
            validate_transcript(
                "Successfully booted.\n",
                mode="boot",
                require_riscv_icache_smp4=True,
            )

    def test_formal_smp4_contract_is_wired_into_ci_and_guest(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/test_riscv.yml").read_text()
        makefile = (REPOSITORY_ROOT / "Makefile").read_text()
        guest = (
            REPOSITORY_ROOT
            / "test/initramfs/src/regression/process/riscv_flush_icache/"
            "riscv_flush_icache.c"
        ).read_text()
        runner = (
            REPOSITORY_ROOT / "test/initramfs/src/regression/process/run_test.sh"
        ).read_text()
        top_level_runner = (
            REPOSITORY_ROOT
            / "test/initramfs/src/regression/scripts/run_regression_test.sh"
        ).read_text()

        self.assertIn("test_id: 'regression-debug-smp4'", workflow)
        self.assertIn("riscv_icache_require_smp4: '1'", workflow)
        self.assertIn('RISCV_ICACHE_REQUIRE_SMP4 ?= 0', makefile)
        self.assertIn("--require-riscv-icache-smp4", makefile)
        self.assertIn('strcmp(argv[1], "--require-smp4")', guest)
        self.assertIn("cpu_count != 4", guest)
        self.assertIn("remote_index < cpu_count", guest)
        self.assertIn("select_current_cpu_as_local(cpus, cpu_count)", guest)
        self.assertIn("wait_for_cpu(context->cpu)", guest)
        self.assertIn("wait_for_cpu(cpus[0])", guest)
        self.assertIn("RISCV_ICACHE_REQUIRE_SMP4=1", runner)
        self.assertIn("formal SMP4 cross-hart I-cache regression", top_level_runner)
        self.assertIn(
            '"${SCRIPT_DIR}/process/riscv_flush_icache/riscv_flush_icache" --require-smp4',
            top_level_runner,
        )
        self.assertLess(
            top_level_runner.index("RISCV_ICACHE_REQUIRE_SMP4=1"),
            top_level_runner.index("for dir in"),
        )


if __name__ == "__main__":
    unittest.main()
