#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from tools.riscv.tests.support.megrez_debug import *


class MegrezDebugBoardTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        addresses = {
            "kernel": 0x80200000,
            "initramfs": 0x83000000,
            "qemu_dtb": 0xF0000000,
            "megrez_dtb": 0xF0000000,
        }
        artifacts = []
        for name in ("kernel", "initramfs", "qemu_dtb", "megrez_dtb"):
            path = self.directory / name
            path.write_bytes(f"{name}-payload".encode())
            artifacts.append(ArtifactIdentity.from_path(name, path, addresses[name]))
        self.plan = DebugPlan(
            schema_version=1,
            profile="tcp-probe",
            artifacts=tuple(artifacts),
            bootargs="loglevel=info init=/init asterinas.reboot_after=180",
            smp=4,
            sv39=True,
            markers=("Enter riscv_boot", "ASTERINAS_GMAC_TCP_PROBE_READY"),
            reboot_after=180,
        )

    def test_all_ram_cache_hits_skip_every_xmodem_transfer(self) -> None:
        commands: list[tuple[str, float]] = []
        transfers: list[tuple[int, bytes, int]] = []
        identities = {item.name: item for item in self.plan.artifacts}
        by_address = {
            identities[name].load_address: identities[name]
            for name in ("kernel", "initramfs", "megrez_dtb")
        }

        def command(text: str, timeout: float) -> str:
            commands.append((text, timeout))
            address = int(text.split()[1], 16)
            identity = by_address[address]
            return f"{text}\r\nCRC32 for 0x{address:x} ... ==> {identity.crc32}\r\n=> "

        transport = BoardTransport(
            fd=7,
            command=command,
            transfer_payload=lambda fd, payload, address: transfers.append(
                (fd, payload, address)
            ),
        )

        outcomes = ensure_board_artifacts(self.plan, transport, timeout=2.5)

        self.assertEqual(tuple(item.status for item in outcomes), ("cache-hit",) * 3)
        self.assertEqual(transfers, [])
        self.assertEqual(len(commands), 3)
        for name, (text, timeout) in zip(
            ("kernel", "initramfs", "megrez_dtb"), commands
        ):
            identity = identities[name]
            self.assertEqual(
                text,
                f"crc32 0x{identity.load_address:x} 0x{identity.size:x}",
            )
            self.assertEqual(timeout, 2.5)

    def test_cache_miss_transfers_once_then_requires_matching_crc(self) -> None:
        identity = next(item for item in self.plan.artifacts if item.name == "kernel")
        resident_crc = "00000000"
        transfers: list[tuple[int, bytes, int]] = []
        commands: list[str] = []

        def command(text: str, _timeout: float) -> str:
            nonlocal resident_crc
            commands.append(text)
            if text.startswith("unzip "):
                resident_crc = identity.crc32
                return f"{text}\r\nUncompressed size: {identity.size}\r\n=> "
            return (
                f"{text}\r\nCRC32 for 0x{identity.load_address:x} ... "
                f"==> {resident_crc}\r\n=> "
            )

        def transfer(fd: int, payload: bytes, address: int) -> None:
            transfers.append((fd, payload, address))

        outcome = BoardTransport(
            fd=9, command=command, transfer_payload=transfer
        ).ensure(identity, timeout=3.0)

        self.assertEqual(outcome.status, "transferred-compressed")
        self.assertEqual(len(transfers), 1)
        fd, payload, address = transfers[0]
        self.assertEqual((fd, address), (9, KERNEL_COMPRESSED_ADDRESS))
        self.assertEqual(gzip.decompress(payload), Path(identity.path).read_bytes())
        self.assertEqual(
            commands[1],
            f"unzip 0x{KERNEL_COMPRESSED_ADDRESS:x} "
            f"0x{identity.load_address:x} 0x{identity.size:x}",
        )

    def test_malformed_or_still_mismatched_crc_fails_closed(self) -> None:
        identity = next(item for item in self.plan.artifacts if item.name == "kernel")

        for output, message in (
            ("=> ", "crc-result"),
            (
                f"CRC32 for 0x{identity.load_address:x} ... ==> 00000000\r\n=> ",
                "post-transfer-crc",
            ),
        ):
            with self.subTest(message=message):
                transport = BoardTransport(
                    fd=11,
                    command=lambda _text, _timeout, value=output: value,
                    transfer_payload=lambda _fd, _payload, _address: None,
                )
                with self.assertRaisesRegex(BoardTransportError, message):
                    transport.ensure(identity, timeout=1.0)


