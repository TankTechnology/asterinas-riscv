#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the Debian rootfs and Stage1 builder entry points."""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

from tools.riscv.debian.rootfs import profiles

REPO_ROOT = Path(__file__).resolve().parents[3]
ROOTFS_BUILDER = REPO_ROOT / "tools/riscv/debian/rootfs/build_rootfs.sh"
STAGE1_BUILDER = REPO_ROOT / "tools/riscv/debian/rootfs/build_stage1.sh"


def run_script(
    script: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *arguments],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


class RootfsBuilderTests(unittest.TestCase):
    def test_required_tool_list_is_frozen(self) -> None:
        result = run_script(ROOTFS_BUILDER, "--print-tools")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "debootstrap",
                "qemu-riscv64-static",
                "gpgv",
                "dpkg-query",
                "mke2fs",
                "dumpe2fs",
                "debugfs",
                "sha256sum",
                "curl",
            ],
        )

    def test_print_packages_matches_the_profile_registry(self) -> None:
        for name in profiles.PROFILE_NAMES:
            with self.subTest(profile=name):
                result = run_script(
                    ROOTFS_BUILDER,
                    "--profile",
                    name,
                    "--print-packages",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    tuple(result.stdout.splitlines()),
                    profiles.get_profile(name).requested_packages,
                )

    def test_unknown_profile_is_rejected(self) -> None:
        result = run_script(ROOTFS_BUILDER, "--profile", "missing", "--print-packages")

        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown rootfs profile", result.stderr)

    def test_print_modes_reject_build_options(self) -> None:
        result = run_script(
            ROOTFS_BUILDER,
            "--print-tools",
            "--output-dir",
            "target/ignored",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("does not accept build options", result.stderr)

    def test_unknown_argument_is_rejected(self) -> None:
        result = run_script(ROOTFS_BUILDER, "--not-an-option")

        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown argument", result.stderr)

    def test_script_has_valid_bash_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(ROOTFS_BUILDER)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class Stage1BuilderTests(unittest.TestCase):
    def test_required_tools_are_reported(self) -> None:
        result = run_script(STAGE1_BUILDER, "--print-tools")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            ["riscv64-linux-gnu-gcc", "cpio", "python3"],
        )

    def test_archive_entries_are_reported(self) -> None:
        result = run_script(STAGE1_BUILDER, "--print-entries")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                ".",
                "init",
                "usr",
                "usr/lib",
                "usr/lib/asterinas",
                "usr/lib/asterinas/browser-web-marionette-gate",
                "usr/lib/asterinas/browser_web_marionette_gate.py",
                "usr/lib/asterinas/browser_marionette_transport.py",
            ],
        )

    def test_help_succeeds_and_unknown_options_fail(self) -> None:
        help_result = run_script(STAGE1_BUILDER, "--help")
        unknown_result = run_script(STAGE1_BUILDER, "--unknown")

        self.assertEqual(help_result.returncode, 0)
        self.assertIn("build_stage1.sh", help_result.stdout)
        self.assertEqual(unknown_result.returncode, 2)
        self.assertIn("unknown option", unknown_result.stderr)

    def test_source_date_epoch_must_fit_newc(self) -> None:
        environment = os.environ.copy()
        environment["SOURCE_DATE_EPOCH"] = "4294967296"
        result = run_script(
            STAGE1_BUILDER,
            "target/ignored.cpio",
            environment=environment,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("canonical decimal newc u32", result.stderr)

    def test_script_has_valid_bash_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(STAGE1_BUILDER)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
