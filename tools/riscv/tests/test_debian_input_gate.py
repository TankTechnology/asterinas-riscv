#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import dataclasses
import hashlib
import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tools.riscv.debian import input_gate as gate


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
        name_bytes = archive[name_start:name_end]
        if not name_bytes.endswith(b"\0"):
            raise ValueError("unterminated newc member name")
        name = name_bytes[:-1].decode()

        data_start = (name_end + 3) & ~3
        offset = (data_start + file_size + 3) & ~3
        if name == "TRAILER!!!":
            return entries

        entries.append((name, mode, uid, gid, mtime))


class InputGateBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.builder = (
            Path(__file__).parents[1] / "debian" / "build_input_gate.sh"
        )
        cls.default_output = (
            Path(__file__).parents[3]
            / "target"
            / "debian-riscv"
            / "input-gate"
            / "initramfs.cpio"
        )

    def test_print_tools_lists_cross_compiler_and_cpio(self) -> None:
        result = subprocess.run(
            [self.builder, "--print-tools"],
            capture_output=True,
            check=False,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            ["riscv64-linux-gnu-gcc", "cpio"],
        )

    def test_print_entries_lists_only_archive_root_and_init(self) -> None:
        result = subprocess.run(
            [self.builder, "--print-entries"],
            capture_output=True,
            check=False,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [".", "init"])

    def test_unknown_option_exits_two_without_creating_output(self) -> None:
        def snapshot_output() -> tuple[int, ...] | None:
            if not self.default_output.exists():
                return None

            metadata = self.default_output.stat()
            try:
                digest = int.from_bytes(
                    hashlib.sha256(self.default_output.read_bytes()).digest(),
                    "big",
                )
            except PermissionError:
                digest = 0
            return (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_uid,
                metadata.st_gid,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
                digest,
            )

        original_output = snapshot_output()

        result = subprocess.run(
            [self.builder, "--unknown"],
            capture_output=True,
            check=False,
            text=True,
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("unknown option", result.stderr)
        if original_output is None:
            self.assertFalse(self.default_output.exists())
        else:
            self.assertEqual(snapshot_output(), original_output)

    def test_build_is_reproducible_with_default_source_date_epoch(self) -> None:
        environment = os.environ.copy()
        environment["RISC_V_CC"] = "cc"
        environment.pop("SOURCE_DATE_EPOCH", None)

        with tempfile.TemporaryDirectory() as temporary_directory:
            first_output = Path(temporary_directory) / "first.cpio"
            second_output = Path(temporary_directory) / "second.cpio"

            first_result = subprocess.run(
                [self.builder, first_output],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )
            self.assertEqual(first_result.returncode, 0, first_result.stderr)

            time.sleep(1.1)

            second_result = subprocess.run(
                [self.builder, second_output],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )
            self.assertEqual(second_result.returncode, 0, second_result.stderr)

            self.assertEqual(first_output.read_bytes(), second_output.read_bytes())

    def test_source_date_epoch_must_fit_newc_mtime(self) -> None:
        environment = os.environ.copy()
        environment["RISC_V_CC"] = "cc"

        for invalid_epoch in ("not-a-number", "-1", "4294967296"):
            with self.subTest(source_date_epoch=invalid_epoch):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    output = Path(temporary_directory) / "initramfs.cpio"
                    environment["SOURCE_DATE_EPOCH"] = invalid_epoch
                    result = subprocess.run(
                        [self.builder, output],
                        capture_output=True,
                        check=False,
                        env=environment,
                        text=True,
                    )

                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("SOURCE_DATE_EPOCH", result.stderr)
                    self.assertFalse(output.exists())

    def test_existing_directory_is_not_used_as_output(self) -> None:
        environment = os.environ.copy()
        environment["RISC_V_CC"] = "cc"

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "initramfs.cpio"
            output.mkdir()
            sentinel = output / "sentinel"
            sentinel.write_text("keep me")

            result = subprocess.run(
                [self.builder, output],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(output.is_dir())
            self.assertEqual(sentinel.read_text(), "keep me")
            self.assertEqual(set(output.iterdir()), {sentinel})
            self.assertEqual(
                list(Path(temporary_directory).glob(".initramfs.cpio.tmp.*")),
                [],
            )

    def test_archive_has_canonical_metadata_and_filesystem_mode(self) -> None:
        source_date_epoch = 1_700_000_000
        environment = os.environ.copy()
        environment["RISC_V_CC"] = "cc"
        environment["SOURCE_DATE_EPOCH"] = str(source_date_epoch)

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "initramfs.cpio"
            result = subprocess.run(
                [self.builder, output],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with self.subTest(contract="archive filesystem mode"):
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o644)
            with self.subTest(contract="newc member metadata"):
                self.assertEqual(
                    parse_newc_entries(output.read_bytes()),
                    [
                        (".", stat.S_IFDIR | 0o755, 0, 0, source_date_epoch),
                        (
                            "init",
                            stat.S_IFREG | 0o755,
                            0,
                            0,
                            source_date_epoch,
                        ),
                    ],
                )

    def test_compile_failure_preserves_existing_output(self) -> None:
        environment = os.environ.copy()
        environment["RISC_V_CC"] = "false"

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory)
            output = output_directory / "initramfs.cpio"
            output.write_text("keep me")

            result = subprocess.run(
                [self.builder, output],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_text(), "keep me")
            self.assertEqual(
                list(output_directory.glob(".initramfs.cpio.tmp.*")),
                [],
            )


class InputGateContractTests(unittest.TestCase):
    def test_guest_input_state_machine_self_test(self) -> None:
        guest_source = Path(__file__).parents[1] / "debian" / "input_gate_init.c"

        with tempfile.TemporaryDirectory() as temporary_directory:
            guest_binary = Path(temporary_directory) / "input-gate-self-test"
            compile_result = subprocess.run(
                [
                    "cc",
                    "-std=c11",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-DINPUT_GATE_SELF_TEST",
                    str(guest_source),
                    "-o",
                    str(guest_binary),
                ],
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)

            run_result = subprocess.run(
                [guest_binary],
                capture_output=True,
                check=False,
                text=True,
            )

        self.assertEqual(run_result.returncode, 0, run_result.stderr)
        self.assertIn("input gate state machine: PASS", run_result.stdout)

    def test_qemu_argv_uses_smp4_and_two_distinct_input_devices(self) -> None:
        argv = gate.qemu_argv(
            Path("/u-boot"),
            Path("/boot.ext4"),
            Path("/tmp/gate-monitor.sock"),
            4,
        )

        self.assertEqual(argv[argv.index("-machine") + 1], "virt")
        self.assertEqual(argv[argv.index("-smp") + 1], "4")
        self.assertIn("virtio-tablet-device", argv)
        self.assertIn("virtio-keyboard-device", argv)
        self.assertLess(
            argv.index("virtio-tablet-device"),
            argv.index("virtio-keyboard-device"),
        )
        self.assertIn(
            "if=none,format=raw,file=/boot.ext4,id=bootdisk",
            argv,
        )
        self.assertIn("virtio-blk-device,drive=bootdisk", argv)
        self.assertEqual(
            argv[argv.index("-monitor") + 1],
            "unix:/tmp/gate-monitor.sock,server=on,wait=off",
        )
        self.assertEqual(argv[argv.index("-serial") + 1], "stdio")
        self.assertEqual(argv[argv.index("-nic") + 1], "none")
        self.assertNotIn("-netdev", argv)
        self.assertFalse(any("virtio-net" in argument for argument in argv))

    def test_qemu_argv_defaults_to_smp4(self) -> None:
        argv = gate.qemu_argv(
            Path("/u-boot"),
            Path("/boot.ext4"),
            Path("/tmp/gate-monitor.sock"),
        )

        self.assertEqual(argv[argv.index("-smp") + 1], "4")

    def test_qemu_argv_rejects_comma_in_boot_disk_path(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "QEMU boot disk path must not contain a comma",
        ):
            gate.qemu_argv(
                Path("/u-boot"),
                Path("/boot,disk.ext4"),
                Path("/tmp/gate-monitor.sock"),
            )

    def test_qemu_argv_rejects_comma_in_monitor_socket_path(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "monitor_socket must not contain a comma",
        ):
            gate.qemu_argv(
                Path("/u-boot"),
                Path("/boot.ext4"),
                Path("/tmp/gate,monitor.sock"),
            )

    def test_qemu_argv_rejects_non_positive_or_non_integer_smp(self) -> None:
        paths = (
            Path("/u-boot"),
            Path("/boot.ext4"),
            Path("/tmp/gate-monitor.sock"),
        )

        for invalid_smp in (0, -1, True, False, 1.0, "4"):
            with self.subTest(smp=invalid_smp):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    gate.qemu_argv(*paths, invalid_smp)

    def test_injected_sequence_covers_normal_modifier_and_editing_keys(self) -> None:
        self.assertEqual(
            gate.KEY_SEQUENCE,
            ("a", "shift-b", "backspace", "ctrl-c"),
        )

    def test_classification_requires_ready_and_pass_without_panic(self) -> None:
        transcript = gate.READY_MARKER + b"\n" + gate.PASS_MARKER + b"\n"

        self.assertTrue(gate.classify_transcript(transcript).passed)
        self.assertFalse(gate.classify_transcript(gate.PASS_MARKER).passed)
        self.assertFalse(
            gate.classify_transcript(transcript + b"Kernel panic").passed
        )

    def test_classification_reports_each_panic_marker(self) -> None:
        transcript = (
            gate.READY_MARKER
            + b"\n"
            + gate.PASS_MARKER
            + b"\n"
            + b"\n".join(gate.PANIC_MARKERS)
        )

        result = gate.classify_transcript(transcript)

        self.assertTrue(result.ready)
        self.assertTrue(result.complete)
        self.assertEqual(
            result.panics,
            tuple(marker.decode() for marker in gate.PANIC_MARKERS),
        )
        self.assertFalse(result.passed)

    def test_gate_result_is_frozen(self) -> None:
        result = gate.GateResult(ready=True, complete=True, panics=())

        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.ready = False


if __name__ == "__main__":
    unittest.main()
