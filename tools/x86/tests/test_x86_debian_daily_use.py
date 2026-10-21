# SPDX-License-Identifier: MPL-2.0

"""Unit tests for the daily-use gate's argument handling."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DAILY_SCRIPT = REPOSITORY_ROOT / "tools/x86/debian/daily_use_check.sh"


def run_script(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(DAILY_SCRIPT), *arguments],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


class DailyUseArgumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.kernel = self.directory / "kernel.elf"
        self.initramfs = self.directory / "initramfs.cpio"
        self.root_image = self.directory / "debian-root.ext2"
        for path in (self.kernel, self.initramfs, self.root_image):
            path.write_bytes(b"fixture")

    def required_arguments(self) -> list[str]:
        return [
            "--kernel",
            str(self.kernel),
            "--initramfs",
            str(self.initramfs),
            "--base-root",
            str(self.root_image),
            "--workspace",
            str(self.directory / "workspace"),
        ]

    def test_missing_required_argument_is_rejected(self) -> None:
        result = run_script("--kernel", str(self.kernel))

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "--kernel, --initramfs, --base-root and --workspace are required",
            result.stderr,
        )

    def test_unknown_argument_is_rejected(self) -> None:
        result = run_script(*self.required_arguments(), "--unknown")

        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown argument", result.stderr)

    def test_accel_is_validated(self) -> None:
        result = run_script(*self.required_arguments(), "--accel", "hvf")

        self.assertEqual(result.returncode, 2)
        self.assertIn("--accel must be 'tcg' or 'kvm'", result.stderr)

    def test_help_prints_usage(self) -> None:
        result = run_script("--help")

        self.assertEqual(result.returncode, 0)
        self.assertIn("Usage: daily_use_check.sh", result.stdout)


if __name__ == "__main__":
    unittest.main()