class MegrezDebugBoardStateTests(unittest.TestCase):
    class Operations:
        def __init__(self, chunks: list[str | BaseException]) -> None:
            self.chunks = list(chunks)
            self.calls: list[tuple[str, float | None]] = []
            self.booti_count = 0
            self.published: StageResult | None = None
            self.outcomes = (
                "kernel:cache-hit",
                "initramfs:cache-hit",
                "megrez_dtb:cache-hit",
            )

        def invalidate(self) -> None:
            self.calls.append(("invalidate", None))

        def open(self, timeout: float) -> None:
            self.calls.append(("open", timeout))

        def ensure_artifacts(self, _plan: DebugPlan, timeout: float) -> tuple[str, ...]:
            self.calls.append(("ensure", timeout))
            return self.outcomes

        def prepare_boot(self, _plan: DebugPlan, timeout: float) -> None:
            self.calls.append(("prepare", timeout))

        def booti(self, _plan: DebugPlan, timeout: float) -> None:
            self.calls.append(("booti", timeout))
            self.booti_count += 1

        def read_chunk(self, timeout: float) -> str:
            self.calls.append(("read", timeout))
            if not self.chunks:
                raise TimeoutError("no more serial data")
            value = self.chunks.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value

        def stop_recovery_autoboot(self, timeout: float) -> None:
            self.calls.append(("stop-autoboot", timeout))

        def close(self) -> None:
            self.calls.append(("close", None))

        def evidence_names(self) -> tuple[str, ...]:
            return ("serial.log", "transport.json")

        def publish(
            self,
            result: StageResult,
            _transcript: str,
            _outcomes: tuple[str, ...],
        ) -> None:
            self.calls.append(("publish", None))
            self.published = result

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        directory = Path(self.temporary_directory.name)
        self.directory = directory
        addresses = {
            "kernel": 0x80200000,
            "initramfs": 0x83000000,
            "qemu_dtb": 0xF0000000,
            "megrez_dtb": 0xF0000000,
        }
        artifacts = []
        for name in ("kernel", "initramfs", "qemu_dtb", "megrez_dtb"):
            path = directory / name
            path.write_bytes(name.encode())
            artifacts.append(ArtifactIdentity.from_path(name, path, addresses[name]))
        self.plan = DebugPlan(
            schema_version=1,
            profile="tcp-probe",
            artifacts=tuple(artifacts),
            bootargs="loglevel=info init=/init asterinas.reboot_after=180",
            smp=4,
            sv39=True,
            markers=("Enter riscv_boot", "ASTERINAS_GMAC_TCP_PROBE_READY"),
            reboot_after=180,
        )

    @staticmethod
    def _clock():
        value = 100.0

        def monotonic() -> float:
            nonlocal value
            current = value
            value += 1.0
            return current

        return monotonic

    def _browser_plan(self) -> DebugPlan:
        addresses = {
            "kernel": 0x80200000,
            "initramfs": 0x83000000,
            "qemu_dtb": 0xF0000000,
            "megrez_dtb": 0xF0000000,
        }
        artifacts = tuple(
            ArtifactIdentity(
                name=name,
                path=str((self.directory / name).absolute()),
                load_address=addresses.get(name, 0),
                size=ROOT_IMAGE_BYTES if name == "root_image" else 4096,
                sha256=hashlib.sha256(name.encode()).hexdigest(),
                crc32=f"{zlib.crc32(name.encode()):08x}",
            )
            for name in DEBIAN_BROWSER_ARTIFACT_ORDER
        )
        return DebugPlan(
            schema_version=2,
            profile="debian-browser",
            artifacts=artifacts,
            bootargs=physical_bootargs(600, target=GateTarget.FIREFOX, network_mode=NetworkMode.PROXY),
            smp=4,
            sv39=True,
            markers=DEBIAN_BROWSER_MARKERS,
            reboot_after=600,
        )

    def test_success_resets_guest_budget_after_transport(self) -> None:
        operations = self.Operations(
            [
                "old text Enter ris",
                "cv_boot\nASTERINAS_GMAC_TCP_PROBE_READY\n",
                "U-Boot recovered\n=> ",
            ]
        )

        result = run_board(
            self.plan,
            BoardRunConfig(timeout=300.0),
            operations,
            clock=self._clock(),
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.reason, "board-pass")
        self.assertEqual(result.plan_sha256, self.plan.plan_sha256)
        self.assertEqual(operations.booti_count, 1)
        self.assertEqual(operations.published, result)
        self.assertEqual(operations.calls[-2:], [("close", None), ("publish", None)])
        transport_budgets = [
            value
            for name, value in operations.calls
            if name in ("open", "ensure", "prepare", "booti")
        ]
        guest_budgets = [value for name, value in operations.calls if name == "read"]
        self.assertTrue(all(value is not None for value in transport_budgets))
        self.assertTrue(all(value is not None for value in guest_budgets))
        self.assertEqual(transport_budgets, sorted(transport_budgets, reverse=True))
        self.assertEqual(guest_budgets, sorted(guest_budgets, reverse=True))
        self.assertGreater(guest_budgets[0], transport_budgets[-1])

    def test_guest_failure_waits_for_a_fresh_uboot_recovery(self) -> None:
        operations = self.Operations(
            [
                "Enter riscv_boot\nASTERINAS_GMAC_TCP_PROBE_FAIL rea",
                "son=receive-poll errno=110 attempts=1 current_bytes=14600 ",
                "completed_bytes=0\npll config ok\nFirmware version:1.4\n",
                "U-Boot 2020.01\n=> ",
            ]
        )

        result = run_board(
            self.plan,
            BoardRunConfig(timeout=120.0),
            operations,
            clock=self._clock(),
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "guest-failure-recovered:receive-poll")
        self.assertEqual(operations.booti_count, 1)
        self.assertEqual(operations.published, result)

    def test_post_terminal_autoboot_countdown_is_stopped_once(self) -> None:
        operations = self.Operations(
            [
                "Hit any key to stop autoboot:  1\nEnter riscv_boot\n",
                "ASTERINAS_GMAC_TCP_PROBE_READY\npll config ok\n",
                "Hit any key to stop auto",
                "boot:  29\n",
                "U-Boot 2024.01\n=> ",
            ]
        )

        result = run_board(
            self.plan,
            BoardRunConfig(timeout=120.0),
            operations,
            clock=self._clock(),
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.reason, "board-pass")
        self.assertEqual(operations.booti_count, 1)
        stop_calls = [call for call in operations.calls if call[0] == "stop-autoboot"]
        self.assertEqual(len(stop_calls), 1)

    def test_terminal_requires_post_terminal_recovery_and_is_unique(self) -> None:
        failure = (
            "ASTERINAS_GMAC_TCP_PROBE_FAIL reason=receive-poll errno=110 "
            "attempts=1 current_bytes=14600 completed_bytes=0\n"
        )
        cases = (
            (
                ["=> \nEnter riscv_boot\n" + failure],
                "recovery-not-observed",
            ),
            (
                ["Enter riscv_boot\n" + failure + failure],
                "guest-terminal-duplicate",
            ),
            (
                [
                    "Enter riscv_boot\nASTERINAS_GMAC_TCP_PROBE_READY\n",
                    "ASTERINAS_GMAC_TCP_PROBE_READY\n=> ",
                ],
                "guest-terminal-duplicate",
            ),
        )
        for chunks, reason in cases:
            with self.subTest(reason=reason):
                operations = self.Operations(chunks)
                result = run_board(
                    self.plan,
                    BoardRunConfig(timeout=120.0),
                    operations,
                    clock=lambda: 10.0,
                )
                self.assertFalse(result.passed)
                self.assertEqual(result.reason, reason)
                self.assertEqual(operations.booti_count, 1)

    def test_marker_and_timeout_failures_never_retry_booti(self) -> None:
        cases = (
            (["ASTERINAS_GMAC_TCP_PROBE_READY\n"], "guest-marker-order"),
            (["MEGREZ_SDHCI_READ_FAIL reason=target-open\n"], "kernel-fatal"),
            (
                ["Enter riscv_boot\nU-Boot 2024.01\n=> "],
                "guest-reboot-before-terminal",
            ),
            ([], "kernel-timeout"),
            (["Enter riscv_boot\n"], "guest-timeout"),
            (
                ["Enter riscv_boot\nASTERINAS_GMAC_TCP_PROBE_READY\n"],
                "recovery-not-observed",
            ),
        )
        for chunks, reason in cases:
            with self.subTest(reason=reason):
                operations = self.Operations(chunks)
                result = run_board(
                    self.plan,
                    BoardRunConfig(timeout=300.0),
                    operations,
                    clock=lambda: 10.0,
                )
                self.assertFalse(result.passed)
                self.assertEqual(result.reason, reason)
                self.assertEqual(operations.booti_count, 1)
                self.assertEqual(operations.published, result)

    def test_first_termination_defers_through_close_and_publication(self) -> None:
        operations = self.Operations([BoardTermination(15)])

        with self.assertRaisesRegex(BoardTermination, "15"):
            run_board(
                self.plan,
                BoardRunConfig(timeout=300.0),
                operations,
                clock=lambda: 20.0,
            )

        self.assertEqual(operations.booti_count, 1)
        assert operations.published is not None
        self.assertFalse(operations.published.passed)
        self.assertEqual(operations.published.reason, "board-terminated-15")
        self.assertEqual(operations.calls[-2:], [("close", None), ("publish", None)])

    def test_termination_during_close_still_publishes_false(self) -> None:
        class TerminatingClose(self.Operations):
            def close(self) -> None:
                self.calls.append(("close", None))
                raise BoardTermination(15)

        operations = TerminatingClose(
            [
                "Enter riscv_boot\nASTERINAS_GMAC_TCP_PROBE_READY\n",
                "U-Boot recovered\n=> ",
            ]
        )
        with self.assertRaisesRegex(BoardTermination, "15"):
            run_board(
                self.plan,
                BoardRunConfig(timeout=300.0),
                operations,
                clock=lambda: 20.0,
            )

        assert operations.published is not None
        self.assertFalse(operations.published.passed)
        self.assertEqual(operations.published.reason, "board-terminated-15")

    def test_termination_during_publication_retries_false_evidence(self) -> None:
        class TerminatingPublish(self.Operations):
            def __init__(self, chunks: list[str | BaseException]) -> None:
                super().__init__(chunks)
                self.publish_count = 0

            def publish(
                self,
                result: StageResult,
                transcript: str,
                outcomes: tuple[str, ...],
            ) -> None:
                self.publish_count += 1
                if self.publish_count == 1:
                    raise BoardTermination(15)
                super().publish(result, transcript, outcomes)

        operations = TerminatingPublish(
            [
                "Enter riscv_boot\nASTERINAS_GMAC_TCP_PROBE_READY\n",
                "U-Boot recovered\n=> ",
            ]
        )
        with self.assertRaisesRegex(BoardTermination, "15"):
            run_board(
                self.plan,
                BoardRunConfig(timeout=300.0),
                operations,
                clock=lambda: 20.0,
            )

        self.assertEqual(operations.publish_count, 2)
        assert operations.published is not None
        self.assertFalse(operations.published.passed)
        self.assertEqual(operations.published.reason, "board-terminated-15")

    def test_absolute_deadline_expires_without_starting_an_extra_read(self) -> None:
        operations = self.Operations(["unreachable"])
        times = iter((0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 301.0))

        result = run_board(
            self.plan,
            BoardRunConfig(timeout=300.0),
            operations,
            clock=lambda: next(times),
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "kernel-timeout")
        self.assertFalse(any(name == "read" for name, _value in operations.calls))

    def test_configuration_and_preboot_failure_forbid_booti(self) -> None:
        try:
            desktop_config = BoardRunConfig(timeout=900.0)
        except ValueError as error:
            self.fail(f"900-second desktop recovery budget was rejected: {error}")
        self.assertEqual(desktop_config.timeout, 900.0)
        with self.assertRaisesRegex(ValueError, "900"):
            BoardRunConfig(timeout=901.0)

        class FailingOperations(self.Operations):
            def prepare_boot(self, _plan: DebugPlan, timeout: float) -> None:
                self.calls.append(("prepare", timeout))
                raise BoardRunFailure("uboot-prepare-failed")

        operations = FailingOperations([])
        result = run_board(
            self.plan,
            BoardRunConfig(timeout=300.0),
            operations,
            clock=lambda: 30.0,
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "uboot-prepare-failed")
        self.assertEqual(operations.booti_count, 0)


