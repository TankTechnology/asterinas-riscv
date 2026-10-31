#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Contract tests for the picked Debian gate framework."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.riscv.debian.rootfs import gate_protocol
from tools.riscv.debian.rootfs import gate_runtime
from tools.riscv.debian.rootfs import rootfs_gate
from tools.riscv.debian.rootfs import systemd_m2_gate


class BootArgsTests(unittest.TestCase):
    def test_systemd_bootargs_have_one_init_and_separator(self) -> None:
        value = gate_protocol.systemd_bootargs()

        self.assertEqual(value, "console=ttyS0 loglevel=4 init=/init -- --root-init=systemd")
        self.assertEqual(value.count("init=/init"), 1)
        self.assertEqual(value.split().count("--"), 1)

    def test_systemd_bootargs_reject_fragment_strings_and_bad_selectors(self) -> None:
        with self.assertRaisesRegex(ValueError, "token sequences"):
            gate_protocol.systemd_bootargs(pre_init="--root-fs=ext4")
        with self.assertRaisesRegex(ValueError, "root-init selector"):
            gate_protocol.systemd_bootargs(init_args=("--root-fs=ext4",))
        with self.assertRaisesRegex(ValueError, "whitespace-free"):
            gate_protocol.systemd_bootargs(pre_init=("bad token",))

    def test_systemd_m2_selects_ext2_or_ext4(self) -> None:
        ext2 = systemd_m2_gate.systemd_m2_bootargs("ext2")
        ext4 = systemd_m2_gate.systemd_m2_bootargs("ext4")

        self.assertIn("--root-init=systemd", ext2)
        self.assertNotIn("--root-fs=", ext2)
        self.assertIn("--root-fs=ext4", ext4)
        with self.assertRaisesRegex(ValueError, "unsupported"):
            systemd_m2_gate.systemd_m2_bootargs("xfs")


class QemuArgumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.uboot = self.root / "u-boot"
        self.boot_disk = self.root / "boot.ext4"
        self.root_disk = self.root / "root.ext2"
        for path in (self.uboot, self.boot_disk, self.root_disk):
            path.write_bytes(b"fixture")

    def argv(self, **overrides: object) -> tuple[str, ...]:
        values = {
            "uboot": self.uboot,
            "boot_disk": self.boot_disk,
            "root_disk": self.root_disk,
            "monitor_socket": self.root / "monitor.sock",
        }
        values.update(overrides)
        return gate_protocol.qemu_argv(**values)

    def test_default_is_an_offline_four_hart_fixed_disk_contract(self) -> None:
        argv = self.argv()

        self.assertEqual(argv[0], "qemu-system-riscv64")
        self.assertEqual(argv[argv.index("-smp") + 1], "4")
        self.assertEqual(argv[argv.index("-nic") + 1], "none")
        self.assertIn("-no-reboot", argv)
        self.assertIn(
            f"file={self.boot_disk},id=bootdisk,readonly=on",
            argv[argv.index("-drive") + 1],
        )
        self.assertIn("cache=directsync", " ".join(argv))

    def test_allow_reboot_and_graphical_devices_are_explicit(self) -> None:
        argv = self.argv(allow_reboot=True, graphical=True)

        self.assertNotIn("-no-reboot", argv)
        joined = " ".join(argv)
        self.assertIn("bochs-display,xres=1280,yres=1024", joined)
        self.assertIn("virtio-keyboard-device", joined)
        self.assertIn("virtio-tablet-device", joined)

    def test_invalid_inputs_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly 4"):
            self.argv(smp=2)
        with self.assertRaisesRegex(ValueError, "unsupported QEMU graphical device"):
            self.argv(graphical=True, graphics_device="cirrus")
        with self.assertRaisesRegex(ValueError, "must not already exist"):
            (self.root / "monitor.sock").write_text("occupied")
            self.argv()
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.argv(uboot=Path("/missing"))


class ParseArgsTests(unittest.TestCase):
    def test_defaults_match_the_gate_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [
                root / name
                for name in (
                    "kernel",
                    "uboot",
                    "dtb",
                    "initramfs",
                    "root.ext2",
                    "manifest",
                    "packages.lock",
                    "checksums",
                    "output",
                )
            ]
            arguments = []
            for option, path in zip(
                (
                    "--kernel",
                    "--uboot",
                    "--dtb",
                    "--stage1-initramfs",
                    "--root-image",
                    "--root-manifest",
                    "--packages-lock",
                    "--package-checksums",
                    "--output-directory",
                ),
                paths,
                strict=True,
            ):
                arguments.extend([option, str(path)])

            config = rootfs_gate.parse_gate_args(arguments)

        self.assertEqual(config.smp, 4)
        self.assertEqual(config.graphics_device, "bochs-display")
        self.assertEqual(config.display, "none")
        self.assertEqual(config.root_fs, "ext2")
        self.assertEqual(config.boot_timeout, 120.0)

    def test_non_four_hart_configuration_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            rootfs_gate.parse_gate_args(
                [
                    "--kernel",
                    "k",
                    "--uboot",
                    "u",
                    "--dtb",
                    "d",
                    "--stage1-initramfs",
                    "i",
                    "--root-image",
                    "r",
                    "--root-manifest",
                    "m",
                    "--packages-lock",
                    "l",
                    "--package-checksums",
                    "c",
                    "--output-directory",
                    "o",
                    "--smp",
                    "2",
                ]
            )


class PinnedDirectoryTests(unittest.TestCase):
    def test_exclusive_lock_and_invalidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            stale = root / "stale.log"
            stale.write_text("old")

            with gate_runtime.PinnedOutputDirectory(root) as first:
                first.lock_exclusive()
                first.invalidate("stale.log")
                self.assertFalse(stale.exists())
                with gate_runtime.PinnedOutputDirectory(root) as second:
                    with self.assertRaisesRegex(RuntimeError, "already active"):
                        second.lock_exclusive()


if __name__ == "__main__":
    unittest.main()
