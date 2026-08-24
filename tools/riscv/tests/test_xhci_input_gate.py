#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import os
import select
import signal
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


def parse_newc_entries(archive: bytes) -> list[tuple[str, int, int, int, int]]:
    entries = []
    offset = 0
    while True:
        header = archive[offset : offset + 110]
        if header[:6] != b"070701":
            raise ValueError("invalid newc header")
        fields = tuple(
            int(header[field_offset : field_offset + 8], 16)
            for field_offset in range(6, 110, 8)
        )
        mode, uid, gid = fields[1:4]
        mtime = fields[5]
        file_size = fields[6]
        name_size = fields[11]
        name_start = offset + 110
        name_end = name_start + name_size
        name = archive[name_start : name_end - 1].decode()
        data_start = (name_end + 3) & ~3
        offset = (data_start + file_size + 3) & ~3
        if name == "TRAILER!!!":
            return entries
        entries.append((name, mode, uid, gid, mtime))


class XhciGuestProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).parents[3]
        cls.source = cls.root / "tools/riscv/xhci/input_gate_init.c"
        cls.builder = cls.root / "tools/riscv/xhci/build_input_gate.sh"

    def compile_probe(self, output: Path, define: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "cc",
                "-std=c11",
                "-O2",
                "-static",
                "-Wall",
                "-Wextra",
                "-Werror",
                f"-D{define}",
                self.source,
                "-o",
                output,
            ],
            capture_output=True,
            check=False,
            text=True,
        )

    def test_native_self_test_covers_usb_only_rejections(self) -> None:
        cases = (
            "valid",
            "zero-keyboards",
            "two-keyboards",
            "virtio-keyboard",
            "missing-release",
            "reordered",
            "syn-dropped",
            "partial-read",
            "panic-text",
            "deadline",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            binary = Path(temporary_directory) / "input-gate-self-test"
            result = self.compile_probe(binary, "XHCI_INPUT_GATE_SELF_TEST")
            self.assertEqual(result.returncode, 0, result.stderr)

            for case in cases:
                with self.subTest(case=case):
                    run = subprocess.run(
                        [binary, case], capture_output=True, check=False, text=True
                    )
                    self.assertEqual(run.returncode, 0, run.stderr)
                    self.assertEqual(run.stdout, f"XHCI_INPUT_SELF_TEST PASS case={case}\n")

    def test_lifecycle_prints_pass_then_holds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            binary = Path(temporary_directory) / "input-gate-lifecycle"
            result = self.compile_probe(binary, "XHCI_INPUT_GATE_LIFECYCLE_TEST")
            self.assertEqual(result.returncode, 0, result.stderr)

            with subprocess.Popen(
                [binary], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            ) as process:
                try:
                    readable, _, _ = select.select([process.stdout], [], [], 2.0)
                    self.assertTrue(readable, "lifecycle probe did not publish PASS")
                    self.assertEqual(process.stdout.readline(), "XHCI_INPUT_PASS events=8\n")
                    self.assertIsNone(process.poll(), "probe exited after publishing PASS")
                finally:
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=2)

    def test_builder_declares_exact_tools_and_entries(self) -> None:
        tools = subprocess.run(
            [self.builder, "--print-tools"], capture_output=True, check=False, text=True
        )
        entries = subprocess.run(
            [self.builder, "--print-entries"], capture_output=True, check=False, text=True
        )
        self.assertEqual(tools.returncode, 0, tools.stderr)
        self.assertEqual(tools.stdout.splitlines(), ["riscv64-linux-gnu-gcc", "cpio"])
        self.assertEqual(entries.returncode, 0, entries.stderr)
        self.assertEqual(entries.stdout.splitlines(), [".", "init"])

    def test_builder_is_reproducible_and_normalizes_newc_metadata(self) -> None:
        environment = os.environ.copy()
        environment["RISC_V_CC"] = "cc"
        environment["SOURCE_DATE_EPOCH"] = "1700000000"
        with tempfile.TemporaryDirectory() as temporary_directory:
            first = Path(temporary_directory) / "first.cpio"
            second = Path(temporary_directory) / "second.cpio"
            for output in (first, second):
                result = subprocess.run(
                    [self.builder, output],
                    capture_output=True,
                    check=False,
                    env=environment,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                time.sleep(1.1)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o644)
            self.assertEqual(
                parse_newc_entries(first.read_bytes()),
                [
                    (".", stat.S_IFDIR | 0o755, 0, 0, 1700000000),
                    ("init", stat.S_IFREG | 0o755, 0, 0, 1700000000),
                ],
            )

    def test_builder_rejects_unsafe_output_and_epoch(self) -> None:
        environment = os.environ.copy()
        environment["RISC_V_CC"] = "cc"
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory_output = Path(temporary_directory) / "archive.cpio"
            directory_output.mkdir()
            sentinel = directory_output / "sentinel"
            sentinel.write_text("keep")
            result = subprocess.run(
                [self.builder, directory_output],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(directory_output.iterdir()), [sentinel])

            environment["SOURCE_DATE_EPOCH"] = "4294967296"
            invalid_output = Path(temporary_directory) / "invalid.cpio"
            result = subprocess.run(
                [self.builder, invalid_output],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertFalse(invalid_output.exists())


if __name__ == "__main__":
    unittest.main()
