#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from tools.riscv.tests.support.debian_rootfs import *


class DebianRootfsGateProtocolTests(unittest.TestCase):
    NONCE = "0123456789abcdef" * 4
    EXPECTED_PACKAGES = tuple(
        (name, version)
        for name, architecture, version in PACKAGE_ROWS
        if name in GATE_IDENTITY_PACKAGES and architecture == "riscv64"
    )
    MANIFEST_PACKAGES = tuple(
        next(
            (package_name, version)
            for package_name, architecture, version in PACKAGE_ROWS
            if package_name == name and architecture == "riscv64"
        )
        for name in GATE_IDENTITY_PACKAGES
    )

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.uboot = self._regular_file("u-boot", b"u-boot")
        self.boot_disk = self._regular_file("boot.ext4", b"ext4")
        self.root_disk = self._regular_file("debian-root.ext2", b"ext2")
        self.monitor_socket = self.directory / "monitor.sock"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _regular_file(self, name: str, contents: bytes) -> Path:
        path = self.directory / name
        path.write_bytes(contents)
        return path

    def _qemu_argv(self, **overrides: object) -> tuple[str, ...]:
        arguments: dict[str, object] = {
            "uboot": self.uboot,
            "boot_disk": self.boot_disk,
            "root_disk": self.root_disk,
            "monitor_socket": self.monitor_socket,
            "smp": 4,
            "dtb_enabled_cpu_count": 4,
        }
        arguments.update(overrides)
        return qemu_argv(**arguments)

    @staticmethod
    def _command_output(command: ShellCommand, *, boot_number: int) -> str:
        outputs = {
            "architecture": "riscv64",
            "debian-release": "13.6",
            "bash-version": "5.2.37(1)-release",
            "packages": "\r\r\n".join(
                f"{name}\t{version}"
                for name, version in DebianRootfsGateProtocolTests.EXPECTED_PACKAGES
            ),
            "root-filesystem": "ext2/ext3",
            "persistence": DebianRootfsGateProtocolTests.NONCE,
            "second-probe": "boot2-probe-created",
        }
        if command.name == "second-probe" and boot_number != 2:
            raise AssertionError("boot one must not create the second probe")
        return outputs[command.name]

    def _successful_transcript(
        self,
        commands: tuple[ShellCommand, ...],
        *,
        boot_number: int,
        echoed: bool = False,
    ) -> str:
        lines = ["Asterinas boot noise"]
        for command in commands:
            if echoed:
                lines.append(f"root@asterinas:/# {command.payload}")
            lines.extend(
                (
                    command.begin_marker,
                    self._command_output(command, boot_number=boot_number),
                    f"{command.status_prefix}0",
                    command.end_marker,
                )
            )
        return "\r\n".join(lines) + "\r\n"

    def _classify(
        self,
        transcript: str | bytes,
        commands: tuple[ShellCommand, ...],
        *,
        boot_number: int,
    ) -> GateResult:
        return classify_boot(
            transcript,
            commands,
            boot_number=boot_number,
            expected_debian_release="13.6",
            expected_packages=self.MANIFEST_PACKAGES,
            expected_nonce=self.NONCE,
        )

    def test_qemu_argv_is_the_frozen_headless_two_disk_contract(self) -> None:
        argv = self._qemu_argv()

        self.assertEqual(argv[0], "qemu-system-riscv64")
        self.assertEqual(argv[argv.index("-machine") + 1], "virt")
        self.assertEqual(
            argv[argv.index("-cpu") + 1],
            "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        )
        self.assertEqual(argv[argv.index("-m") + 1], "2G")
        self.assertEqual(argv[argv.index("-smp") + 1], "4")
        self.assertEqual(argv[argv.index("-serial") + 1], "stdio")
        self.assertEqual(argv[argv.index("-display") + 1], "none")
        self.assertEqual(argv[argv.index("-nic") + 1], "none")
        self.assertIn("-no-reboot", argv)

        drives = [
            argv[index + 1] for index, value in enumerate(argv) if value == "-drive"
        ]
        devices = [
            argv[index + 1] for index, value in enumerate(argv) if value == "-device"
        ]
        self.assertEqual(
            drives,
            [
                f"if=none,format=raw,file={self.boot_disk},id=bootdisk,readonly=on",
                (
                    f"if=none,format=raw,file={self.root_disk},id=rootdisk,"
                    "cache=directsync"
                ),
            ],
        )
        self.assertEqual(
            devices,
            [
                "virtio-blk-device,drive=bootdisk",
                "virtio-blk-device,drive=rootdisk",
            ],
        )
        forbidden = ("xhci", "usb", "keyboard", "tablet", "mouse", "netdev")
        self.assertFalse(
            any(token in argument.lower() for argument in argv for token in forbidden)
        )

    def test_qemu_argv_rejects_non_four_hart_contracts(self) -> None:
        for override in ({"smp": 1}, {"smp": True}, {"dtb_enabled_cpu_count": 3}):
            with (
                self.subTest(override=override),
                self.assertRaisesRegex(ValueError, "exactly 4"),
            ):
                self._qemu_argv(**override)

    def test_qemu_argv_rejects_unsafe_or_non_regular_inputs(self) -> None:
        comma_path = self._regular_file("boot,unsafe.ext4", b"ext4")
        missing_path = self.directory / "missing.ext2"
        symlink_path = self.directory / "root-link.ext2"
        symlink_path.symlink_to(self.root_disk)
        cases = (
            ("boot_disk", comma_path, "comma"),
            ("root_disk", missing_path, "regular file"),
            ("root_disk", symlink_path, "symbolic link"),
            ("uboot", self.directory, "regular file"),
        )

        for argument, path, message in cases:
            with (
                self.subTest(argument=argument, path=path),
                self.assertRaisesRegex(ValueError, message),
            ):
                self._qemu_argv(**{argument: path})

    def test_protocol_types_are_frozen(self) -> None:
        command = ShellCommand("name", "true", "begin", "end", "status=")
        evidence = BootEvidence(
            boot_number=1,
            architecture="riscv64",
            debian_release="13.6",
            bash_version="5.2",
            packages=self.EXPECTED_PACKAGES,
            root_filesystem="ext2/ext3",
            persistence_nonce=self.NONCE,
            second_probe=None,
        )
        result = GateResult(True, "pass", evidence)

        for instance in (command, evidence, result):
            with (
                self.subTest(instance=instance),
                self.assertRaises(FrozenInstanceError),
            ):
                instance.__setattr__(next(iter(instance.__dataclass_fields__)), None)

    def test_shell_commands_cover_boot_one_identity_write_and_sync(self) -> None:
        commands = shell_commands(boot_number=1, nonce=self.NONCE)

        self.assertEqual(
            tuple(command.name for command in commands),
            (
                "architecture",
                "debian-release",
                "bash-version",
                "packages",
                "root-filesystem",
                "persistence",
            ),
        )
        payload = "\n".join(command.payload for command in commands)
        for required in (
            "uname -m",
            "/etc/debian_version",
            "BASH_VERSION",
            "base-files libc6 bash coreutils util-linux",
            "stat -f",
            "/var/lib/asterinas-debian-m1",
            self.NONCE,
            "sync",
        ):
            self.assertIn(required, payload)
        self.assertIn("> /var/lib/asterinas-debian-m1/persist", payload)

    def test_shell_commands_cover_boot_two_read_probe_and_sync(self) -> None:
        commands = shell_commands(boot_number=2, nonce=self.NONCE)
        payloads = {command.name: command.payload for command in commands}

        self.assertIn(
            "cat /var/lib/asterinas-debian-m1/persist", payloads["persistence"]
        )
        self.assertNotIn(
            "> /var/lib/asterinas-debian-m1/persist", payloads["persistence"]
        )
        self.assertIn("second-probe", payloads["second-probe"])
        self.assertIn("sync", payloads["second-probe"])
        self.assertEqual(
            len({command.begin_marker for command in commands}), len(commands)
        )
        self.assertTrue(
            all(
                len(command.payload.encode()) <= MAX_COMMAND_PAYLOAD_BYTES
                for command in commands
            )
        )

    def test_shell_commands_reject_bad_boot_or_nonce(self) -> None:
        for arguments in (
            {"boot_number": 3, "nonce": self.NONCE},
            {"boot_number": 1, "nonce": "not-shell-safe"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                shell_commands(**arguments)

    def test_classify_boot_accepts_echoed_commands_and_exact_identity(self) -> None:
        for boot_number in (1, 2):
            with self.subTest(boot_number=boot_number):
                commands = shell_commands(boot_number=boot_number, nonce=self.NONCE)
                transcript = self._successful_transcript(
                    commands, boot_number=boot_number, echoed=True
                )

                result = self._classify(transcript, commands, boot_number=boot_number)

                self.assertTrue(result.passed)
                self.assertEqual(result.reason, "pass")
                self.assertEqual(result.evidence.architecture, "riscv64")
                self.assertEqual(result.evidence.packages, self.EXPECTED_PACKAGES)
                self.assertEqual(result.evidence.persistence_nonce, self.NONCE)
                self.assertEqual(
                    result.evidence.second_probe,
                    "boot2-probe-created" if boot_number == 2 else None,
                )

    def test_classify_boot_rejects_stale_duplicate_and_reordered_markers(self) -> None:
        commands = shell_commands(boot_number=1, nonce=self.NONCE)
        good = self._successful_transcript(commands, boot_number=1)
        blocks = good.splitlines()[1:]
        first_block = blocks[:4]
        second_block = blocks[4:8]
        cases = (
            (commands[0].begin_marker + "\n" + good, "duplicate"),
            ("\n".join(second_block + first_block + blocks[8:]), "reordered"),
        )

        for transcript, reason in cases:
            with self.subTest(reason=reason):
                result = self._classify(transcript, commands, boot_number=1)
                self.assertFalse(result.passed)
                self.assertIn(reason, result.reason)

    def test_classify_boot_rejects_nonzero_and_wrong_identity(self) -> None:
        commands = shell_commands(boot_number=1, nonce=self.NONCE)
        good = self._successful_transcript(commands, boot_number=1)
        cases = (
            (
                good.replace(
                    f"{commands[0].status_prefix}0", f"{commands[0].status_prefix}7"
                ),
                "status 7",
            ),
            (good.replace("13.6", "12.0", 1), "Debian release"),
            (good.replace("5.2.37-2+b5", "0.invalid", 1), "package versions"),
        )

        for transcript, reason in cases:
            with self.subTest(reason=reason):
                result = self._classify(transcript, commands, boot_number=1)
                self.assertFalse(result.passed)
                self.assertIn(reason, result.reason)

    def test_classify_boot_scans_complete_transcript_for_fatal_markers(self) -> None:
        commands = shell_commands(boot_number=1, nonce=self.NONCE)
        good = self._successful_transcript(commands, boot_number=1)
        fatal_markers = (
            "Kernel panic - not syncing",
            "reboot: Restarting system",
            "EXT2-fs error (device vdb)",
            "Buffer I/O error on dev vdb",
            "blk_update_request: I/O error",
            "DEBIAN_ROOTFS_FAIL reason=root_mount",
        )

        for marker in fatal_markers:
            with self.subTest(marker=marker):
                result = self._classify(good + marker + "\n", commands, boot_number=1)
                self.assertFalse(result.passed)
                self.assertIn("fatal transcript marker", result.reason)

    def test_classify_boot_rejects_oversized_transcript_and_payload(self) -> None:
        commands = shell_commands(boot_number=1, nonce=self.NONCE)
        oversized_command = replace(
            commands[0], payload="x" * (MAX_COMMAND_PAYLOAD_BYTES + 1)
        )
        cases = (
            (b"x" * (MAX_TRANSCRIPT_BYTES + 1), commands, "transcript exceeds"),
            (b"", (oversized_command, *commands[1:]), "payload exceeds"),
        )

        for transcript, candidate_commands, reason in cases:
            with self.subTest(reason=reason):
                result = self._classify(transcript, candidate_commands, boot_number=1)
                self.assertFalse(result.passed)
                self.assertIn(reason, result.reason)

    def test_classify_boot_rejects_missing_or_mismatched_persistence(self) -> None:
        commands = shell_commands(boot_number=2, nonce=self.NONCE)
        good = self._successful_transcript(commands, boot_number=2)
        cases = (
            (good.replace(self.NONCE, "", 1), "persistence nonce"),
            (good.replace(self.NONCE, "f" * 64, 1), "persistence nonce"),
            (good.replace("boot2-probe-created", "", 1), "second probe"),
        )

        for transcript, reason in cases:
            with self.subTest(reason=reason):
                result = self._classify(transcript, commands, boot_number=2)
                self.assertFalse(result.passed)
                self.assertIn(reason, result.reason)


class DebianRootfsGateRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def test_serial_console_wait_can_start_after_a_transcript_checkpoint(self) -> None:
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        self.addCleanup(os.close, writer)
        console = SerialConsole(reader, max_bytes=1024)
        os.write(writer, b"repeat-marker\n")
        console.wait_for(b"repeat-marker", time.monotonic() + 1.0)
        checkpoint = console.checkpoint()

        delayed = threading.Thread(
            target=lambda: (time.sleep(0.02), os.write(writer, b"repeat-marker\n"))
        )
        delayed.start()
        self.addCleanup(delayed.join)
        transcript = console.wait_for(
            b"repeat-marker", time.monotonic() + 1.0, start=checkpoint
        )

        self.assertEqual(transcript.count(b"repeat-marker"), 2)

    def test_serial_console_observes_admitted_chunks(self) -> None:
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        self.addCleanup(os.close, writer)
        observed: list[bytes] = []
        console = SerialConsole(reader, max_bytes=8, observer=observed.append)
        os.write(writer, b"ready\n")

        console.wait_for(b"ready", time.monotonic() + 1.0)

        self.assertEqual(observed, [b"ready\n"])
        os.write(writer, b"overflow")
        with self.assertRaisesRegex(BufferError, "serial transcript"):
            console.wait_for(b"missing", time.monotonic() + 1.0)
        self.assertEqual(observed, [b"ready\n"])

    def test_serial_console_wait_for_any_returns_the_first_observed_marker(
        self,
    ) -> None:
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        self.addCleanup(os.close, writer)
        console = SerialConsole(reader, max_bytes=1024)
        os.write(writer, b"boot noise\nDEBIAN_DESKTOP_M3_FAIL reason=xorg\n")

        marker = console.wait_for_any(
            (b"DEBIAN_DESKTOP_M3_READY", b"DEBIAN_DESKTOP_M3_FAIL"),
            time.monotonic() + 1.0,
        )

        self.assertEqual(marker, b"DEBIAN_DESKTOP_M3_FAIL")

    @staticmethod
    def _deadline(seconds: float = 1.0) -> float:
        return time.monotonic() + seconds

    def _hmp_server(
        self,
        chunks: tuple[bytes, ...],
        *,
        receive_command: bool = False,
        keep_open: float = 0.0,
    ) -> tuple[Path, threading.Thread, list[bytes]]:
        path = self.directory / f"hmp-{time.monotonic_ns()}.sock"
        ready = threading.Event()
        received: list[bytes] = []

        def serve() -> None:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
                listener.listen(1)
                ready.set()
                connection, _ = listener.accept()
                with connection:
                    if receive_command:
                        connection.sendall(b"QEMU 10.0\r\n(qemu) ")
                        received.append(connection.recv(1024))
                    for chunk in chunks:
                        connection.sendall(chunk)
                        time.sleep(0.005)
                    if keep_open:
                        time.sleep(keep_open)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(1.0))
        return path, thread, received

    def test_serial_console_matches_split_markers_with_one_total_deadline(self) -> None:
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        console = SerialConsole(master, max_bytes=128)

        def write_split_marker() -> None:
            for chunk in (b"boot noi", b"se\r\nROOT_", b"READY\r\n"):
                os.write(slave, chunk)
                time.sleep(0.01)

        writer = threading.Thread(target=write_split_marker)
        writer.start()
        matched = console.wait_for(b"ROOT_READY", self._deadline())
        writer.join()
        console.send(b"echo ready\n", self._deadline())

        self.assertIn(b"ROOT_READY", matched)
        self.assertIn(b"boot noise", console.transcript)
        self.assertEqual(os.read(slave, 128), b"echo ready\n")

    def test_serial_console_can_pace_physical_uart_transmission(self) -> None:
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        self.addCleanup(os.close, writer)
        console = SerialConsole(writer, max_bytes=128, tx_delay=0.005)

        with mock.patch("tools.riscv.debian.rootfs.gate_runtime.time.sleep") as sleep:
            console.send(b"abc", self._deadline(3.0))

        self.assertEqual(os.read(reader, 128), b"abc")
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_has_calls([mock.call(0.005), mock.call(0.005)])

    def test_paced_transmission_drains_concurrent_serial_output(self) -> None:
        local, remote = socket.socketpair()
        descriptor = local.detach()
        self.addCleanup(os.close, descriptor)
        self.addCleanup(remote.close)
        console = SerialConsole(descriptor, max_bytes=128, tx_delay=0.005)
        payload = b"abcdefgh"

        def echo_during_send() -> None:
            remote.recv(1)
            remote.sendall(b"readline-redraw\n")
            remaining = len(payload) - 1
            while remaining:
                remaining -= len(remote.recv(remaining))

        echo = threading.Thread(target=echo_during_send, daemon=True)
        echo.start()
        self.addCleanup(echo.join, 1.0)

        console.send(payload, self._deadline(3.0))
        echo.join(1.0)

        self.assertFalse(echo.is_alive())
        self.assertIn(b"readline-redraw\n", console.transcript)

    def test_serial_console_rejects_paced_command_before_partial_transmission(
        self,
    ) -> None:
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        self.addCleanup(os.close, writer)
        console = SerialConsole(writer, max_bytes=128, tx_delay=0.005)

        with (
            mock.patch(
                "tools.riscv.debian.rootfs.gate_runtime.time.monotonic",
                return_value=10.0,
            ),
            self.assertRaisesRegex(TimeoutError, "before transmission"),
        ):
            console.send(b"abcdef", 10.01)

        readable, _, _ = select.select([reader], [], [], 0)
        self.assertEqual(readable, [])

    def test_serial_console_bounds_prompt_boot_and_drain_deadlines(self) -> None:
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        console = SerialConsole(master, max_bytes=128)

        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "serial marker"):
            console.wait_for(b"never", started + 0.04)
        self.assertLess(time.monotonic() - started, 0.25)

        started = time.monotonic()
        drained = console.drain(started + 0.04)
        self.assertEqual(drained, b"")
        self.assertLess(time.monotonic() - started, 0.25)
        os.close(slave)

        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        console = SerialConsole(write_fd, max_bytes=128)
        with self.assertRaisesRegex(TimeoutError, "serial command"):
            console.send(b"x" * (1024 * 1024), self._deadline(0.04))
        os.close(write_fd)

    def test_serial_console_caps_transcript_and_reports_early_process_exit(
        self,
    ) -> None:
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        process = launch_process(("/bin/sh", "-c", "exit 23"))
        console = SerialConsole(read_fd, process=process, max_bytes=8)
        os.write(write_fd, b"123456789")
        os.close(write_fd)
        with self.assertRaisesRegex(BufferError, "serial transcript"):
            console.wait_for(b"missing", self._deadline())
        process.wait(self._deadline())

        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        os.close(write_fd)
        console = SerialConsole(read_fd, process=process, max_bytes=128)
        with self.assertRaises(EarlyProcessExit) as caught:
            console.wait_for(b"missing", self._deadline())
        self.assertEqual(caught.exception.returncode, 23)

    def test_hmp_connect_and_prompt_deadlines_are_bounded(self) -> None:
        missing = self.directory / "missing.sock"
        started = time.monotonic()
        with self.assertRaises(MonitorError):
            HmpMonitor.connect(missing, started + 0.04, max_response_bytes=64)
        self.assertLess(time.monotonic() - started, 0.25)

        path, thread, _ = self._hmp_server((b"not-a-prompt",), keep_open=0.15)
        started = time.monotonic()
        with self.assertRaisesRegex(MonitorError, "prompt"):
            HmpMonitor.connect(path, started + 0.04, max_response_bytes=64)
        self.assertLess(time.monotonic() - started, 0.25)
        thread.join(1.0)

    def test_hmp_matches_split_prompt_and_caps_each_response(self) -> None:
        path, thread, received = self._hmp_server(
            (b"info sta", b"tus\r\nrunning\r\n(qe", b"mu) "),
            receive_command=True,
        )
        monitor = HmpMonitor.connect(path, self._deadline(), max_response_bytes=128)
        self.addCleanup(monitor.close)
        response = monitor.command("info status", self._deadline())
        thread.join(1.0)
        self.assertIn(b"running", response)
        self.assertEqual(received, [b"info status\n"])

        path, thread, _ = self._hmp_server(
            (b"x" * 65,), receive_command=True, keep_open=0.05
        )
        monitor = HmpMonitor.connect(path, self._deadline(), max_response_bytes=64)
        with self.assertRaisesRegex(MonitorError, "exceeds"):
            monitor.command("query", self._deadline())
        monitor.close()
        thread.join(1.0)

    def test_pinned_output_rejects_symlink_and_survives_parent_swap(self) -> None:
        outside = self.directory / "outside"
        outside.mkdir()
        symlink = self.directory / "output-link"
        symlink.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            PinnedOutputDirectory(symlink)

        output = self.directory / "output"
        output.mkdir()
        moved = self.directory / "moved-output"
        with PinnedOutputDirectory(output) as pinned:
            output.rename(moved)
            output.symlink_to(outside, target_is_directory=True)
            pinned.atomic_write("result.json", b"safe")
        self.assertEqual((moved / "result.json").read_bytes(), b"safe")
        self.assertEqual(list(outside.iterdir()), [])

    def test_pinned_output_invalidates_stale_result_before_validation(self) -> None:
        output = self.directory / "output"
        output.mkdir()
        stale = output / "result.json"
        stale.write_bytes(b"stale pass")

        with PinnedOutputDirectory(output) as pinned:
            pinned.invalidate("result.json")
            with self.assertRaisesRegex(ValueError, "bad input"):
                raise ValueError("bad input")

        self.assertFalse(stale.exists())

    def test_pinned_output_atomic_write_copy_hash_and_directory_fsync(self) -> None:
        output = self.directory / "output"
        output.mkdir()
        source = self.directory / "source"
        source.write_bytes(b"copy contents")
        fsync_modes: list[int] = []
        real_fsync = gate_runtime_module.os.fsync

        def record_fsync(fd: int) -> None:
            fsync_modes.append(os.fstat(fd).st_mode)
            real_fsync(fd)

        with (
            mock.patch.object(gate_runtime_module.os, "fsync", new=record_fsync),
            PinnedOutputDirectory(output) as pinned,
        ):
            pinned.atomic_write("result.json", b"new result")
            pinned.atomic_copy("run-root.ext2", source)
            self.assertEqual(
                pinned.sha256("run-root.ext2"),
                hashlib.sha256(b"copy contents").hexdigest(),
            )

        self.assertEqual((output / "result.json").read_bytes(), b"new result")
        self.assertTrue(any(stat.S_ISDIR(mode) for mode in fsync_modes))
        self.assertTrue(any(stat.S_ISREG(mode) for mode in fsync_modes))
        self.assertFalse(
            any(path.name.startswith(".gate-") for path in output.iterdir())
        )

    def test_launch_starts_new_session_with_unblocked_termination_masks(self) -> None:
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        os.set_inheritable(slave, True)
        passed_path = self.directory / "passed-fd"
        passed_path.write_bytes(b"fd contents")
        passed_fd = os.open(passed_path, os.O_RDONLY | os.O_CLOEXEC)
        self.addCleanup(os.close, passed_fd)
        previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGHUP, signal.SIGTERM}
        )
        self.addCleanup(signal.pthread_sigmask, signal.SIG_SETMASK, previous_mask)
        script = (
            "import os,signal,sys; "
            "m=signal.pthread_sigmask(signal.SIG_BLOCK, []); "
            "stdio_inherited=1; passed_inherited=1; "
            "\ntry: os.fstat(int(sys.argv[1]))\n"
            "except OSError: stdio_inherited=0\n"
            "try: os.fstat(int(sys.argv[2]))\n"
            "except OSError: passed_inherited=0\n"
            "print(os.getpid(),os.getsid(0),int(signal.SIGHUP in m),"
            "int(signal.SIGTERM in m),stdio_inherited,passed_inherited,flush=True)"
        )
        process = launch_process(
            (sys.executable, "-c", script, str(slave), str(passed_fd)),
            stdio_fd=slave,
            pass_fds=(passed_fd,),
        )
        os.close(slave)
        output = SerialConsole(master, process=process, max_bytes=256).drain(
            self._deadline()
        )
        self.assertEqual(process.wait(self._deadline()), 0)
        pid, sid, hup_blocked, term_blocked, stdio_inherited, passed_inherited = (
            output.decode().split()
        )
        self.assertEqual(pid, sid)
        self.assertEqual((hup_blocked, term_blocked), ("0", "0"))
        self.assertEqual((stdio_inherited, passed_inherited), ("0", "1"))

    def test_cleanup_kills_group_after_leader_exit(self) -> None:
        child_pid_file = self.directory / "child.pid"
        script = (
            "import ctypes,os,time\n"
            "p=os.fork()\n"
            "if p:\n"
            f" open({str(child_pid_file)!r},'w').write(str(p))\n"
            " os._exit(0)\n"
            "ctypes.CDLL(None).prctl(15,b'gate child name',0,0,0)\n"
            "time.sleep(30)\n"
        )
        process = launch_process((sys.executable, "-c", script))
        self.assertEqual(process.wait(self._deadline()), 0)
        child_pid = int(child_pid_file.read_text())

        process.terminate_group(self._deadline(0.2), self._deadline(0.5))

        child_stat = Path(f"/proc/{child_pid}/stat")
        if child_stat.exists():
            _, _, remainder = child_stat.read_text(encoding="ascii").rpartition(") ")
            self.assertEqual(remainder.split()[0], "Z")

    def test_cleanup_escalates_term_to_kill_for_stubborn_group(self) -> None:
        script = (
            "import signal,time; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "print('ready',flush=True); time.sleep(30)"
        )
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        process = launch_process((sys.executable, "-c", script), stdio_fd=slave)
        os.close(slave)
        SerialConsole(master, process=process, max_bytes=128).wait_for(
            b"ready", self._deadline()
        )

        process.terminate_group(self._deadline(0.04), self._deadline(0.5))

        self.assertEqual(process.returncode, -signal.SIGKILL)

    def test_teardown_orders_monitor_group_cleanup_and_serial_drain(self) -> None:
        events: list[str] = []

        class RecordingMonitor:
            def close(self) -> None:
                events.append("monitor")

        class RecordingProcess:
            def terminate_group(
                self, term_deadline: float, kill_deadline: float
            ) -> None:
                events.append("process")

        class RecordingSerial:
            def drain(self, deadline: float) -> bytes:
                events.append("serial")
                return b"tail"

        tail = teardown_gate(
            RecordingMonitor(),
            RecordingProcess(),
            RecordingSerial(),
            term_deadline=self._deadline(),
            kill_deadline=self._deadline(),
            drain_deadline=self._deadline(),
        )
        self.assertEqual(events, ["monitor", "process", "serial"])
        self.assertEqual(tail, b"tail")

        events.clear()

        class FailingMonitor:
            def close(self) -> None:
                events.append("monitor")
                raise MonitorError("close failed")

        with self.assertRaisesRegex(MonitorError, "close failed"):
            teardown_gate(
                FailingMonitor(),
                RecordingProcess(),
                RecordingSerial(),
                term_deadline=self._deadline(),
                kill_deadline=self._deadline(),
                drain_deadline=self._deadline(),
            )
        self.assertEqual(events, ["monitor", "process", "serial"])

    def test_first_hup_or_term_is_deferred_through_publication_and_restored(
        self,
    ) -> None:
        for signum in (signal.SIGHUP, signal.SIGTERM):
            with self.subTest(signum=signum):
                previous = signal.getsignal(signum)
                result = self.directory / f"result-{signum}.json"
                with self.assertRaises(GateTermination) as caught:
                    with TerminationSignalState():
                        os.kill(os.getpid(), signum)
                        result.write_text("published", encoding="utf-8")
                self.assertEqual(caught.exception.signum, signum)
                self.assertEqual(result.read_text(encoding="utf-8"), "published")
                self.assertIs(signal.getsignal(signum), previous)

    def test_second_scoped_termination_signal_hard_exits(self) -> None:
        pid = os.fork()
        if pid == 0:
            with TerminationSignalState():
                os.kill(os.getpid(), signal.SIGTERM)
                os.kill(os.getpid(), signal.SIGTERM)
            os._exit(99)
        _, status = os.waitpid(pid, 0)
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(os.WEXITSTATUS(status), 128 + signal.SIGTERM)


