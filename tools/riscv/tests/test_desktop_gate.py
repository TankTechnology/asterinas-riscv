#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the compact direct-boot Debian desktop gate."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from tools.riscv.debian import desktop_gate


class QemuArgumentTests(unittest.TestCase):
    def test_direct_boot_loads_stage1_and_dtb_with_a_private_qmp_socket(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            argv = desktop_gate.build_qemu_argv(
                qemu="qemu-system-riscv64",
                kernel=root / "kernel.Image",
                rootfs=root / "root.ext2",
                stage1=root / "stage1.cpio",
                dtb=root / "fb.dtb",
                log_dir=root,
                memory="2G",
                smp=4,
            )

        joined = " ".join(argv)
        self.assertIn("-initrd", argv)
        self.assertIn("-dtb", argv)
        self.assertIn(
            "bochs-display,id=desktop-display,xres=1280,yres=1024",
            joined,
        )
        self.assertIn("-vga none", joined)
        self.assertIn("init=/init", argv[argv.index("-append") + 1])
        self.assertIn("--root-init=systemd", argv[argv.index("-append") + 1])
        self.assertIn("snapshot=on", argv[argv.index("-drive") + 1])

    def test_markers_are_ordered_and_unique(self) -> None:
        markers = desktop_gate.DESKTOP_MARKERS

        self.assertEqual(len(markers), len(set(markers)))
        self.assertTrue(markers[0].endswith("display-provider-ready"))
        self.assertTrue(markers[-1].endswith("panel-start"))


class LogDirectoryTests(unittest.TestCase):
    def test_private_directory_is_accepted_and_public_one_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "private"
            root.mkdir(mode=0o700)
            desktop_gate._prepare_log_dir(root)

            root.chmod(0o755)
            with self.assertRaisesRegex(desktop_gate.DesktopGateError, "0700"):
                desktop_gate._prepare_log_dir(root)


class ArgumentTests(unittest.TestCase):
    def test_defaults_are_bounded_to_the_contract(self) -> None:
        args = desktop_gate.parse_args(
            [
                "--kernel",
                "k",
                "--rootfs",
                "r",
                "--stage1",
                "s",
                "--dtb",
                "d",
            ]
        )

        self.assertEqual(args.memory, "2G")
        self.assertEqual(args.smp, 4)
        self.assertEqual(args.timeout, 900.0)

    def test_required_inputs_and_four_harts_are_enforced(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            desktop_gate.parse_args(["--kernel", "k"])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            desktop_gate.parse_args(
                [
                    "--kernel",
                    "k",
                    "--rootfs",
                    "r",
                    "--stage1",
                    "s",
                    "--dtb",
                    "d",
                    "--smp",
                    "2",
                ]
            )


if __name__ == "__main__":
    unittest.main()
