#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Contract tests for the ext4/JBD2 QEMU gate."""

from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from tools.riscv.debian import ext4_gate as gate


class MarkerContractTests(unittest.TestCase):
    def test_every_semantics_marker_is_emitted_by_a_command(self) -> None:
        script = "\n".join(gate.SEMANTICS_COMMANDS)

        for marker in gate.SEMANTICS_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker, script)

    def test_crash_workload_creates_the_expected_directory_size(self) -> None:
        script = "\n".join(gate.CRASH_COMMANDS)

        self.assertIn("$n -lt 32", script)
        self.assertIn("GATE_EXT4_MANY_CREATED", script)
        self.assertIn("GATE_EXT4_SYNC_DONE", script)
        self.assertIn("GATE_EXT4_MANY=$count", gate.RECOVERY_MANY)

    def test_gate_markers_are_distinct(self) -> None:
        markers = {
            gate.GATE_BEGIN,
            gate.SEMANTICS_END,
            gate.SYNC_DONE,
            gate.RECOVERY_END,
        }
        self.assertEqual(len(markers), 4)


class ImageCopyTests(unittest.TestCase):
    def test_sparse_copy_preserves_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.ext4"
            destination = root / "destination.ext4"
            source.write_bytes(b"ext4-gate" * 1024)

            gate.copy_sparse(source, destination)

            self.assertEqual(destination.read_bytes(), source.read_bytes())


class E2fsckTests(unittest.TestCase):
    def test_failure_is_reported_and_keeps_the_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "e2fsck.txt"
            failed = subprocess.CompletedProcess(
                ["e2fsck"], 1, stdout="inode table mismatch\n"
            )
            with (
                mock.patch.object(gate.subprocess, "run", return_value=failed),
                self.assertRaises(gate.Ext4GateError),
            ):
                gate.run_e2fsck(Path("image.ext4"), log)

            self.assertEqual(log.read_text(), "inode table mismatch\n")

    def test_success_returns_quietly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "e2fsck.txt"
            passed = subprocess.CompletedProcess(["e2fsck"], 0, stdout="clean\n")
            with mock.patch.object(gate.subprocess, "run", return_value=passed):
                gate.run_e2fsck(Path("image.ext4"), log)

            self.assertEqual(log.read_text(), "clean\n")


class ArgumentTests(unittest.TestCase):
    def test_defaults_cover_all_crash_stages(self) -> None:
        args = gate.parse_args(["--kernel", "k", "--rootfs", "r"])

        self.assertEqual(args.fault_stages, "1,2,3,4")
        self.assertEqual(args.memory, "2G")
        self.assertEqual(args.smp, 4)
        self.assertEqual(args.timeout, 300.0)
        self.assertFalse(args.keep_images)

    def test_empty_fault_stages_selects_write_only(self) -> None:
        args = gate.parse_args(
            ["--kernel", "k", "--rootfs", "r", "--fault-stages", ""]
        )

        self.assertEqual(args.fault_stages, "")

    def test_required_arguments_are_enforced(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            gate.parse_args([])


if __name__ == "__main__":
    unittest.main()