class DebianRootfsGateOrchestrationTests(unittest.TestCase):
    NONCE = "0123456789abcdef" * 4

    class Operations:
        def __init__(self, failure: str | None = None) -> None:
            self.failure = failure
            self.events: list[str] = []
            self.publications: list[dict[str, object]] = []

        def _event(self, name: str) -> None:
            self.events.append(name)
            if self.failure == name:
                raise GateFailure(name)

        def invalidate(self, config: GateConfig) -> None:
            del config
            self._event("invalidate")

        def snapshot_inputs(self, config: GateConfig) -> dict[str, str]:
            del config
            self._event("snapshot")
            return {"kernel": "a" * 64, "root_image": "b" * 64}

        def validate_inputs(
            self, config: GateConfig, snapshots: dict[str, str]
        ) -> dict[str, object]:
            del config, snapshots
            self._event("validate")
            return {
                "debian_release": "13.6",
                "packages": (("bash", "5.2"),),
            }

        def prepare(
            self,
            config: GateConfig,
            snapshots: dict[str, str],
            identity: dict[str, object],
        ) -> dict[str, str]:
            del config, snapshots, identity
            self._event("prepare")
            return {"boot_disk": "boot.ext4", "root_disk": "root.ext2"}

        def launch(
            self, config: GateConfig, prepared: dict[str, str], boot_number: int
        ) -> dict[str, object]:
            del config, prepared
            self._event(f"launch{boot_number}")
            return {
                "boot_number": boot_number,
                "argv": ("qemu-system-riscv64", f"boot={boot_number}"),
            }

        def drive_uboot(self, session: dict[str, object], config: GateConfig) -> None:
            del config
            self._event(f"uboot{session['boot_number']}")

        def enter_debian(self, session: dict[str, object], config: GateConfig) -> None:
            del config
            self._event(f"shell{session['boot_number']}")

        def execute_checks(
            self,
            session: dict[str, object],
            config: GateConfig,
            identity: dict[str, object],
            nonce: str,
        ) -> None:
            del config, identity, nonce
            self._event(f"commands{session['boot_number']}")

        def request_quit(self, session: dict[str, object], config: GateConfig) -> None:
            del config
            self._event(f"quit{session['boot_number']}")

        def close_monitor(self, session: dict[str, object]) -> None:
            self._event(f"close{session['boot_number']}")

        def cleanup_process(
            self, session: dict[str, object], config: GateConfig
        ) -> None:
            del config
            self._event(f"cleanup{session['boot_number']}")

        def drain_serial(self, session: dict[str, object], config: GateConfig) -> bytes:
            del config
            boot_number = session["boot_number"]
            self._event(f"drain{boot_number}")
            return f"complete boot {boot_number}\n".encode()

        def hash_final_root(self, config: GateConfig, prepared: dict[str, str]) -> str:
            del config, prepared
            self._event("hash-final-root")
            return "c" * 64

        def publish(
            self,
            config: GateConfig,
            prepared: dict[str, str] | None,
            transcripts: tuple[bytes, bytes],
            result: dict[str, object],
        ) -> None:
            del config, prepared, transcripts
            self._event("publish")
            self.publications.append(copy.deepcopy(result))

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        directory = Path(self.temporary_directory.name)
        inputs = []
        for name in (
            "kernel",
            "u-boot",
            "qemu-virt.dtb",
            "stage1-initramfs.cpio",
            "debian-root.ext2",
            "rootfs-manifest.json",
            "packages.lock",
            "package-checksums",
        ):
            path = directory / name
            path.write_bytes(name.encode())
            inputs.append(path)
        self.config = GateConfig(*inputs, directory / "output")

    def test_configuration_is_frozen_and_has_exact_four_hart_timeouts(self) -> None:
        self.assertEqual(self.config.smp, 4)
        self.assertGreater(self.config.boot_timeout, 0)
        self.assertGreater(self.config.command_timeout, 0)
        self.assertGreater(self.config.cleanup_timeout, 0)
        with self.assertRaises(FrozenInstanceError):
            self.config.smp = 1

    def test_success_uses_exact_two_boot_lifecycle_and_same_prepared_root(self) -> None:
        operations = self.Operations()

        result = orchestrate_gate(self.config, operations, nonce=self.NONCE)

        self.assertEqual(
            operations.events,
            [
                "invalidate",
                "snapshot",
                "validate",
                "prepare",
                "launch1",
                "uboot1",
                "shell1",
                "commands1",
                "quit1",
                "close1",
                "cleanup1",
                "drain1",
                "launch2",
                "uboot2",
                "shell2",
                "commands2",
                "quit2",
                "close2",
                "cleanup2",
                "drain2",
                "hash-final-root",
                "publish",
            ],
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["reason"], "pass")
        self.assertEqual(
            result["nonce_sha256"], hashlib.sha256(self.NONCE.encode()).hexdigest()
        )
        self.assertNotIn(self.NONCE, json.dumps(result))
        self.assertEqual(len(result["qemu_argv"]), 2)
        self.assertEqual(
            result["manifest_identity"],
            {"debian_release": "13.6"},
        )
        self.assertEqual(result["package_identity"], (("bash", "5.2"),))
        self.assertEqual(
            set(result["phase_durations_seconds"]),
            {"snapshot", "validate", "prepare", "boot1", "boot2", "hash-final-root"},
        )
        self.assertTrue(
            all(
                isinstance(duration, float) and duration >= 0
                for duration in result["phase_durations_seconds"].values()
            )
        )

    def test_failures_never_publish_a_passing_result_and_always_drain_launched_boot(
        self,
    ) -> None:
        cases = (
            ("prepare", []),
            ("launch1", []),
            ("uboot1", ["close1", "cleanup1", "drain1"]),
            ("shell1", ["close1", "cleanup1", "drain1"]),
            ("commands1", ["close1", "cleanup1", "drain1"]),
            ("drain1", ["close1", "cleanup1", "drain1"]),
            ("cleanup1", ["close1", "cleanup1", "drain1"]),
            ("hash-final-root", ["close2", "cleanup2", "drain2"]),
        )

        for failure, required_tail in cases:
            with self.subTest(failure=failure):
                operations = self.Operations(failure)
                result = orchestrate_gate(self.config, operations, nonce=self.NONCE)
                self.assertFalse(result["passed"])
                self.assertEqual(result["reason"], failure)
                for event in required_tail:
                    self.assertIn(event, operations.events)
                self.assertFalse(
                    any(
                        publication["passed"] for publication in operations.publications
                    )
                )

    def test_interrupted_publication_cannot_leave_passing_evidence(self) -> None:
        operations = self.Operations("publish")

        with self.assertRaisesRegex(GateFailure, "publish"):
            orchestrate_gate(self.config, operations, nonce=self.NONCE)

        self.assertEqual(operations.events[0], "invalidate")
        self.assertEqual(operations.events[-1], "publish")
        self.assertEqual(operations.publications, [])


class DebianRootfsGateArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def test_real_boot_image_is_64_mib_and_contains_exactly_three_files(self) -> None:
        kernel = self.directory / "kernel"
        dtb = self.directory / "dtb"
        initramfs = self.directory / "initramfs"
        kernel.write_bytes(b"kernel")
        dtb.write_bytes(b"dtb")
        initramfs.write_bytes(b"initramfs")
        image = self.directory / "boot.ext4"

        build_boot_image(kernel, dtb, initramfs, image)

        self.assertEqual(image.stat().st_size, 64 * 1024 * 1024)
        listing = subprocess.run(
            ["debugfs", "-R", "ls -p /", str(image)],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        names = {
            fields[5]
            for line in listing.splitlines()
            if len(fields := line.split("/")) >= 6 and fields[5] not in (".", "..", "")
        }
        self.assertEqual(
            names,
            {"asterinas.booti", "qemu-virt.dtb", "stage1-initramfs.cpio"},
        )
        self.assertNotIn("lost+found", listing)

    def test_sparse_root_copy_uses_descriptor_and_preserves_source_identity(
        self,
    ) -> None:
        source = self.directory / "base.ext2"
        with source.open("wb") as stream:
            stream.write(b"root")
            stream.seek(16 * 1024 * 1024 - 1)
            stream.write(b"\0")
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        self.addCleanup(os.close, descriptor)
        destination = self.directory / "run.ext2"

        before, after, copied = copy_sparse_root(descriptor, destination)

        self.assertEqual(before, after)
        self.assertEqual(copied, before)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertLess(destination.stat().st_blocks * 512, destination.stat().st_size)

        directory_fd = os.open(
            self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        )
        self.addCleanup(os.close, directory_fd)
        pinned_destination = Path(f"/proc/self/fd/{directory_fd}/pinned-run.ext2")
        pinned_before, pinned_after, pinned_copied = copy_sparse_root(
            descriptor,
            pinned_destination,
            destination_directory_fd=directory_fd,
        )
        self.assertEqual(
            (pinned_before, pinned_after, pinned_copied),
            (before, before, before),
        )
        self.assertTrue((self.directory / "pinned-run.ext2").is_file())

    def test_fdtget_accepts_exactly_four_enabled_cpu_nodes(self) -> None:
        source = self.directory / "four.dts"
        dtb = self.directory / "four.dtb"
        cpus = "\n".join(
            f'cpu@{index} {{ device_type = "cpu"; reg = <{index}>; status = "okay"; }};'
            for index in range(4)
        )
        source.write_text(
            "/dts-v1/; / { #address-cells = <1>; #size-cells = <1>; "
            f"cpus {{ #address-cells = <1>; #size-cells = <0>; {cpus} }}; }};",
            encoding="ascii",
        )
        subprocess.run(["dtc", "-I", "dts", "-O", "dtb", "-o", dtb, source], check=True)

        self.assertEqual(verify_four_hart_dtb(dtb), 4)

        source.write_text(
            source.read_text().replace('status = "okay";', 'status = "disabled";', 1),
            encoding="ascii",
        )
        subprocess.run(["dtc", "-I", "dts", "-O", "dtb", "-o", dtb, source], check=True)
        with self.assertRaisesRegex(GateFailure, "exactly 4"):
            verify_four_hart_dtb(dtb)

    def test_cli_requires_all_eight_inputs_and_output_without_build_mode(self) -> None:
        required = (
            "--kernel",
            "--uboot",
            "--dtb",
            "--stage1-initramfs",
            "--root-image",
            "--root-manifest",
            "--packages-lock",
            "--package-checksums",
            "--output-directory",
        )
        arguments = [
            value for option in required for value in (option, f"/{option[2:]}")
        ]

        config = parse_gate_args(arguments)

        self.assertEqual(config.smp, 4)
        self.assertEqual(config.root_fs, "ext2")
        self.assertEqual(
            parse_gate_args([*arguments, "--root-fs", "ext4"]).root_fs,
            "ext4",
        )
        with self.assertRaises(SystemExit):
            parse_gate_args(arguments[:-2])
        for forbidden in ("download", "rebuild", "mirror"):
            self.assertNotIn(forbidden, " ".join(arguments))
        with mock.patch.object(
            gate_backend_module, "main", return_value=7
        ) as backend_main:
            self.assertEqual(rootfs_gate_module.main(arguments), 7)
        backend_main.assert_called_once_with(arguments)

        help_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.riscv.debian.rootfs.rootfs_gate",
                "--help",
            ],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--root-image", help_result.stdout)


