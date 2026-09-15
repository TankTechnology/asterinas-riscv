#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Contract tests for the two-host diskless RISC-V probe."""

from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
import tempfile
import unittest


class DualHostQemuCommandTests(unittest.TestCase):
    def probe_module(self):
        try:
            return importlib.import_module("tools.riscv.dual_host_qemu_probe")
        except ModuleNotFoundError as error:
            self.fail(f"dual-host QEMU probe module is missing: {error}")

    def test_regular_artifact_identity_is_exact(self) -> None:
        probe = self.probe_module()
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "kernel.Image"
            artifact.write_bytes(b"asterinas-kernel")
            identity = probe.read_identity(artifact)
            self.assertEqual(identity.size, len(b"asterinas-kernel"))
            self.assertEqual(
                identity.sha256, hashlib.sha256(b"asterinas-kernel").hexdigest()
            )

    def test_symlink_is_not_an_artifact(self) -> None:
        probe = self.probe_module()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "kernel.Image"
            source.write_bytes(b"kernel")
            link = Path(directory) / "kernel-link.Image"
            link.symlink_to(source)
            with self.assertRaises(OSError):
                probe.read_identity(link)

    def test_probe_bootargs_exclude_disk_and_network_workload(self) -> None:
        probe = self.probe_module()
        bootargs = probe.qemu_bootargs()
        self.assertIn("--root-init=probe", bootargs)
        self.assertIn("asterinas.reboot_after=180", bootargs)
        self.assertNotIn("mmc_write_partition2", bootargs)
        self.assertNotIn("asterinas.net=", bootargs)

    def test_developer_command_is_bounded_and_device_free(self) -> None:
        probe = self.probe_module()
        argv = probe.developer_argv(
            "asterinas-dev-v1-test", Path("/root/asterinas/kernel.Image"),
            Path("/root/asterinas/initramfs.cpio"), 210,
        )
        self.assertEqual(argv[:3], ("docker", "exec", "-it"))
        self.assertIn("timeout", argv)
        self.assertIn("-nic", argv)
        self.assertIn("none", argv)
        self.assertIn("-no-reboot", argv)
        self.assertIn("-accel", argv)
        self.assertIn("tcg", argv)

    def test_rockos_command_is_bounded_and_device_free(self) -> None:
        probe = self.probe_module()
        argv = probe.rockos_argv(
            "debian@10.100.19.200", Path("/tmp/asterinas-qemu-probe/a/kernel.Image"),
            Path("/tmp/asterinas-qemu-probe/a/initramfs.cpio"), 210,
        )
        self.assertEqual(argv[:2], ("ssh", "-tt"))
        command = argv[-1]
        self.assertIn("timeout -k 5 210", command)
        self.assertIn("nice -n 10", command)
        self.assertIn("-nic none", command)
        self.assertIn("-no-reboot", command)
        self.assertIn("-accel tcg", command)


if __name__ == "__main__":
    unittest.main()
