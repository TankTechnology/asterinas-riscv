#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import signal
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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


def write_test_dtb(path: Path, cpu_count: int) -> None:
    cpu_nodes = "\n".join(
        f"""cpu@{cpu} {{
            device_type = "cpu";
            reg = <{cpu}>;
            status = "okay";
        }};"""
        for cpu in range(cpu_count)
    )
    source = f"""/dts-v1/;
/ {{
    #address-cells = <2>;
    #size-cells = <2>;
    cpus {{
        #address-cells = <1>;
        #size-cells = <0>;
        timebase-frequency = <10000000>;
        {cpu_nodes}
    }};
}};
"""
    subprocess.run(
        ["dtc", "-I", "dts", "-O", "dtb", "-o", path],
        check=True,
        input=source,
        text=True,
        capture_output=True,
    )


class InputGateBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.builder = Path(__file__).parents[1] / "debian" / "build_input_gate.sh"
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
        self.assertEqual(
            argv[argv.index("-cpu") + 1],
            "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        )
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
        self.assertFalse(gate.classify_transcript(transcript + b"Kernel panic").passed)

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


class FakeProcess:
    def __init__(self) -> None:
        self.pid = 4242


class FakeBoot:
    def __init__(
        self,
        events: list[str],
        failure_marker: bytes | None = None,
        failure: Exception | None = None,
        failure_wait_number: int | None = None,
        drain_append: bytes = b"",
        drain_failure: Exception | None = None,
    ) -> None:
        self.events = events
        self.failure_marker = failure_marker
        self.failure = failure
        self.failure_wait_number = failure_wait_number
        self.drain_append = drain_append
        self.drain_failure = drain_failure
        self.wait_count = 0
        self.transcript = b""

    def wait_for(self, marker: bytes, timeout: float) -> None:
        del timeout
        self.wait_count += 1
        self.events.append(f"wait:{marker.decode()}")
        if marker == self.failure_marker or self.wait_count == self.failure_wait_number:
            raise self.failure or TimeoutError(marker.decode())
        self.transcript += marker + b"\n"

    def send_line(self, command: str) -> None:
        self.events.append(f"boot:{command}")

    def drain(self, timeout: float) -> None:
        del timeout
        self.events.append("drain")
        self.transcript += self.drain_append
        if self.drain_failure is not None:
            raise self.drain_failure


class FakeMonitor:
    def __init__(
        self,
        events: list[str],
        failure_key: str | None = None,
        connect_failure: Exception | None = None,
        close_failure: Exception | None = None,
    ) -> None:
        self.events = events
        self.failure_key = failure_key
        self.connect_failure = connect_failure
        self.close_failure = close_failure

    def connect(self) -> None:
        self.events.append("monitor:connect")
        if self.connect_failure is not None:
            raise self.connect_failure

    def send_key(self, key: str) -> None:
        self.events.append(f"key:{key}")
        if key == self.failure_key:
            raise gate.MonitorError(f"failed to send {key}")

    def close(self) -> None:
        self.events.append("monitor:close")
        if self.close_failure is not None:
            raise self.close_failure


class InputGateOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.artifacts = {}
        for name in ("kernel", "uboot", "dtb", "initramfs"):
            path = self.root / name
            path.write_bytes(f"{name}-contents".encode())
            self.artifacts[name] = path
        write_test_dtb(self.artifacts["dtb"], 4)
        self.output = self.root / "evidence"
        self.events: list[str] = []
        self.process = FakeProcess()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def config(self) -> gate.GateConfig:
        return gate.GateConfig(
            kernel=self.artifacts["kernel"],
            uboot=self.artifacts["uboot"],
            dtb=self.artifacts["dtb"],
            initramfs=self.artifacts["initramfs"],
            output_dir=self.output,
            smp=4,
            startup_timeout=1.0,
            command_timeout=1.0,
            input_timeout=1.0,
        )

    def dependencies(
        self,
        boot: FakeBoot,
        monitor: FakeMonitor | None = None,
    ) -> gate.GateDependencies:
        monitor = monitor or FakeMonitor(self.events)

        def prepare(config: gate.GateConfig, boot_disk: Path) -> None:
            del config
            self.events.append("prepare")
            boot_disk.write_bytes(b"private-ext4")

        def launch(argv: list[str]) -> FakeProcess:
            self.events.append("launch:" + " ".join(argv))
            return self.process

        def cleanup(process: FakeProcess) -> None:
            self.assertIs(process, self.process)
            self.events.append("cleanup")

        return gate.GateDependencies(
            prepare_boot_disk=prepare,
            launch_process=launch,
            boot_console=lambda process: boot,
            monitor=lambda path, timeout: monitor,
            cleanup_process=cleanup,
        )

    def _write_stale_pass_evidence(self) -> None:
        self.output.mkdir(mode=0o700, exist_ok=True)
        (self.output / "serial.log").write_bytes(b"stale PASS transcript")
        (self.output / "result.json").write_text('{"passed": true}\n')

    def test_prepare_failure_invalidates_stale_pass_evidence(self) -> None:
        self._write_stale_pass_evidence()
        dependencies = self.dependencies(FakeBoot(self.events))

        def fail_prepare(config: gate.GateConfig, boot_disk: Path) -> None:
            del config, boot_disk
            raise RuntimeError("prepare failed")

        dependencies = dataclasses.replace(
            dependencies,
            prepare_boot_disk=fail_prepare,
        )

        with self.assertRaisesRegex(RuntimeError, "prepare failed"):
            gate.run_gate(self.config(), dependencies)

        self.assertFalse((self.output / "serial.log").exists())
        self.assertFalse((self.output / "result.json").exists())

    def test_launch_interruption_invalidates_stale_pass_evidence(self) -> None:
        self._write_stale_pass_evidence()
        dependencies = self.dependencies(FakeBoot(self.events))

        def interrupt_launch(argv: list[str]) -> FakeProcess:
            del argv
            raise KeyboardInterrupt

        dependencies = dataclasses.replace(
            dependencies,
            launch_process=interrupt_launch,
        )

        with self.assertRaises(KeyboardInterrupt):
            gate.run_gate(self.config(), dependencies)

        self.assertFalse((self.output / "serial.log").exists())
        self.assertFalse((self.output / "result.json").exists())

    def test_gate_hashes_and_uses_private_artifact_snapshots(self) -> None:
        original_contents = {
            name: path.read_bytes() for name, path in self.artifacts.items()
        }
        snapshot_paths: dict[str, Path] = {}
        dependencies = self.dependencies(FakeBoot(self.events))

        def prepare(snapshot: gate.GateConfig, boot_disk: Path) -> None:
            for name in self.artifacts:
                snapshot_paths[name] = getattr(snapshot, name)
                self.artifacts[name].write_bytes(f"changed-{name}".encode())
            boot_disk.write_bytes(b"private-ext4")

        dependencies = dataclasses.replace(
            dependencies,
            prepare_boot_disk=prepare,
        )

        result = gate.run_gate(self.config(), dependencies)

        self.assertTrue(result.passed)
        for name, original_content in original_contents.items():
            self.assertNotEqual(snapshot_paths[name], self.artifacts[name])
            self.assertEqual(snapshot_paths[name].parent.parent, self.output)
            self.assertEqual(
                result.sha256[name], hashlib.sha256(original_content).hexdigest()
            )
        launched_uboot = Path(result.qemu_argv[result.qemu_argv.index("-kernel") + 1])
        self.assertEqual(launched_uboot, snapshot_paths["uboot"])
        self.assertFalse(snapshot_paths["uboot"].parent.exists())
        self.assertEqual(list(self.output.glob(".artifacts.*")), [])

    def test_success_observes_boot_then_keys_then_pass_and_writes_evidence(
        self,
    ) -> None:
        boot = FakeBoot(self.events)

        result = gate.run_gate(self.config(), self.dependencies(boot))

        expected_boot_commands = [
            "version",
            "virtio scan",
            "ext4ls virtio 0:0 /",
            "ext4load virtio 0:0 0x80200000 /asterinas.booti",
            "ext4load virtio 0:0 0x90000000 /qemu-virt.dtb",
            "fdt addr 0x90000000",
            "setenv bootargs console=ttyS0 loglevel=warn init=/init",
            "ext4load virtio 0:0 0x83000000 /initramfs.cpio.gz",
            "setenv initrd_size ${filesize}",
            "booti 0x80200000 0x83000000:${initrd_size} 0x90000000",
        ]
        expected = ["prepare", mock.ANY, "wait:=> "]
        for command in expected_boot_commands[:-1]:
            expected.extend([f"boot:{command}", "wait:=> "])
        expected.extend(
            [
                f"boot:{expected_boot_commands[-1]}",
                "wait:Starting kernel",
                f"wait:{gate.READY_MARKER.decode()}",
                "monitor:connect",
                "key:a",
                "key:shift-b",
                "key:backspace",
                "key:ctrl-c",
                f"wait:{gate.PASS_MARKER.decode()}",
                "monitor:close",
                "cleanup",
                "drain",
            ]
        )
        self.assertEqual(self.events, expected)
        self.assertTrue(result.passed)

        serial_path = self.output / "serial.log"
        result_path = self.output / "result.json"
        evidence = json.loads(result_path.read_text())
        self.assertEqual(serial_path.read_bytes(), boot.transcript)
        self.assertEqual(evidence["smp"], 4)
        self.assertTrue(evidence["ready"])
        self.assertTrue(evidence["complete"])
        self.assertEqual(evidence["panics"], [])
        self.assertTrue(evidence["passed"])
        self.assertEqual(evidence["terminal_reason"], "passed")
        self.assertEqual(evidence["qemu_argv"], result.qemu_argv)
        self.assertEqual(
            set(evidence["sha256"]),
            {"uboot", "boot_disk", "kernel", "dtb", "initramfs"},
        )
        for name, path in self.artifacts.items():
            self.assertEqual(
                evidence["sha256"][name],
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        self.assertEqual(
            evidence["sha256"]["boot_disk"],
            hashlib.sha256(b"private-ext4").hexdigest(),
        )
        self.assertEqual(
            result_path.read_text(),
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        )

    def test_cleanup_and_evidence_follow_ready_timeout(self) -> None:
        boot = FakeBoot(
            self.events,
            gate.READY_MARKER,
            TimeoutError("guest READY timeout"),
        )

        result = gate.run_gate(self.config(), self.dependencies(boot))

        self.assertFalse(result.passed)
        self.assertEqual(result.terminal_reason, "timeout: guest READY")
        self.assertEqual(self.events[-3:], ["monitor:close", "cleanup", "drain"])
        self.assertTrue((self.output / "serial.log").exists())
        evidence = json.loads((self.output / "result.json").read_text())
        self.assertFalse(evidence["passed"])
        self.assertEqual(evidence["terminal_reason"], "timeout: guest READY")

    def test_timeout_reason_distinguishes_uboot_ready_and_pass_phases(self) -> None:
        cases = (
            (1, None, "timeout: U-Boot prompt"),
            (2, None, "timeout: U-Boot command"),
            (None, gate.READY_MARKER, "timeout: guest READY"),
            (None, gate.PASS_MARKER, "timeout: guest PASS"),
        )
        for wait_number, marker, expected_reason in cases:
            with self.subTest(reason=expected_reason):
                events: list[str] = []
                self.events = events
                boot = FakeBoot(
                    events,
                    failure_marker=marker,
                    failure=TimeoutError("deadline expired"),
                    failure_wait_number=wait_number,
                )

                result = gate.run_gate(self.config(), self.dependencies(boot))

                self.assertFalse(result.passed)
                self.assertEqual(result.terminal_reason, expected_reason)

    def test_cleanup_and_evidence_follow_monitor_failure(self) -> None:
        boot = FakeBoot(self.events)
        monitor = FakeMonitor(
            self.events,
            connect_failure=gate.MonitorError("connect failed"),
        )

        result = gate.run_gate(
            self.config(),
            self.dependencies(boot, monitor),
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.terminal_reason, "monitor failure")
        self.assertEqual(self.events[-3:], ["monitor:close", "cleanup", "drain"])
        self.assertNotIn("key:a", self.events)
        self.assertNotIn(f"wait:{gate.PASS_MARKER.decode()}", self.events)

    def test_cleanup_and_evidence_follow_early_process_exit(self) -> None:
        boot = FakeBoot(
            self.events,
            b"=> ",
            gate.EarlyProcessExit("QEMU exited with status 7"),
        )

        result = gate.run_gate(self.config(), self.dependencies(boot))

        self.assertFalse(result.passed)
        self.assertEqual(result.terminal_reason, "early process exit")
        self.assertEqual(self.events[-3:], ["monitor:close", "cleanup", "drain"])

    def test_panic_prevents_pass_even_after_pass_marker(self) -> None:
        boot = FakeBoot(self.events)
        original_wait_for = boot.wait_for

        def wait_with_panic(marker: bytes, timeout: float) -> None:
            original_wait_for(marker, timeout)
            if marker == gate.PASS_MARKER:
                boot.transcript += b"Kernel panic\n"

        boot.wait_for = wait_with_panic

        result = gate.run_gate(self.config(), self.dependencies(boot))

        self.assertTrue(result.complete)
        self.assertEqual(result.panics, ("Kernel panic",))
        self.assertFalse(result.passed)
        self.assertEqual(result.terminal_reason, "panic detected")

    def test_panic_arriving_during_post_cleanup_drain_prevents_pass(self) -> None:
        boot = FakeBoot(self.events, drain_append=b"Kernel panic\n")

        result = gate.run_gate(self.config(), self.dependencies(boot))

        self.assertFalse(result.passed)
        self.assertEqual(result.panics, ("Kernel panic",))
        self.assertEqual(result.terminal_reason, "panic detected")
        self.assertIn(b"Kernel panic", (self.output / "serial.log").read_bytes())
        evidence = json.loads((self.output / "result.json").read_text())
        self.assertEqual(evidence["panics"], ["Kernel panic"])
        self.assertFalse(evidence["passed"])

    def test_serial_drain_failure_is_recorded_and_prevents_pass(self) -> None:
        boot = FakeBoot(self.events, drain_failure=OSError("drain failed"))

        result = gate.run_gate(self.config(), self.dependencies(boot))

        self.assertFalse(result.passed)
        self.assertEqual(result.terminal_reason, "serial drain failure")
        self.assertEqual(self.events[-3:], ["monitor:close", "cleanup", "drain"])

    def test_serial_drain_timeout_is_recorded_and_prevents_pass(self) -> None:
        boot = FakeBoot(self.events, drain_failure=TimeoutError("still open"))

        result = gate.run_gate(self.config(), self.dependencies(boot))

        self.assertFalse(result.passed)
        self.assertEqual(result.terminal_reason, "serial drain timeout")

    def test_cleanup_failure_is_recorded_and_prevents_pass(self) -> None:
        boot = FakeBoot(self.events)
        dependencies = self.dependencies(boot)

        def fail_cleanup(process: FakeProcess) -> None:
            del process
            self.events.append("cleanup")
            raise RuntimeError("descendant survived")

        dependencies = dataclasses.replace(
            dependencies,
            cleanup_process=fail_cleanup,
        )

        result = gate.run_gate(self.config(), dependencies)

        self.assertFalse(result.passed)
        self.assertEqual(result.terminal_reason, "cleanup failure")
        self.assertEqual(self.events[-3:], ["monitor:close", "cleanup", "drain"])

    def test_monitor_close_failure_is_recorded_and_prevents_pass(self) -> None:
        boot = FakeBoot(self.events)
        monitor = FakeMonitor(
            self.events,
            close_failure=gate.MonitorError("close failed"),
        )

        result = gate.run_gate(self.config(), self.dependencies(boot, monitor))

        self.assertFalse(result.passed)
        self.assertEqual(result.terminal_reason, "monitor close failure")
        self.assertEqual(self.events[-3:], ["monitor:close", "cleanup", "drain"])

    def test_boot_disk_preparation_is_atomic_and_contains_only_contract_files(
        self,
    ) -> None:
        self.output.mkdir(mode=0o700)
        boot_disk = self.output / "boot.ext4"
        commands = []

        def run_command(argv: list[str]) -> None:
            commands.append(argv)
            if argv[0] == "mkfs.ext4":
                stage = Path(argv[argv.index("-d") + 1])
                self.assertEqual(
                    sorted(path.name for path in stage.iterdir()),
                    ["asterinas.booti", "initramfs.cpio.gz", "qemu-virt.dtb"],
                )
                Path(argv[-2]).write_bytes(b"formatted")

        gate.prepare_boot_disk(self.config(), boot_disk, run_command=run_command)

        self.assertEqual(boot_disk.read_bytes(), b"formatted")
        self.assertEqual(commands[0][:4], ["mkfs.ext4", "-q", "-F", "-d"])
        self.assertGreaterEqual(int(commands[0][-1]), 64 * 1024)
        self.assertEqual(
            commands[1],
            ["debugfs", "-w", "-R", "rmdir lost+found", mock.ANY],
        )
        self.assertEqual(list(self.output.glob(".boot.ext4.*")), [])

    def test_real_boot_disk_contains_exactly_the_three_payloads(self) -> None:
        self.output.mkdir(mode=0o700)
        boot_disk = self.output / "boot.ext4"

        gate.prepare_boot_disk(self.config(), boot_disk)

        listing = subprocess.run(
            ["debugfs", "-R", "ls -p /", boot_disk],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        names = {
            fields[5]
            for line in listing.splitlines()
            if len(fields := line.split("/")) > 5 and fields[5] not in (".", "..")
        }
        self.assertEqual(
            names,
            {"asterinas.booti", "initramfs.cpio.gz", "qemu-virt.dtb"},
        )

    def test_boot_disk_failure_preserves_previous_disk(self) -> None:
        self.output.mkdir(mode=0o700)
        boot_disk = self.output / "boot.ext4"
        boot_disk.write_bytes(b"previous")

        def fail(argv: list[str]) -> None:
            del argv
            raise subprocess.CalledProcessError(1, "mkfs.ext4")

        with self.assertRaises(subprocess.CalledProcessError):
            gate.prepare_boot_disk(
                self.config(),
                boot_disk,
                run_command=fail,
            )

        self.assertEqual(boot_disk.read_bytes(), b"previous")
        self.assertEqual(list(self.output.glob(".boot.ext4.*")), [])

    def test_config_rejects_invalid_timeouts_and_artifacts(self) -> None:
        for timeout in (0.0, -1.0, float("inf"), float("nan")):
            with self.subTest(timeout=timeout):
                config = dataclasses.replace(self.config(), input_timeout=timeout)
                with self.assertRaisesRegex(ValueError, "finite and positive"):
                    gate.validate_config(config)

        empty = self.root / "empty"
        empty.touch()
        with self.assertRaisesRegex(ValueError, "nonempty regular file"):
            gate.validate_config(dataclasses.replace(self.config(), dtb=empty))

        link = self.root / "kernel-link"
        link.symlink_to(self.artifacts["kernel"])
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            gate.validate_config(dataclasses.replace(self.config(), kernel=link))

    def test_dtb_enabled_cpu_count_accepts_one_and_four_node_contracts(self) -> None:
        for cpu_count in (1, 4):
            with self.subTest(cpu_count=cpu_count):
                dtb = self.root / f"{cpu_count}-cpu.dtb"
                write_test_dtb(dtb, cpu_count)
                gate.validate_dtb_cpu_count(dtb, cpu_count)

    def test_dtb_smp_mismatch_is_rejected_before_disk_preparation(self) -> None:
        write_test_dtb(self.artifacts["dtb"], 1)
        self._write_stale_pass_evidence()
        prepared = False
        dependencies = self.dependencies(FakeBoot(self.events))

        def record_prepare(config: gate.GateConfig, boot_disk: Path) -> None:
            nonlocal prepared
            del config, boot_disk
            prepared = True

        dependencies = dataclasses.replace(
            dependencies,
            prepare_boot_disk=record_prepare,
        )

        with self.assertRaisesRegex(ValueError, "enabled CPU count 1.*SMP 4"):
            gate.run_gate(self.config(), dependencies)

        self.assertFalse(prepared)
        self.assertFalse((self.output / "result.json").exists())

    def test_output_directory_is_private_and_rejects_unsafe_existing_paths(
        self,
    ) -> None:
        config = gate.validate_config(self.config())
        self.assertEqual(stat.S_IMODE(config.output_dir.stat().st_mode), 0o700)

        for mode in (0o500, 0o755, 0o1700):
            unsafe_output = self.root / f"unsafe-{mode:o}"
            unsafe_output.mkdir(mode=0o700)
            unsafe_output.chmod(mode)
            with self.subTest(mode=oct(mode)):
                with self.assertRaisesRegex(ValueError, "exact mode 0700"):
                    gate.validate_config(
                        dataclasses.replace(self.config(), output_dir=unsafe_output)
                    )

        output_link = self.root / "output-link"
        output_link.symlink_to(config.output_dir, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            gate.validate_config(
                dataclasses.replace(self.config(), output_dir=output_link)
            )

    def test_new_output_directory_is_chmoded_to_0700_despite_umask(self) -> None:
        output = self.root / "umask-output"
        original_umask = os.umask(0o777)
        try:
            config = gate.validate_config(
                dataclasses.replace(self.config(), output_dir=output)
            )
        finally:
            os.umask(original_umask)

        self.assertEqual(stat.S_IMODE(config.output_dir.stat().st_mode), 0o700)

    def test_config_rejects_comma_before_qemu_structured_options(self) -> None:
        comma_output = self.root / "comma,output"

        with self.assertRaisesRegex(ValueError, "comma"):
            gate.validate_config(
                dataclasses.replace(self.config(), output_dir=comma_output)
            )

    def test_serial_console_does_not_reuse_an_old_prompt(self) -> None:
        read_fd, write_fd = os.pipe()
        process = type(
            "PipeProcess",
            (),
            {
                "stdin": io.BytesIO(),
                "stdout": os.fdopen(read_fd, "rb", buffering=0),
                "poll": lambda self: None,
            },
        )()
        try:
            os.write(write_fd, b"=> ")
            console = gate.SerialBootConsole(process)
            console.wait_for(b"=> ", 0.1)

            with self.assertRaises(TimeoutError):
                console.wait_for(b"=> ", 0.01)
        finally:
            os.close(write_fd)
            process.stdout.close()

    def test_hmp_partial_responses_obey_one_total_command_deadline(self) -> None:
        class PartialSocket:
            def __init__(self) -> None:
                self.timeouts: list[float] = []
                self.recv_count = 0

            def sendall(self, command: bytes) -> None:
                self.command = command

            def settimeout(self, timeout: float) -> None:
                self.timeouts.append(timeout)

            def recv(self, size: int) -> bytes:
                del size
                self.recv_count += 1
                if self.recv_count > 2:
                    raise AssertionError("read beyond total deadline")
                return b"partial"

        fake_socket = PartialSocket()
        monitor = gate.HmpMonitor(Path("/monitor.sock"), 1.0)
        monitor._socket = fake_socket

        with mock.patch.object(
            gate.time,
            "monotonic",
            side_effect=(0.0, 0.25, 0.75, 1.0),
        ):
            with self.assertRaisesRegex(gate.MonitorError, "failed to send"):
                monitor.send_key("a")

        self.assertEqual(fake_socket.command, b"sendkey a\n")
        self.assertEqual(fake_socket.timeouts, [0.75, 0.25])

    def test_hmp_response_rejects_more_than_the_total_byte_limit(self) -> None:
        class OversizedSocket:
            def sendall(self, command: bytes) -> None:
                del command

            def settimeout(self, timeout: float) -> None:
                del timeout

            def recv(self, size: int) -> bytes:
                del size
                return b"x" * (gate.HMP_MAX_RESPONSE_BYTES + 1)

        monitor = gate.HmpMonitor(Path("/monitor.sock"), 1.0)
        monitor._socket = OversizedSocket()
        with mock.patch.object(gate.time, "monotonic", return_value=0.0):
            with self.assertRaisesRegex(gate.MonitorError, "response byte limit"):
                monitor.send_key("a")

    def test_hmp_inter_key_delay_must_fit_inside_command_deadline(self) -> None:
        class PromptSocket:
            def sendall(self, command: bytes) -> None:
                del command

            def settimeout(self, timeout: float) -> None:
                del timeout

            def recv(self, size: int) -> bytes:
                del size
                return b"(qemu) "

        monitor = gate.HmpMonitor(Path("/monitor.sock"), 0.04)
        monitor._socket = PromptSocket()
        with (
            mock.patch.object(gate.time, "monotonic", return_value=0.0),
            mock.patch.object(gate.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(gate.MonitorError, "inter-key delay"):
                monitor.send_key("a")
        sleep.assert_not_called()

    def test_cleanup_targets_process_group_when_leader_already_exited(self) -> None:
        class ExitedLeader:
            pid = 4242

            def __init__(self) -> None:
                self.wait_calls = []

            def poll(self) -> int:
                return 7

            def wait(self, timeout: float | None = None) -> int:
                self.wait_calls.append(timeout)
                return 7

        process = ExitedLeader()
        kill_calls = []

        def killpg(process_group: int, requested_signal: int) -> None:
            kill_calls.append((process_group, requested_signal))
            if requested_signal == 0 and kill_calls.count((process_group, 0)) > 1:
                raise ProcessLookupError

        with (
            mock.patch.object(gate.os, "killpg", side_effect=killpg),
            mock.patch.object(gate.time, "sleep"),
        ):
            gate._cleanup_process(process)

        self.assertEqual(kill_calls[0], (process.pid, signal.SIGTERM))
        self.assertEqual(kill_calls[1:], [(process.pid, 0), (process.pid, 0)])
        self.assertTrue(process.wait_calls)


if __name__ == "__main__":
    unittest.main()