class DebianRootfsGateBackendSessionTests(unittest.TestCase):
    def test_validation_uses_manifest_schema_for_package_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            inputs = []
            for index in range(8):
                path = directory / f"input-{index}"
                path.write_bytes(str(index).encode())
                inputs.append(path)
            output = directory / "output"
            output.mkdir()
            config = GateConfig(*inputs, output)
            downloaded_packages = (
                ("firefox-esr", "riscv64", "140.14.0esr-1", "0" * 64, "security"),
            )
            manifest = mock.Mock(
                schema_version=6,
                downloaded_packages=downloaded_packages,
                suite="trixie",
                architecture="riscv64",
                debian_release="13.6",
                root_image_sha256="1" * 64,
                packages_lock_sha256="2" * 64,
                gate_packages=(),
            )
            with (
                mock.patch.object(
                    gate_backend_module, "load_manifest", return_value=manifest
                ),
                mock.patch.object(gate_backend_module, "validate_frozen_root"),
                mock.patch.object(
                    gate_backend_module,
                    "load_package_checksums",
                    return_value=downloaded_packages,
                ) as load_checksums,
                mock.patch.object(
                    gate_backend_module, "verify_four_hart_dtb", return_value=4
                ),
                gate_backend_module.ConcreteOperations(config) as operations,
            ):
                operations.validate_inputs(config, operations.snapshot_inputs(config))

            load_checksums.assert_called_once_with(
                mock.ANY, schema_version=manifest.schema_version
            )

    def test_session_uses_hardlinked_same_root_and_removes_directory_after_drain(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            inputs = []
            for index in range(8):
                path = directory / f"input-{index}"
                path.write_bytes(str(index).encode())
                inputs.append(path)
            output = directory / ("repository-output-" + "x" * 40) / ("y" * 40)
            output.mkdir(parents=True)
            boot = output / "boot.ext4"
            root = output / "debian-root.run.ext2"
            boot.write_bytes(b"boot")
            root.write_bytes(b"root")
            config = GateConfig(*inputs, output)
            process = mock.Mock()
            monitor = mock.Mock()
            serial = mock.Mock(transcript=b"")
            serial.drain.return_value = b""
            with (
                mock.patch.object(
                    gate_backend_module, "launch_process", return_value=process
                ) as launch,
                mock.patch.object(
                    gate_backend_module.HmpMonitor,
                    "connect",
                    side_effect=lambda path, *_args, **_kwargs: (
                        monitor
                        if len(os.fsencode(path)) < 108
                        else (_ for _ in ()).throw(OSError("AF_UNIX path too long"))
                    ),
                ),
                mock.patch.object(
                    gate_backend_module, "SerialConsole", return_value=serial
                ),
                gate_backend_module.ConcreteOperations(config) as operations,
            ):
                session = operations.launch(
                    config, {"boot_disk": boot, "root_disk": root}, 1
                )
                session_directory = session["directory"]
                output_fd = operations._require_output()._operation_fd
                self.assertEqual(session_directory.parent, Path("/tmp"))
                self.assertLess(
                    len(os.fsencode(session_directory / "monitor.sock")), 108
                )
                self.assertFalse((session_directory / root.name).exists())
                launched_argv = launch.call_args.args[0]
                self.assertIn(
                    f"file=/proc/self/fd/{output_fd}/{root.name}",
                    " ".join(launched_argv),
                )
                self.assertEqual(launch.call_args.kwargs["pass_fds"], (output_fd,))
                operations.close_monitor(session)
                operations.cleanup_process(session, config)
                self.assertEqual(operations.drain_serial(session, config), b"")
                self.assertFalse(session_directory.exists())

    def test_shell_wait_marker_is_not_present_in_echoed_command_payload(self) -> None:
        class EchoingSerial:
            def __init__(self) -> None:
                self.payload = b""

            def send(self, payload: bytes, deadline: float) -> None:
                del deadline
                self.payload = payload

            def wait_for(self, marker: bytes, deadline: float) -> bytes:
                del deadline
                if marker in self.payload:
                    raise AssertionError("echoed payload matched completion marker")
                return self.payload + marker + b"\r\n"

        operations = object.__new__(gate_backend_module.ConcreteOperations)
        session = {"boot_number": 1, "serial": EchoingSerial()}
        operations.execute_checks(
            session,
            mock.Mock(command_timeout=1.0),
            {"debian_release": "13.6", "packages": ()},
            "0123456789abcdef" * 4,
        )
        self.assertIn("commands", session)


if __name__ == "__main__":
    unittest.main()
