#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Contract tests for the Debian direct-boot QEMU gate."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from tools.riscv.debian import direct_boot_gate as gate


class QemuArgumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.kernel = Path("/tmp/kernel.Image")
        self.rootfs = Path("/tmp/rootfs.ext2")

    def build(self, **overrides: object) -> list[str]:
        values = {
            "qemu": "qemu-system-riscv64",
            "kernel": self.kernel,
            "rootfs": self.rootfs,
            "dsh_image": None,
            "memory": "2G",
            "smp": 4,
        }
        values.update(overrides)
        return gate.build_qemu_argv(**values)

    def test_default_command_uses_snapshot_and_direct_boot(self) -> None:
        argv = self.build()

        self.assertEqual(argv[0], "qemu-system-riscv64")
        append = argv[argv.index("-append") + 1]
        self.assertIn("root=/dev/vda rw", append)
        self.assertIn("console=ttyS0", append)
        self.assertIn("init=/bin/bash", append)
        self.assertNotIn("rootfstype=", append)
        self.assertIn(
            f"file={self.rootfs},snapshot=on",
            argv[argv.index("-drive") + 1],
        )
        self.assertEqual(argv[-2:], ["-device", "virtio-blk-device,drive=root"])

    def test_rootfstype_and_extra_kernel_arguments_are_appended(self) -> None:
        argv = self.build(
            rootfstype="ext4",
            extra_kernel_args="asterinas.ext4_fault_stage=2",
            snapshot=False,
        )

        append = argv[argv.index("-append") + 1]
        self.assertIn("rootfstype=ext4", append)
        self.assertIn("asterinas.ext4_fault_stage=2", append)
        self.assertIn(
            f"file={self.rootfs},snapshot=off",
            argv[argv.index("-drive") + 1],
        )

    def test_dsh_disk_precedes_the_root_device(self) -> None:
        dsh = Path("/tmp/dsh.ext2")
        argv = self.build(dsh_image=dsh)

        self.assertIn(f"id=dsh,file={dsh},snapshot=on", " ".join(argv))
        self.assertEqual(
            argv[-4:-2],
            ["-device", "virtio-blk-device,drive=dsh"],
        )
        self.assertEqual(argv[-2:], ["-device", "virtio-blk-device,drive=root"])


class GuestCommandTests(unittest.TestCase):
    def test_commands_export_path_and_finish_with_gate_end(self) -> None:
        commands = gate.guest_commands(dsh_image=None)

        self.assertTrue(
            any(command.startswith("export PATH=") for command in commands)
        )
        self.assertEqual(commands[0], "mount -t proc proc /proc 2>/dev/null || true")
        self.assertIn("echo GATE_BEGIN", commands)
        self.assertEqual(commands[-1], "echo GATE_END")
        self.assertFalse(any("dsh" in command.lower() for command in commands))

    def test_dsh_commands_require_the_second_disk(self) -> None:
        commands = gate.guest_commands(dsh_image=Path("/tmp/dsh.ext2"))
        text = "\n".join(commands)

        self.assertIn("mount -t ext2 /dev/vdb /mnt/dsh", text)
        self.assertIn("GATE_DSH_BIN", text)
        self.assertIn("GATE_DSH_HELP", text)
        self.assertEqual(commands[-1], "echo GATE_END")


class ValueValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.valid = {
            "KERNEL": "Linux 6.16.0 riscv64",
            "GCC": "14.2.0",
            "CC_RC": "42",
            "PYTHON": "Python 3.13.5",
            "NODE": "v24.21.0",
            "GIT": "git version 2.47.3",
        }

    def test_matching_toolchain_is_accepted(self) -> None:
        gate.check_values(self.valid, dsh_image=None)

    def test_each_required_value_is_checked(self) -> None:
        for key in self.valid:
            values = dict(self.valid)
            values[key] = "wrong"
            with self.subTest(key=key), self.assertRaises(gate.GateError):
                gate.check_values(values, dsh_image=None)

    def test_dsh_values_are_required_only_when_the_image_is_attached(self) -> None:
        dsh = Path("/tmp/dsh.ext2")
        with self.assertRaises(gate.GateError):
            gate.check_values(self.valid, dsh_image=dsh)

        values = dict(self.valid)
        values["DSH_BIN"] = "yes"
        values["DSH_HELP"] = "Usage: dsh [options]"
        gate.check_values(values, dsh_image=dsh)


class ArgumentTests(unittest.TestCase):
    def test_defaults_target_direct_boot(self) -> None:
        args = gate.parse_args(["--kernel", "k", "--rootfs", "r"])

        self.assertIsNone(args.rootfstype)
        self.assertEqual(args.memory, "2G")
        self.assertEqual(args.smp, 4)
        self.assertEqual(args.timeout, 120.0)

    def test_unknown_rootfstype_is_rejected(self) -> None:
        with (
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            gate.parse_args(
                ["--kernel", "k", "--rootfs", "r", "--rootfstype", "ext3"]
            )


if __name__ == "__main__":
    unittest.main()