class MegrezDebugRealBoardOperationsTests(unittest.TestCase):
    def test_uboot_staging_rejects_writable_boot_without_sync_recovery(self) -> None:
        bootargs = (
            "init=/init asterinas.mmc_write_partition2 "
            "-- --root-init=systemd"
        )
        with self.assertRaises(BoardRunFailure):
            board_module._uboot_bootargs_commands(bootargs)

    def test_long_bootargs_are_staged_below_the_uboot_line_limit(self) -> None:
        bootargs = physical_bootargs(180, target=GateTarget.FIREFOX, network_mode=NetworkMode.PROXY).replace(
            " -- ",
            " systemd.setenv=ASTERINAS_DESKTOP_M4_TIMEOUT_SECONDS=60 -- ",
        )

        commands = board_module._uboot_bootargs_commands(bootargs)

        self.assertTrue(all(len(command.encode()) <= 512 for command in commands))
        staged = [
            command.split('"', 2)[1]
            for command in commands
            if command.startswith("setenv asterinas_bootargs_") and '"' in command
        ]
        self.assertEqual(" ".join(staged), bootargs)
        self.assertTrue(
            any(command.startswith("setenv bootargs ") for command in commands)
        )
        self.assertNotIn('fdt set /chosen bootargs "${bootargs}"', commands)
        self.assertFalse(any("saveenv" in command for command in commands))

    def test_hardware_watchdog_refuses_unknown_component_without_arming(self) -> None:
        commands: list[str] = []

        class Session:
            def command(self, command: str, timeout: float) -> str:
                del timeout
                commands.append(command)
                values = {
                    "md.l 0x51828200 1": "51828200: fe3fff83",
                    "md.l 0x51828444 1": "51828444: 00000001",
                    "md.l 0x508000fc 1": "508000fc: 00000000",
                }
                return f"{command}\r\r\n{values[command]}\r\r\n=> "

        with self.assertRaisesRegex(BoardRunFailure, "watchdog-type-mismatch"):
            RealBoardOperations._arm_hardware_watchdog(
                Session(), time.monotonic() + 1.0
            )

        self.assertEqual(
            commands,
            [
                "md.l 0x51828200 1",
                "md.l 0x51828444 1",
                "md.l 0x51828200 1",
                "md.l 0x51828444 1",
                "md.l 0x508000fc 1",
            ],
        )
        self.assertFalse(any(command.startswith("mw.l 0x5080") for command in commands))

    def test_real_adapter_reuses_one_fd_and_publishes_result_last(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            output = repository / "target/megrez-debug/board"
            artifact_directory = repository / "artifacts"
            artifact_directory.mkdir()
            addresses = {
                "kernel": 0x80200000,
                "initramfs": 0x83000000,
                "qemu_dtb": 0xF0000000,
                "megrez_dtb": 0xF0000000,
            }
            artifacts = []
            for name in ("kernel", "initramfs", "qemu_dtb", "megrez_dtb"):
                path = artifact_directory / name
                path.write_bytes(name.encode())
                artifacts.append(
                    ArtifactIdentity.from_path(name, path, addresses[name])
                )
            plan = DebugPlan(
                schema_version=1,
                profile="tcp-probe",
                artifacts=tuple(artifacts),
                bootargs="loglevel=info init=/init asterinas.reboot_after=180",
                smp=4,
                sv39=True,
                markers=("Enter riscv_boot", "ASTERINAS_GMAC_TCP_PROBE_READY"),
                reboot_after=180,
            )
            identities = {item.load_address: item for item in artifacts}
            identities[0xF0000000] = next(
                item for item in artifacts if item.name == "megrez_dtb"
            )
            commands: list[str] = []
            sends: list[str] = []
            closed: list[int] = []

            class Session:
                fd = 23
                reset_deasserted = False

                def send(self, command: str) -> None:
                    sends.append(command)

                def wait_for_uboot_prompt(self, timeout: float) -> str:
                    self.log.write(f"prompt timeout={timeout}\n")
                    return "U-Boot\n=> "

                def command(self, command: str, timeout: float) -> str:
                    commands.append(command)
                    self.log.write(f"{command}\n")
                    if command == "md.l 0x51828200 1":
                        return (
                            f"{command}\r\r\n"
                            "51828200: fe3fff83                             ..?."
                            "\r\r\n=> "
                        )
                    if command == "md.l 0x51828444 1":
                        reset = 1 if self.reset_deasserted else 0
                        return f"{command}\r\r\n51828444: {reset:08x}\r\r\n=> "
                    if command == "mw.l 0x51828444 0x1":
                        self.reset_deasserted = True
                        return f"{command}\r\n=> "
                    if command == "md.l 0x508000fc 1":
                        return f"{command}\r\r\n508000fc: 44570120\r\r\n=> "
                    if command == "md.l 0x50800000 2":
                        return f"{command}\r\r\n50800000: 0000001f 0000000f\r\r\n=> "
                    if command.startswith("crc32 "):
                        address = int(command.split()[1], 16)
                        return (
                            f"{command}\r\nCRC32 for 0x{address:x} ... "
                            f"==> {identities[address].crc32}\r\n=> "
                        )
                    return f"{command}\r\n=> "

            def session_factory(
                fd: int,
                _log_path: str | None,
                *,
                confirm: bool,
                final_marker: str,
                log_stream,
            ):
                self.assertEqual(fd, 23)
                self.assertFalse(confirm)
                self.assertEqual(final_marker, plan.markers[-1])
                session = Session()
                session.log = log_stream
                return session

            operations = RealBoardOperations(
                plan,
                "/dev/fake",
                output,
                repository_root=repository,
                open_device=lambda _device: 23,
                lock_device=lambda _fd: None,
                close_device=closed.append,
                session_factory=session_factory,
                hardware_watchdog=True,
                probe_trace_provider=lambda plan_sha256: (
                    json.dumps(
                        {
                            "schema_version": 1,
                            "plan_sha256": plan_sha256,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode(),
            )
            operations.invalidate()
            operations.open(10.0)
            outcomes = operations.ensure_artifacts(plan, 10.0)
            operations.prepare_boot(plan, 10.0)
            operations.booti(plan, 10.0)
            operations.stop_recovery_autoboot(10.0)
            operations.close()
            result = StageResult(
                schema_version=1,
                stage="board",
                passed=True,
                reason="board-pass",
                plan_sha256=plan.plan_sha256,
                evidence=(
                    "serial.log",
                    "transport.json",
                    "probe-tcp-info.json",
                ),
            )
            operations.publish(result, "post-boot\n", outcomes)
            operations.finish()

            self.assertEqual(closed, [23])
            self.assertEqual(sends.count(""), 2)
            initramfs = next(
                item for item in plan.artifacts if item.name == "initramfs"
            )
            self.assertEqual(
                sends.count(
                    f"booti 0x80200000 0x83000000:0x{initramfs.size:x} 0xf0000000"
                ),
                1,
            )
            self.assertFalse(any("saveenv" in command for command in commands))
            mmc_device = commands.index("mmc dev 1")
            self.assertEqual(
                commands[mmc_device : mmc_device + 2], ["mmc dev 1", "mmc rescan"]
            )
            self.assertLess(mmc_device, commands.index("fdt addr 0xf0000000"))
            self.assertTrue(
                any(command.startswith("fdt addr ") for command in commands)
            )
            self.assertTrue(
                any("asterinas,usb-host" in command for command in commands)
            )
            self.assertEqual(
                commands[-11:],
                [
                    MEGREZ_USB_HOST_COMMAND,
                    "md.l 0x51828200 1",
                    "md.l 0x51828444 1",
                    "mw.l 0x51828444 0x1",
                    "md.l 0x51828200 1",
                    "md.l 0x51828444 1",
                    "md.l 0x508000fc 1",
                    "mw.l 0x50800004 0xf",
                    "mw.l 0x5080000c 0x76",
                    "mw.l 0x50800000 0x1f",
                    "md.l 0x50800000 2",
                ],
            )
            self.assertTrue((output / "serial.log").is_file())
            self.assertTrue((output / "transport.json").is_file())
            trace = json.loads((output / "probe-tcp-info.json").read_bytes())
            self.assertEqual(trace["plan_sha256"], plan.plan_sha256)
            self.assertEqual(
                StageResult.from_bytes((output / "result.json").read_bytes()),
                result,
            )



if __name__ == "__main__":
    unittest.main()
