#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import os
import json
import select
import signal
import stat
import subprocess
import tempfile
import time
import unittest
from dataclasses import dataclass
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

    def compile_probe(
        self, output: Path, define: str
    ) -> subprocess.CompletedProcess[str]:
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
            "delayed-keyboard",
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
                    self.assertEqual(
                        run.stdout, f"XHCI_INPUT_SELF_TEST PASS case={case}\n"
                    )

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
                    self.assertEqual(
                        process.stdout.readline(), "XHCI_INPUT_PASS events=8\n"
                    )
                    self.assertIsNone(
                        process.poll(), "probe exited after publishing PASS"
                    )
                finally:
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=2)

    def test_builder_declares_exact_tools_and_entries(self) -> None:
        tools = subprocess.run(
            [self.builder, "--print-tools"], capture_output=True, check=False, text=True
        )
        entries = subprocess.run(
            [self.builder, "--print-entries"],
            capture_output=True,
            check=False,
            text=True,
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


class XhciHostGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from tools.riscv.xhci import input_gate

        cls.gate = input_gate

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.directory.chmod(0o700)
        self.uboot = self.directory / "u-boot"
        self.boot_disk = self.directory / "boot.ext4"
        self.manifest = self.directory / "artifacts.json"
        self.serial_log = self.directory / "serial.log"
        self.result = self.directory / "result.json"
        self.uboot.write_bytes(b"uboot")
        self.boot_disk.write_bytes(b"disk")
        self.manifest.write_text("{}")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def config(self):
        return self.gate.GateConfig(
            uboot=self.uboot,
            boot_disk=self.boot_disk,
            manifest=self.manifest,
            serial_log=self.serial_log,
            result=self.result,
            smp=4,
            startup_timeout=0.2,
            command_timeout=0.2,
            input_timeout=0.2,
        )

    def test_qemu_argv_has_only_pci_xhci_usb_keyboard(self) -> None:
        argv = self.gate.qemu_argv(
            self.uboot,
            self.boot_disk,
            self.directory / "monitor.sock",
            4,
        )
        joined = " ".join(argv)
        self.assertIn("-smp 4", joined)
        self.assertLess(
            argv.index("qemu-xhci,id=xhci,msi=off,msix=off"),
            argv.index("usb-kbd,id=usb-kbd,bus=xhci.0"),
        )
        self.assertEqual(sum(item.startswith("usb-kbd,") for item in argv), 1)
        for forbidden in ("virtio-keyboard", "i8042", "-nic user", "usb-tablet"):
            self.assertNotIn(forbidden, joined)
        self.assertIn("-nic none", joined)

    def test_registered_uboot_commands_preserve_expected_output(self) -> None:
        artifacts = self.gate.ArtifactExpectations(
            kernel_size=1024,
            kernel_crc32="11111111",
            dtb_size=1024,
            dtb_crc32="22222222",
            initrd_size=1024,
            initrd_crc32="33333333",
            kernel_sha256="1" * 64,
            dtb_sha256="2" * 64,
            initrd_sha256="3" * 64,
        )
        commands = self.gate._registered_commands(artifacts)
        self.assertEqual(commands[0].name, "version")
        self.assertEqual(commands[0].expected, b"U-Boot 2026.07")
        self.assertEqual(commands[-1].name, "booti")
        self.assertEqual(commands[-1].expected, b"Starting kernel ...")

    def test_gate_rejects_non_smp4_dtb_and_plan_builds_sv39_smp4(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for count in (1, 4):
                cpus = "".join(
                    f'cpu@{index} {{ device_type = "cpu"; reg = <{index}>; status = "okay"; }};'
                    for index in range(count)
                )
                source = directory / f"{count}.dts"
                output = directory / f"{count}.dtb"
                source.write_text(
                    f"/dts-v1/; / {{ #address-cells = <2>; #size-cells = <2>; cpus {{ #address-cells = <1>; #size-cells = <0>; {cpus} }}; }};"
                )
                result = subprocess.run(
                    ["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", output, source],
                    capture_output=True,
                    check=False,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            with self.assertRaisesRegex(ValueError, "exactly 4 enabled CPU"):
                self.gate._validate_dtb_cpu_count(directory / "1.dtb")
            self.gate._validate_dtb_cpu_count(directory / "4.dtb")

        root = Path(__file__).parents[3]
        plan = (
            root / "docs/superpowers/plans/2026-08-24-riscv-pci-xhci-m1.md"
        ).read_text()
        self.assertIn("FEATURES=riscv_sv39_mode", plan)
        self.assertIn("QEMU_UBOOT_PROFILE=generic-sv39-ltp-smp4", plan)

    def test_classifier_requires_current_ordered_exact_evidence(self) -> None:
        valid = self.gate.expected_transcript()
        result = self.gate.classify_transcript(valid)
        self.assertTrue(result.passed, result.reason)
        mutations = {
            "missing": valid.replace(self.gate.USB_MARKER + b"\n", b""),
            "duplicate": valid.replace(
                self.gate.PCI_MARKER + b"\n",
                self.gate.PCI_MARKER + b"\n" + self.gate.PCI_MARKER + b"\n",
            ),
            "stale-before-ready": self.gate.PASS_MARKER + b"\n" + valid,
            "reversed": b"\n".join(reversed(valid.splitlines())) + b"\n",
            "fallback": valid + b"virtio_keyboard registered\n",
            "panic": valid + b"Kernel panic\n",
        }
        for name, transcript in mutations.items():
            with self.subTest(name=name):
                self.assertFalse(self.gate.classify_transcript(transcript).passed)

    def test_run_gate_orders_ready_keys_cleanup_and_atomic_evidence(self) -> None:
        actions: list[str] = []
        gate = self.gate

        @dataclass
        class Process:
            pid: int = 123

        class Console:
            def __init__(self) -> None:
                self.transcript = b"U-Boot 2026.07\n=> "

            def wait_for(self, marker: bytes, timeout: float) -> None:
                del timeout
                actions.append(f"wait:{marker.decode(errors='replace')}")
                if marker == gate.PCI_MARKER:
                    self.transcript += gate.PCI_MARKER + b"\n"
                elif marker == gate.USB_MARKER:
                    self.transcript += gate.USB_MARKER + b"\n"
                elif marker == gate.READY_MARKER:
                    self.transcript += b"XHCI_INPUT_READY path=/dev/input/event0 bustype=3 name=usb_boot_keyboard\n"
                elif marker == gate.PASS_MARKER:
                    self.transcript += gate.EVENT_TRANSCRIPT + gate.PASS_MARKER + b"\n"
                else:
                    self.transcript += marker + b"\n"

            def send_line(self, command: str) -> None:
                actions.append(f"serial:{command}")

            def drain(self, timeout: float) -> None:
                del timeout
                actions.append("drain")

        console = Console()

        class Monitor:
            def connect(self) -> None:
                actions.append("monitor:connect")

            def send_key(self, key: str) -> None:
                actions.append(f"key:{key}")

            def close(self) -> None:
                actions.append("monitor:close")

        monitor_paths: list[Path] = []
        monitor_modes: list[int] = []
        monitor_parents: list[Path] = []

        def monitor(path: Path, timeout: float):
            del timeout
            monitor_paths.append(path)
            monitor_modes.append(stat.S_IMODE(path.parent.stat().st_mode))
            monitor_parents.append(path.parent.resolve())
            return Monitor()

        dependencies = self.gate.GateDependencies(
            validate_artifacts=lambda disk, manifest, pass_fds: {"validated": True},
            boot_commands=lambda artifacts: (
                self.gate.BootCommand("boot", "booti", b"Starting kernel"),
            ),
            launch_process=lambda argv, pass_fds: Process(),
            boot_console=lambda process: console,
            monitor=monitor,
            cleanup_process=lambda process: actions.append("cleanup"),
            qemu_version=lambda: "QEMU emulator version test",
        )
        result = self.gate.run_gate(self.config(), dependencies)
        self.assertTrue(result.passed, result.reason)
        self.assertLess(actions.index("wait:XHCI_INPUT_READY"), actions.index("key:a"))
        self.assertLess(actions.index("key:a"), actions.index("key:1"))
        self.assertEqual(actions[-3:], ["monitor:close", "cleanup", "drain"])
        self.assertEqual(monitor_modes, [0o700])
        self.assertTrue(monitor_parents[0].is_relative_to(self.directory))

        evidence = json.loads(self.result.read_text())
        self.assertTrue(evidence["passed"])
        self.assertEqual(evidence["smp"], 4)
        self.assertEqual(evidence["pci"]["bdf"], "0000:00:01.0")
        self.assertEqual(evidence["usb"]["vendor_id"], "0627")
        self.assertEqual(len(evidence["events"]), 8)
        self.assertEqual(evidence["cleanup"], "complete")
        self.assertEqual(self.serial_log.read_bytes(), console.transcript)

    def test_lifecycle_failure_overrides_pass(self) -> None:
        gate = self.gate

        class Console:
            transcript = b""

            def wait_for(self, marker: bytes, timeout: float) -> None:
                del timeout
                if marker == gate.PCI_MARKER:
                    self.transcript += marker + b"\n"
                elif marker == gate.USB_MARKER:
                    self.transcript += marker + b"\n"
                elif marker == gate.READY_MARKER:
                    self.transcript += b"XHCI_INPUT_READY path=/dev/input/event0 bustype=3 name=usb_boot_keyboard\n"
                elif marker == gate.PASS_MARKER:
                    self.transcript += gate.EVENT_TRANSCRIPT + marker + b"\n"
                else:
                    self.transcript += marker + b"\n"

            def send_line(self, command: str) -> None:
                del command

            def drain(self, timeout: float) -> None:
                del timeout

        class Monitor:
            def connect(self) -> None:
                pass

            def send_key(self, key: str) -> None:
                del key

            def close(self) -> None:
                pass

        dependencies = self.gate.GateDependencies(
            validate_artifacts=lambda disk, manifest, pass_fds: {},
            boot_commands=lambda artifacts: (
                self.gate.BootCommand("boot", "booti", b"Starting kernel"),
            ),
            launch_process=lambda argv, pass_fds: object(),
            boot_console=lambda process: Console(),
            monitor=lambda path, timeout: Monitor(),
            cleanup_process=lambda process: (_ for _ in ()).throw(
                RuntimeError("survived")
            ),
            qemu_version=lambda: "QEMU test",
        )
        result = self.gate.run_gate(self.config(), dependencies)
        self.assertFalse(result.passed)
        self.assertEqual(result.cleanup, "cleanup failure")

    def test_failed_start_invalidates_old_evidence(self) -> None:
        self.serial_log.write_text("stale serial")
        self.result.write_text('{"passed": true}')
        dependencies = self.gate.GateDependencies(
            validate_artifacts=lambda disk, manifest, pass_fds: (_ for _ in ()).throw(
                ValueError("bad manifest")
            ),
            boot_commands=lambda artifacts: (),
            launch_process=lambda argv, pass_fds: self.fail("must not launch"),
            boot_console=lambda process: self.fail("must not open console"),
            monitor=lambda path, timeout: self.fail("must not open monitor"),
            cleanup_process=lambda process: self.fail("must not clean absent process"),
            qemu_version=lambda: "QEMU test",
        )
        with self.assertRaisesRegex(ValueError, "bad manifest"):
            self.gate.run_gate(self.config(), dependencies)
        self.assertFalse(self.result.exists())
        self.assertFalse(self.serial_log.exists())

    def test_launch_and_timeout_failures_replace_stale_success_and_cleanup(
        self,
    ) -> None:
        gate = self.gate

        class Monitor:
            def connect(self) -> None:
                pass

            def send_key(self, key: str) -> None:
                del key

            def close(self) -> None:
                pass

        self.result.write_text('{"passed": true}')
        launch_failure = gate.GateDependencies(
            validate_artifacts=lambda disk, manifest, pass_fds: {},
            boot_commands=lambda artifacts: (),
            launch_process=lambda argv, pass_fds: (_ for _ in ()).throw(
                OSError("launch failed")
            ),
            boot_console=lambda process: self.fail("no console after failed launch"),
            monitor=lambda path, timeout: Monitor(),
            cleanup_process=lambda process: self.fail(
                "no process group after failed launch"
            ),
            qemu_version=lambda: "QEMU test",
        )
        launch_result = gate.run_gate(self.config(), launch_failure)
        self.assertFalse(launch_result.passed)
        self.assertNotEqual(json.loads(self.result.read_text()), {"passed": True})

        actions: list[str] = []

        class TimedOutConsole:
            transcript = b"partial current boot"

            def wait_for(self, marker: bytes, timeout: float) -> None:
                del marker, timeout
                raise TimeoutError

            def send_line(self, command: str) -> None:
                del command

            def drain(self, timeout: float) -> None:
                del timeout
                actions.append("drain")

        timeout_dependencies = gate.GateDependencies(
            validate_artifacts=lambda disk, manifest, pass_fds: {},
            boot_commands=lambda artifacts: (),
            launch_process=lambda argv, pass_fds: object(),
            boot_console=lambda process: TimedOutConsole(),
            monitor=lambda path, timeout: Monitor(),
            cleanup_process=lambda process: actions.append("cleanup"),
            qemu_version=lambda: "QEMU test",
        )
        timeout_result = gate.run_gate(self.config(), timeout_dependencies)
        self.assertFalse(timeout_result.passed)
        self.assertEqual(timeout_result.reason, "timeout: U-Boot prompt")
        self.assertEqual(actions, ["cleanup", "drain"])

    def test_hmp_cap_signal_deferral_and_stubborn_group_cleanup(self) -> None:
        gate = self.gate

        class EndlessSocket:
            def settimeout(self, timeout: float) -> None:
                self.timeout = timeout

            def recv(self, count: int) -> bytes:
                del count
                return b"x" * 4096

        monitor = gate.HmpMonitor(self.directory / "unused.sock", 0.2)
        monitor.socket = EndlessSocket()
        with self.assertRaisesRegex(gate.MonitorError, "byte limit"):
            monitor._read_prompt(time.monotonic() + 0.2)

        state = gate.TerminationSignalState((signal.SIGTERM,))
        with state.defer():
            state.first_signal(signal.SIGTERM, object())
            self.assertEqual(state.pending, signal.SIGTERM)
        with self.assertRaises(gate.GateTermination):
            state.raise_if_pending()

        process = subprocess.Popen(
            [
                "python3",
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(30)",
            ],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        original_grace = gate.PROCESS_TERM_GRACE_SECONDS
        gate.PROCESS_TERM_GRACE_SECONDS = 0.05
        try:
            self.assertEqual(process.stdout.readline(), "ready\n")
            gate._cleanup_process(process)
            self.assertIsNotNone(process.returncode)
        finally:
            gate.PROCESS_TERM_GRACE_SECONDS = original_grace
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=1)
            process.stdout.close()


if __name__ == "__main__":
    unittest.main()
