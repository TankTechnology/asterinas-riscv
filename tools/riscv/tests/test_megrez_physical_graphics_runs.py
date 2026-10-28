#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from tools.riscv.tests.support.megrez_physical_graphics import *


class PhysicalLifecycleTests(unittest.TestCase):
    NONCES = NONCES
    PLAN_SHA256 = "a" * 64
    PLAN_BOOTARGS = (
        "console=ttyS0 console=tty0 loglevel=info init=/init "
        "asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1 "
        "asterinas.mmc_write_partition2 asterinas.reboot_after=600 "
        "-- --root-init=systemd"
    )

    class Operations:
        def __init__(
            self,
            gate,
            *,
            recover: bool = True,
            late_fatal: bool = False,
            fail_cycle: int | None = None,
            fail_final: bool = False,
            fail_boot_after_start: bool = False,
            cycle_error: BaseException | None = None,
            fail_hdmi_pixels: bool = False,
        ):
            self.gate = gate
            self.recover = recover
            self.late_fatal = late_fatal
            self.fail_cycle = fail_cycle
            self.fail_final = fail_final
            self.fail_boot_after_start = fail_boot_after_start
            self.cycle_error = cycle_error
            self.fail_hdmi_pixels = fail_hdmi_pixels
            self._guest_started = False
            self.events: list[str] = []
            self._transcript: list[str] = ["boot noise"]
            self.published = None

        @property
        def transcript(self) -> str:
            return "\n".join(self._transcript) + "\n"

        @property
        def guest_started(self) -> bool:
            return self._guest_started

        def invalidate(self) -> None:
            self.events.append("invalidate")
            self._guest_started = False

        def open(self, _timeout: float) -> None:
            self.events.append("open")

        def ensure_artifacts(self, _plan, _timeout: float) -> tuple[str, ...]:
            self.events.append("ensure-artifacts")
            return ("kernel:cache-hit", "initramfs:cache-hit", "megrez_dtb:cache-hit")

        def boot(self, _plan, bootargs: str, _timeout: float) -> None:
            self.events.append("boot")
            if "--debug-console=isolated-root" not in bootargs:
                raise AssertionError("physical boot omitted the isolated debug console")
            self._guest_started = True
            if self.fail_boot_after_start:
                raise self.gate.HostGateError("post-boot setup failed")

        def prove_graphical_readiness(self, _timeout: float):
            self.events.append("readiness")
            return self.gate.GraphicalReadinessEvidence(
                browser_pid=41,
                input_nodes=2,
                framebuffer=True,
                xorg_fbdev=True,
                openbox=True,
                firefox=True,
                browser_service="active",
                browser_restarts=0,
                usb_inputs=2,
                usb_keyboard=True,
                usb_mouse=True,
                keyboard_node="event1",
                mouse_node="event0",
                xorg_keyboard=True,
                xorg_mouse=True,
            )

        def run_cycle(self, cycle: int, nonce: str, _timeout: float) -> bytes:
            self.events.append(f"cycle-{cycle}")
            if cycle == self.fail_cycle:
                if self.cycle_error is not None:
                    raise self.cycle_error
                raise self.gate.HostGateError(f"cycle {cycle} injected failure")
            payload = png_payload(value=0x30 + cycle)
            nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
            event_hash = hashlib.sha256(f"events-{cycle}".encode()).hexdigest()
            screenshot_hash = hashlib.sha256(payload).hexdigest()
            self._transcript.extend(
                (
                    f"ASTERINAS_PHYSICAL_GRAPHICS_READY cycle={cycle} nonce_sha256={nonce_hash}",
                    f"ASTERINAS_PHYSICAL_GRAPHICS_KEY_READY cycle={cycle} nonce_sha256={nonce_hash}",
                    f"ASTERINAS_PHYSICAL_GRAPHICS_POINTER_READY cycle={cycle}",
                    f"ASTERINAS_PHYSICAL_GRAPHICS_INPUT cycle={cycle} key_downs=4 relative_events=2 absolute_events=0 left_down=1 left_up=1 digest={event_hash}",
                    f"ASTERINAS_PHYSICAL_GRAPHICS_DOM cycle={cycle} nonce_sha256={nonce_hash} trusted_key=1 trusted_input=1 trusted_pointer=1 trusted_click=1 click_count=1 color=cyan",
                    f"ASTERINAS_PHYSICAL_GRAPHICS_LATENCY cycle={cycle} count=4 min_ms=1.000 p50_ms=2.000 p95_ms=4.000 max_ms=4.000",
                    f"ASTERINAS_PHYSICAL_GRAPHICS_SCREENSHOT cycle={cycle} sha256={screenshot_hash}",
                    f"ASTERINAS_PHYSICAL_GRAPHICS_PASS cycle={cycle}",
                )
            )
            return payload

        def retain_hdmi(self, _timeout: float):
            self.events.append("hdmi")
            payload = b"\x89PNG\r\n\x1a\nhdmi"
            return self.gate.FileEvidence(
                path=Path("/retained/hdmi-evidence.png"),
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                format="png",
            )

        def verify_hdmi_pixels(self, _screenshot: bytes, _hdmi) -> None:
            self.events.append("hdmi-pixels")
            if self.fail_hdmi_pixels:
                raise self.gate.HostGateError("HDMI pixels do not match")

        def retain_operator_display(self, nonce: str, _timeout: float):
            self.events.append("operator-display")
            return self.gate.OperatorDisplayEvidence(
                kind="operator-attested",
                nonce_sha256=hashlib.sha256(nonce.encode()).hexdigest(),
                state="cyan-final-cycle-pass",
                confirmed=True,
            )

        def prove_final_state(
            self, cycle: int, nonce: str, readiness, _timeout: float
        ) -> None:
            self.events.append("final-state")
            if (
                nonce != PhysicalLifecycleTests.NONCES[cycle - 1]
                or readiness.browser_pid != 41
            ):
                raise AssertionError("terminal state was not bound to final cycle")
            if self.fail_final:
                raise self.gate.HostGateError("terminal state changed")

        def emit_complete(self, cycles_requested: int, _timeout: float) -> None:
            self.events.append("complete")
            self._transcript.append(
                f"ASTERINAS_PHYSICAL_GRAPHICS_COMPLETE cycles={cycles_requested}"
            )

        def request_reboot(self, _timeout: float) -> None:
            self.events.append("request-reboot")

        def await_recovery(self, _timeout: float) -> None:
            self.events.append("recovery")
            if not self.recover:
                raise TimeoutError("fresh U-Boot prompt not observed")
            self._transcript.append("OpenSBI v1.7\nU-Boot 2026.07\n=> ")
            if self.late_fatal:
                self._transcript.append("Kernel panic - not syncing")

        def publish(self, result, screenshots, hdmi, outcomes) -> None:
            self.events.append(f"publish:{result.passed}")
            self.published = (result, screenshots, hdmi, outcomes)

        def close(self) -> None:
            self.events.append("close")

    @classmethod
    def _plan(cls):
        return SimpleNamespace(
            bootargs=cls.PLAN_BOOTARGS,
            plan_sha256=cls.PLAN_SHA256,
            validate=lambda: None,
        )

    def test_derives_the_fixed_physical_debug_bootargs(self) -> None:
        gate = load_gate(self)
        bootargs = gate.physical_bootargs(self._plan())
        tokens = bootargs.split()
        self.assertEqual(
            tokens[:3],
            [
                "console=tty0",
                "loglevel=off",
                "asterinas.klog_capture=info",
            ],
        )
        self.assertNotIn("console=ttyS0", tokens)
        self.assertEqual(tokens.count("asterinas.klog_capture=info"), 1)
        self.assertEqual(tokens.count("asterinas.reboot_after=1140"), 1)
        self.assertFalse(any(token.startswith("systemd.unit=") for token in tokens))
        self.assertNotIn("systemd.unit=multi-user.target", tokens)
        self.assertNotIn("asterinas.reboot_after=600", tokens)
        self.assertEqual(tokens.count("asterinas.mmc_write_partition2"), 1)
        self.assertEqual(
            tokens.count("systemd.mask=asterinas-browser-web-evidence.service"), 1
        )
        self.assertEqual(
            tokens.count("systemd.mask=asterinas-desktop-network.service"), 1
        )
        self.assertEqual(
            tokens.count("systemd.setenv=ASTERINAS_BROWSER_WEB_BASIC_ONLY=1"), 1
        )
        self.assertEqual(tokens.count("systemd.mask=serial-getty@ttyS0.service"), 1)
        self.assertEqual(tokens.count("systemd.mask=console-getty.service"), 1)
        self.assertEqual(
            tokens.count("systemd.setenv=ASTERINAS_WEB_NETWORK_MODE=proxy"), 1
        )
        self.assertEqual(
            tokens.count("systemd.setenv=ASTERINAS_DESKTOP_PROXY_HOST=127.0.0.1"),
            1,
        )
        self.assertEqual(
            tokens.count("systemd.setenv=ASTERINAS_DESKTOP_PROXY_PORT=9"), 1
        )
        self.assertNotIn(
            "systemd.setenv=ASTERINAS_DESKTOP_PROXY_HOST=10.100.19.216", tokens
        )
        self.assertEqual(
            tokens[-4:],
            [
                "--",
                "--root-init=systemd",
                "--debug-console=isolated-root",
                "--volatile-home",
            ],
        )

    def test_physical_bootargs_canonicalize_partition_write_aliases(self) -> None:
        gate = load_gate(self)
        for spelling in (
            "asterinas.mmc_write_partition2",
            "asterinas.mmc_write_partition2=1",
            "asterinas.mmc-write-partition2=yes",
        ):
            with self.subTest(spelling=spelling):
                plan = self._plan()
                plan.bootargs = self.PLAN_BOOTARGS.replace(
                    "asterinas.mmc_write_partition2", spelling
                )
                normalized_names = {
                    token.partition("=")[0].replace("-", "_")
                    for token in gate.physical_bootargs(plan).split()
                }
                self.assertIn("asterinas.mmc_write_partition2", normalized_names)
                self.assertEqual(
                    gate.physical_bootargs(plan)
                    .split()
                    .count("asterinas.mmc_write_partition2"),
                    1,
                )

    def test_publishes_pass_only_after_three_cycles_hdmi_and_recovery(self) -> None:
        gate = load_gate(self)
        operations = self.Operations(gate)
        result = gate.run_physical_graphics(
            self._plan(),
            gate.PhysicalGraphicsConfig(),
            operations,
            nonces=self.NONCES,
            artifact_validator=lambda _plan: {},
        )
        self.assertTrue(result.passed)
        self.assertTrue(result.physical)
        self.assertTrue(result.recovered)
        self.assertEqual([cycle.cycle for cycle in result.cycles], [1, 2, 3])
        self.assertLess(
            operations.events.index("recovery"), operations.events.index("publish:True")
        )
        self.assertLess(
            operations.events.index("complete"),
            operations.events.index("request-reboot"),
        )
        self.assertLess(
            operations.events.index("request-reboot"),
            operations.events.index("recovery"),
        )
        self.assertEqual(operations.events.count("publish:True"), 1)
        self.assertLess(
            operations.events.index("hdmi"), operations.events.index("hdmi-pixels")
        )
        self.assertLess(
            operations.events.index("hdmi-pixels"), operations.events.index("final-state")
        )
        self.assertEqual(operations.events[-1], "close")

    def test_wrong_hdmi_pixels_fail_but_still_recover(self) -> None:
        gate = load_gate(self)
        operations = self.Operations(gate, fail_hdmi_pixels=True)
        result = gate.run_physical_graphics(
            self._plan(), gate.PhysicalGraphicsConfig(), operations,
            nonces=self.NONCES, artifact_validator=lambda _plan: {},
        )
        self.assertFalse(result.passed)
        self.assertTrue(result.recovered)
        self.assertIn("hdmi-pixels-do-not-match", result.reason)
        self.assertNotIn("final-state", operations.events)

    def test_one_cycle_operator_attestation_preserves_terminal_checks(self) -> None:
        gate = load_gate(self)
        operations = self.Operations(gate)
        result = gate.run_physical_graphics(
            self._plan(),
            gate.PhysicalGraphicsConfig(
                cycles_requested=1,
                display_mode=gate.DisplayEvidenceMode.OPERATOR_ATTESTED,
            ),
            operations,
            nonces=self.NONCES[:1],
            artifact_validator=lambda _plan: {},
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.schema_version, 2)
        self.assertEqual(result.cycles_requested, 1)
        self.assertEqual(result.reason, "physical-graphics-operator-attested-pass")
        self.assertEqual([cycle.cycle for cycle in result.cycles], [1])
        self.assertIsNone(result.hdmi)
        self.assertIsNotNone(result.operator_display)
        self.assertEqual(
            result.operator_display.nonce_sha256, result.cycles[0].nonce_sha256
        )
        self.assertIn("operator-display", operations.events)
        self.assertNotIn("hdmi", operations.events)
        self.assertLess(
            operations.events.index("operator-display"),
            operations.events.index("final-state"),
        )
        self.assertLess(
            operations.events.index("final-state"), operations.events.index("complete")
        )

    def test_operator_start_follows_readiness_and_precedes_cycle(self) -> None:
        gate = load_gate(self)
        operations = self.Operations(gate)

        def confirm(code: str) -> None:
            operations.events.append(f"operator-start:{code}")

        result = gate.run_physical_graphics(
            self._plan(),
            gate.PhysicalGraphicsConfig(
                cycles_requested=1,
                display_mode=gate.DisplayEvidenceMode.OPERATOR_ATTESTED,
            ),
            operations,
            nonces=self.NONCES[:1],
            operator_start=confirm,
            artifact_validator=lambda _plan: {},
        )

        self.assertTrue(result.passed)
        self.assertLess(operations.events.index("readiness"), operations.events.index("operator-start:0123"))
        self.assertLess(operations.events.index("operator-start:0123"), operations.events.index("cycle-1"))

    def test_rejects_nonce_count_that_does_not_match_requested_cycles(self) -> None:
        gate = load_gate(self)
        operations = self.Operations(gate)

        with self.assertRaisesRegex(gate.HostGateError, "requested cycle count"):
            gate.run_physical_graphics(
                self._plan(),
                gate.PhysicalGraphicsConfig(cycles_requested=3),
                operations,
                nonces=self.NONCES[:1],
                artifact_validator=lambda _plan: {},
            )

        self.assertEqual(operations.events, [])

    def test_recovery_failure_and_late_fatal_marker_cannot_publish_pass(self) -> None:
        gate = load_gate(self)
        for options in ({"recover": False}, {"late_fatal": True}):
            with self.subTest(options=options):
                operations = self.Operations(gate, **options)
                result = gate.run_physical_graphics(
                    self._plan(),
                    gate.PhysicalGraphicsConfig(),
                    operations,
                    nonces=self.NONCES,
                    artifact_validator=lambda _plan: {},
                )
                self.assertFalse(result.passed)
                self.assertNotIn("publish:True", operations.events)
                self.assertEqual(operations.events[-1], "close")

    def test_early_guest_failure_still_waits_for_recovery_before_publication(
        self,
    ) -> None:
        gate = load_gate(self)
        operations = self.Operations(gate, fail_cycle=2)
        result = gate.run_physical_graphics(
            self._plan(),
            gate.PhysicalGraphicsConfig(),
            operations,
            nonces=self.NONCES,
            artifact_validator=lambda _plan: {},
        )
        self.assertFalse(result.passed)
        self.assertTrue(result.recovered)
        self.assertIn("request-reboot", operations.events)
        self.assertLess(
            operations.events.index("recovery"),
            operations.events.index("publish:False"),
        )
        self.assertEqual(len(operations.published[1]), 1)

    def test_terminal_state_change_after_hdmi_fails_then_recovers(self) -> None:
        gate = load_gate(self)
        operations = self.Operations(gate, fail_final=True)
        result = gate.run_physical_graphics(
            self._plan(),
            gate.PhysicalGraphicsConfig(),
            operations,
            nonces=self.NONCES,
            artifact_validator=lambda _plan: {},
        )
        self.assertFalse(result.passed)
        self.assertTrue(result.recovered)
        self.assertLess(
            operations.events.index("recovery"),
            operations.events.index("publish:False"),
        )

    def test_post_boot_setup_failure_still_recovers(self) -> None:
        gate = load_gate(self)
        operations = self.Operations(gate, fail_boot_after_start=True)
        result = gate.run_physical_graphics(
            self._plan(),
            gate.PhysicalGraphicsConfig(),
            operations,
            nonces=self.NONCES,
            artifact_validator=lambda _plan: {},
        )
        self.assertFalse(result.passed)
        self.assertTrue(result.recovered)
        self.assertLess(
            operations.events.index("recovery"),
            operations.events.index("publish:False"),
        )

    def test_unexpected_guest_exception_still_recovers_publishes_and_closes(
        self,
    ) -> None:
        gate = load_gate(self)
        error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid serial byte")
        operations = self.Operations(gate, fail_cycle=2, cycle_error=error)
        result = gate.run_physical_graphics(
            self._plan(),
            gate.PhysicalGraphicsConfig(),
            operations,
            nonces=self.NONCES,
            artifact_validator=lambda _plan: {},
        )
        self.assertFalse(result.passed)
        self.assertTrue(result.recovered)
        self.assertIn("unicode-decode-error", result.reason)
        self.assertLess(
            operations.events.index("recovery"),
            operations.events.index("publish:False"),
        )
        self.assertEqual(operations.events[-1], "close")

    def test_keyboard_interrupt_recovers_and_closes_before_reraising(self) -> None:
        gate = load_gate(self)
        operations = self.Operations(
            gate,
            fail_cycle=2,
            cycle_error=KeyboardInterrupt(),
        )
        with self.assertRaises(KeyboardInterrupt):
            gate.run_physical_graphics(
                self._plan(),
                gate.PhysicalGraphicsConfig(),
                operations,
                nonces=self.NONCES,
                artifact_validator=lambda _plan: {},
            )
        self.assertIn("recovery", operations.events)
        self.assertEqual(operations.events[-1], "close")
        self.assertFalse(
            any(event.startswith("publish:") for event in operations.events)
        )


class PhysicalCommandTests(unittest.TestCase):
    DAILY_USE_ID = "0123456789abcdef0123456789abcdef"

    def _daily_use_operations(
        self,
        gate,
        lines: tuple[str, ...],
        *,
        prefix: str = "boot noise\n",
    ):
        payload = prefix.encode() + ("\n".join(lines) + "\n").encode()
        serial = mock.Mock(transcript=payload)
        serial.checkpoint.return_value = len(prefix.encode())
        operations = object.__new__(gate.RealPhysicalGraphicsOperations)
        operations._serial = serial
        operations._guest_deadline = time.monotonic() + 300
        return operations, serial

    def test_daily_use_command_is_closed(self) -> None:
        gate = load_gate(self)
        command = gate.physical_daily_use_command(self.DAILY_USE_ID, 120.0, 4242)

        self.assertEqual(
            command,
            "/run/asterinas-tools/physical-graphics-control daily-use "
            "0123456789abcdef0123456789abcdef 120 4242",
        )
        for experiment_id, timeout, pid in (
            ("short", 120.0, 4242),
            (self.DAILY_USE_ID.upper(), 120.0, 4242),
            (self.DAILY_USE_ID, 0.0, 4242),
            (self.DAILY_USE_ID, 120.1, 4242),
            (self.DAILY_USE_ID, 121.0, 4242),
            (self.DAILY_USE_ID, 120.0, True),
            (self.DAILY_USE_ID, 120.0, 0),
        ):
            with (
                self.subTest(experiment_id=experiment_id, timeout=timeout, pid=pid),
                self.assertRaises(gate.HostGateError),
            ):
                gate.physical_daily_use_command(experiment_id, timeout, pid)

    def test_run_daily_use_profile_requires_matching_terminal_id(self) -> None:
        gate = load_gate(self)
        operations, _serial = self._daily_use_operations(
            gate,
            (
                "__ASTERINAS_PHYSICAL_DAILY_USE__ "
                "experiment_id=ffffffffffffffffffffffffffffffff outcome=pass "
                "gate_status=0 upload_status=0",
            ),
        )

        with self.assertRaisesRegex(gate.HostGateError, "experiment"):
            operations.run_daily_use_profile(self.DAILY_USE_ID, 120.0, 4242)

    def test_run_daily_use_profile_returns_closed_terminal_status(self) -> None:
        gate = load_gate(self)
        operations, serial = self._daily_use_operations(
            gate,
            (
                "__ASTERINAS_PHYSICAL_DAILY_USE__ "
                f"experiment_id={self.DAILY_USE_ID} outcome=pass "
                "gate_status=0 upload_status=0",
            ),
            prefix=(
                "__ASTERINAS_PHYSICAL_DAILY_USE__ "
                "experiment_id=ffffffffffffffffffffffffffffffff outcome=fail "
                "gate_status=1 upload_status=0\n"
            ),
        )

        with mock.patch.object(operations, "_sync_serial_log"):
            status = operations.run_daily_use_profile(self.DAILY_USE_ID, 120.0, 4242)

        self.assertEqual(status.experiment_id, self.DAILY_USE_ID)
        self.assertEqual(status.outcome, "pass")
        self.assertEqual((status.gate_status, status.upload_status), (0, 0))
        self.assertEqual(
            serial.send.call_args.args[0],
            (
                "/run/asterinas-tools/physical-graphics-control daily-use "
                f"{self.DAILY_USE_ID} 120 4242\n"
            ).encode(),
        )

    def test_run_daily_use_profile_uses_guest_lifetime_as_outer_deadline(
        self,
    ) -> None:
        gate = load_gate(self)
        operations, serial = self._daily_use_operations(
            gate,
            (
                "__ASTERINAS_PHYSICAL_DAILY_USE__ "
                f"experiment_id={self.DAILY_USE_ID} outcome=pass "
                "gate_status=0 upload_status=0",
            ),
        )
        operations._guest_deadline = 2000.0

        with (
            mock.patch.object(gate.time, "monotonic", return_value=1000.0),
            mock.patch.object(operations, "_sync_serial_log"),
        ):
            operations.run_daily_use_profile(self.DAILY_USE_ID, 120.0, 4242)

        self.assertEqual(serial.send.call_args.args[1], 2000.0)

    def test_run_daily_use_profile_rejects_open_or_duplicate_terminal(self) -> None:
        gate = load_gate(self)
        valid = (
            "__ASTERINAS_PHYSICAL_DAILY_USE__ "
            f"experiment_id={self.DAILY_USE_ID} outcome=pass "
            "gate_status=0 upload_status=0"
        )
        invalid_lines = (
            (valid, valid),
            (valid.replace("gate_status=0", "gate_status=no"),),
            (valid.replace("gate_status=0", "gate_status=-1"),),
            (valid.replace("outcome=pass", "outcome=unknown"),),
            (valid.replace("gate_status=0", "gate_status=1"),),
            (valid.replace("upload_status=0", "upload_status=1"),),
            (valid.replace("outcome=pass", "outcome=fail"),),
            (valid.replace(self.DAILY_USE_ID, "0" * 31 + "G"),),
            (valid + " trailing=field",),
        )
        for lines in invalid_lines:
            with self.subTest(lines=lines):
                operations, _serial = self._daily_use_operations(gate, lines)
                with self.assertRaises(gate.HostGateError):
                    operations.run_daily_use_profile(self.DAILY_USE_ID, 120.0, 4242)

    def test_one_cycle_real_prompt_uses_one_as_the_denominator(self) -> None:
        gate = load_gate(self)
        operations = object.__new__(gate.RealPhysicalGraphicsOperations)
        operations._browser_pid = 42
        operations._cycles_requested = 1
        operations._guest_deadline = time.monotonic() + 900
        serial = mock.Mock(transcript=b"")
        serial.checkpoint.return_value = 0
        operations._serial = serial
        nonce = "0123"
        nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
        lines = iter(
            (
                f"ASTERINAS_PHYSICAL_GRAPHICS_READY cycle=1 nonce_sha256={nonce_hash}",
                "ASTERINAS_PHYSICAL_GRAPHICS_PASS cycle=1",
                "__ASTERINAS_PHYSICAL_COMMAND_STATUS__cycle=1 status=0",
            )
        )
        with (
            mock.patch.object(
                operations,
                "_next_line",
                side_effect=lambda _serial, cursor, _deadline: (
                    next(lines),
                    cursor + 1,
                ),
            ),
            mock.patch.object(operations, "_sync_serial_log"),
            mock.patch.object(gate, "extract_screenshot_frame", return_value=b"png"),
            mock.patch("builtins.print") as printed,
        ):
            operations.run_cycle(1, nonce, 30)

        self.assertIn("[physical cycle 1/1]", printed.call_args.args[0])

    def test_recovery_interrupts_a_stuck_shell_command_before_reboot(self) -> None:
        gate = load_gate(self)
        operations = object.__new__(gate.RealPhysicalGraphicsOperations)
        operations._debug_console_ready = True
        operations._guest_deadline = time.monotonic() + 60
        operations._recovery_cursor = 0
        serial = mock.Mock(transcript=b"1234567^C\r\nroot@asterinas-debug:/# ")
        serial.checkpoint.side_effect = (7, 11)
        operations._serial = serial
        nonce = "0123"
        marker = f"__ASTERINAS_PHYSICAL_REBOOT__ nonce={nonce}"

        with (
            mock.patch.object(gate.secrets, "token_hex", return_value=nonce),
            mock.patch.object(
                operations,
                "_next_line",
                return_value=(marker, 12),
            ),
            mock.patch.object(operations, "_sync_serial_log"),
        ):
            operations.request_reboot(30)

        self.assertEqual(
            serial.send.call_args_list[0].args[0],
            b"\x03",
        )
        self.assertEqual(
            serial.send.call_args_list[1].args[0],
            f"sync; printf '{marker}\\n'; reboot -f\n".encode(),
        )
        self.assertEqual(
            serial.wait_for.call_args_list,
            [
                mock.call(b"^C", mock.ANY, start=7),
                mock.call(b"root@asterinas-debug:/#", mock.ANY, start=9),
            ],
        )
        self.assertEqual(operations._recovery_cursor, 11)

    def test_one_cycle_final_command_and_completion_marker(self) -> None:
        gate = load_gate(self)
        command = gate.physical_final_command("0123", 41, 180.0, cycle=1)
        self.assertEqual(
            command,
            "/run/asterinas-tools/physical-graphics-control final "
            "1 0123 180 540 41",
        )
        self.assertLess(len(command.encode()), 128)

        operations = object.__new__(gate.RealPhysicalGraphicsOperations)
        serial = mock.Mock()
        serial.checkpoint.return_value = 0
        operations._serial = serial
        operations._guest_deadline = time.monotonic() + 60
        with (
            mock.patch.object(
                operations,
                "_next_line",
                return_value=(
                    "ASTERINAS_PHYSICAL_GRAPHICS_COMPLETE cycles=1",
                    1,
                ),
            ),
            mock.patch.object(operations, "_sync_serial_log"),
        ):
            operations.emit_complete(1, 30)
        self.assertIn(
            b"ASTERINAS_PHYSICAL_GRAPHICS_COMPLETE cycles=1",
            serial.send.call_args.args[0],
        )

    def test_three_slow_cycles_share_the_original_board_reboot_budget(self) -> None:
        gate = load_gate(self)
        operations = object.__new__(gate.RealPhysicalGraphicsOperations)
        operations._browser_pid = 42
        operations._cycles_requested = 3
        operations._guest_deadline = 970.0
        serial = mock.Mock(transcript=b"")
        serial.checkpoint.return_value = 0
        operations._serial = serial
        now = [100.0]
        nonce = "0123"
        nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
        lines = iter(
            (elapsed, line)
            for cycle in (1, 2, 3)
            for elapsed, line in (
                (
                    250.0,
                    f"ASTERINAS_PHYSICAL_GRAPHICS_READY cycle={cycle} nonce_sha256={nonce_hash}",
                ),
                (100.0, f"ASTERINAS_PHYSICAL_GRAPHICS_PASS cycle={cycle}"),
                (0.0, f"__ASTERINAS_PHYSICAL_COMMAND_STATUS__cycle={cycle} status=0"),
            )
        )

        def next_line(_serial, cursor, deadline):
            elapsed, line = next(lines)
            if now[0] + elapsed >= deadline:
                now[0] = deadline
                raise TimeoutError("bounded serial phase expired")
            now[0] += elapsed
            return line, cursor + 1

        with (
            mock.patch.object(gate.time, "monotonic", side_effect=lambda: now[0]),
            mock.patch.object(operations, "_next_line", side_effect=next_line),
            mock.patch.object(operations, "_sync_serial_log"),
            mock.patch.object(gate, "extract_screenshot_frame", return_value=b"png"),
            mock.patch("builtins.print"),
        ):
            operations.run_cycle(1, nonce, 100.0)
            operations.run_cycle(2, nonce, 100.0)
            with self.assertRaises(TimeoutError):
                operations.run_cycle(3, nonce, 100.0)
        self.assertEqual(now[0], 970.0)
        self.assertTrue(
            all(call.args[1] <= 970.0 for call in serial.send.call_args_list)
        )

    def test_rejects_command_exit_before_pass_without_waiting(self) -> None:
        from tools.riscv import megrez_physical_graphics as gate

        ready = (
            "ASTERINAS_PHYSICAL_GRAPHICS_READY cycle=1 nonce_sha256="
            + hashlib.sha256("0123".encode()).hexdigest()
        )
        for prefix in ([], [ready]):
            for status in ("cycle=1 status=1", "cycle=1 status=0", "malformed"):
                with self.subTest(ready=bool(prefix), status=status):
                    serial = mock.Mock()
                    serial.checkpoint.return_value = 0
                    operations = object.__new__(gate.RealPhysicalGraphicsOperations)
                    operations._serial = serial
                    operations._browser_pid = 42
                    operations._cycles_requested = 3
                    operations._guest_deadline = time.monotonic() + 900
                    lines = iter(
                        [*prefix, "__ASTERINAS_PHYSICAL_COMMAND_STATUS__" + status]
                    )

                    def next_line(_serial, cursor, _deadline):
                        try:
                            return next(lines), cursor + 1
                        except StopIteration:
                            self.fail(
                                "waited for more serial output after command exit"
                            )

                    with (
                        mock.patch.object(
                            operations, "_next_line", side_effect=next_line
                        ),
                        mock.patch("builtins.print"),
                        self.assertRaisesRegex(
                            gate.HostGateError, "exited before PASS"
                        ),
                    ):
                        operations.run_cycle(1, "0123", 30)

    def test_cli_exposes_every_physical_phase_deadline(self) -> None:
        gate = load_gate(self)
        values = gate.parse_args(
            (
                "/dev/serial/by-id/test",
                "--plan",
                "/tmp/plan.json",
                "--output-directory",
                "/tmp/evidence",
                "--hdmi-capture",
                "/tmp/capture.png",
                "--open-timeout",
                "60",
                "--artifact-timeout",
                "300",
                "--boot-timeout",
                "120",
                "--cycle-timeout",
                "180",
                "--hdmi-timeout",
                "60",
                "--recovery-timeout",
                "930",
            )
        )
        self.assertEqual(
            (
                values.open_timeout,
                values.artifact_timeout,
                values.boot_timeout,
                values.cycle_timeout,
                values.hdmi_timeout,
                values.recovery_timeout,
            ),
            (60.0, 300.0, 120.0, 180.0, 60.0, 930.0),
        )

    def test_preflight_binds_fbdev_openbox_firefox_service_and_input_nodes(
        self,
    ) -> None:
        gate = load_gate(self)
        command = gate.physical_preflight_command()
        script = (
            REPOSITORY_ROOT / "tools/riscv/debian/rootfs/physical_graphics_control.sh"
        ).read_text()
        identity_script = (
            REPOSITORY_ROOT
            / "tools/riscv/debian/rootfs/desktop_input_identity.py"
        ).read_text()
        self.assertEqual(
            command, "/run/asterinas-tools/g preflight"
        )
        self.assertLessEqual(len(command.encode()), 32)
        for fragment in (
            "/dev/fb0",
            "/dev/input/event*",
            "/proc/$xorg_pid/fd/*",
            "readlink",
            "usb_boot_keyboard",
            "usb_boot_mouse",
            "QEMU Virtio Keyboard",
            "QEMU Virtio Tablet",
            "keyboard_node",
            "mouse_node",
            "xorg_keyboard",
            "xorg_mouse",
            "/etc/X11/xorg.conf.d/20-asterinas.conf",
            "pgrep -x Xorg",
            "/tmp/.X11-unix/X0",
            "openbox",
            "asterinas-browser-web.service",
            "MainPID",
            "NRestarts",
            "__ASTERINAS_PHYSICAL_PREFLIGHT__",
        ):
            self.assertIn(fragment, script)
        for fragment in ("0x81004506", "0x80084502", "0x81004507"):
            self.assertIn(fragment, identity_script)
        self.assertEqual(script.count("PYTHONDONTWRITEBYTECODE=1"), 5)
        self.assertNotIn("PYTHONPYCACHEPREFIX=", script)
        self.assertNotIn("dmesg", script)
        self.assertNotIn("Xorg.0.log", script)

    def test_preflight_returns_incomplete_marker_for_bounded_retry(self) -> None:
        gate = load_gate(self)

        serial = mock.Mock()
        serial.checkpoint.return_value = 11
        operations = object.__new__(gate.RealPhysicalGraphicsOperations)
        operations._serial = serial
        marker = (
            "__ASTERINAS_PHYSICAL_PREFLIGHT__ browser_pid=0 input_nodes=2 "
            "framebuffer=0 xorg_fbdev=0 openbox=1 firefox=0 "
            "browser_service=inactive browser_restarts=0 usb_inputs=0 "
            "usb_keyboard=0 usb_mouse=0 keyboard_node=missing "
            "mouse_node=missing xorg_keyboard=0 xorg_mouse=0"
        )

        with (
            mock.patch.object(
                operations,
                "_next_line",
                side_effect=[
                    (marker, 12),
                    AssertionError("ignored a complete preflight marker"),
                ],
            ),
            self.assertRaisesRegex(
                gate.HostGateError, "graphical readiness contract is incomplete"
            ),
        ):
            operations._probe_graphical_readiness(time.monotonic() + 30)

        serial.send.assert_called_once()

    def test_external_service_quiesce_is_fail_closed(self) -> None:
        gate = load_gate(self)
        command = gate.physical_external_services_quiesce_command()
        browser_command = gate.physical_browser_start_command()
        web_browser_command = gate.physical_web_browser_start_command()
        script = (
            REPOSITORY_ROOT
            / "tools/riscv/debian/rootfs/physical_external_services_quiesce.sh"
        ).read_text()
        control_script = (
            REPOSITORY_ROOT / "tools/riscv/debian/rootfs/physical_graphics_control.sh"
        ).read_text()

        self.assertEqual(
            command,
            "/run/asterinas-tools/q",
        )
        self.assertEqual(
            browser_command,
            "/run/asterinas-tools/g start-browser",
        )
        self.assertEqual(
            web_browser_command,
            "/run/asterinas-tools/g start-web",
        )
        self.assertLessEqual(len(browser_command.encode()), 40)
        self.assertLessEqual(len(web_browser_command.encode()), 32)
        self.assertNotIn("/usr/lib/asterinas", command)
        self.assertNotIn("||", command)
        self.assertNotIn(";", command)
        self.assertNotIn("\n", command)
        for fragment in (
            "systemctl_bounded()",
            '/usr/bin/timeout --kill-after=1s 5s /usr/bin/systemctl "$@"',
            "systemctl_bounded stop --no-block",
            "/run/systemd/system.control",
            "/run/asterinas-physical-home",
            "mount --bind",
            "$_asterinas_browser.d/physical.conf",
            "Environment=HOME=/run/asterinas-physical-home",
            "Environment=ASTERINAS_WEB_NETWORK_MODE=proxy",
            "_asterinas_proxy_host=127.0.0.1",
            "_asterinas_proxy_host=10.100.19.216",
            "Environment=ASTERINAS_DESKTOP_PROXY_HOST=$_asterinas_proxy_host",
            "Environment=ASTERINAS_DESKTOP_PROXY_PORT=9",
            "ln -sfn /dev/null",
            "systemctl_bounded daemon-reload",
            "_asterinas_stop_units",
            "_asterinas_after_stop_state",
            "active | activating | deactivating | reloading",
            "inactive | failed",
            "systemctl_bounded start --no-block asterinas-desktop.service",
            "systemctl_bounded start --no-block \\\n    asterinas-browser-web-timeline-basic.service",
            "systemctl_bounded reset-failed",
            "asterinas-browser-web-timeline-basic.service",
            "asterinas-browser-web-evidence.service",
            "asterinas-desktop-network.service",
            "serial-getty@ttyS0.service",
            "console-getty.service",
            "evidence_state=%s",
            "network_state=%s",
            "setup_status=%s",
            "failure_step=%s",
            "__ASTERINAS_PHYSICAL_EXTERNAL_FAILURE__ status=%s step=%s",
            "__ASTERINAS_PHYSICAL_EXTERNAL__",
        ):
            self.assertIn(fragment, script)
        self.assertEqual(script.count("/usr/bin/systemctl"), 1)
        self.assertNotIn("--job-mode=ignore-dependencies", script)
        self.assertNotIn("\n", browser_command)
        self.assertIn("--job-mode=ignore-dependencies", control_script)
        self.assertIn("__ASTERINAS_PHYSICAL_BROWSER_START__", control_script)
        start_section = control_script[control_script.index("start_browser() {") :]
        stop_browser = start_section.index(
            "stop --no-block asterinas-browser-web.service"
        )
        stop_desktop = start_section.index(
            "stop --no-block asterinas-desktop.service"
        )
        identify = start_section.index("set -- $(input_identity)")
        provider = start_section.index("ASTERINAS_DISPLAY_PROVIDER_ROOT=")
        start_desktop = start_section.index(
            "asterinas-desktop.service >/dev/null 2>&1", provider
        )
        start_browser = start_section.index(
            "asterinas-browser-web.service >/dev/null 2>&1", start_desktop
        )
        offsets = [
            stop_browser,
            stop_desktop,
            identify,
            provider,
            start_desktop,
            start_browser,
        ]
        self.assertEqual(offsets, sorted(offsets))
        self.assertIn('mount --bind "$_asterinas_home" /home/asterinas', script)
        self.assertNotIn(
            'mount --bind "$_asterinas_home/browser-web-timeline.log"', script
        )
        self.assertLess(
            script.index('mount --bind "$_asterinas_home" /home/asterinas'),
            script.index(
                "systemctl_bounded start --no-block \\\n    asterinas-browser-web-timeline-basic.service"
            ),
        )
        self.assertLess(
            script.index("ln -sfn /dev/null"),
            script.index("systemctl_bounded daemon-reload"),
        )
        self.assertLess(
            script.index("systemctl_bounded daemon-reload"),
            script.index("systemctl_bounded stop --no-block"),
        )
        self.assertLess(
            script.index("systemctl_bounded stop --no-block"),
            script.index("mount --bind"),
        )
        self.assertLess(
            script.index("mount --bind"),
            script.index(
                "systemctl_bounded start --no-block asterinas-desktop.service"
            ),
        )
        self.assertLess(
            script.index(
                "systemctl_bounded start --no-block asterinas-desktop.service"
            ),
            script.index(
                "systemctl_bounded start --no-block \\\n    asterinas-browser-web-timeline-basic.service"
            ),
        )
        self.assertNotIn("systemctl_bounded start --no-block graphical.target", script)
        self.assertIn("start-web) [ \"$#\" -eq 0 ]", control_script)
        self.assertIn(
            '/usr/bin/timeout --kill-after=1s 10s /usr/bin/systemctl "$@"',
            control_script,
        )
        self.assertIn(
            "/usr/bin/timeout --kill-after=1s 10s /usr/bin/python3 -c",
            control_script,
        )
        self.assertIn(
            "/run/asterinas-tools/desktop-input-identity", control_script
        )
        self.assertNotIn('open(p,"rb",buffering=0)', control_script)
        self.assertIn('if [ "$stop_attempt" -ge 120 ]', control_script)
        self.assertIn("active | activating | deactivating | reloading", control_script)
        self.assertIn("inactive | failed", control_script)
        for stage in (
            "input-wait",
            "input-ready",
            "desktop-start",
            "desktop-ready",
            "browser-start",
        ):
            self.assertIn("browser_stage " + stage, control_script)
        self.assertIn(
            "__ASTERINAS_PHYSICAL_BROWSER_STAGE__ stage=%s", control_script
        )

    def test_system_probe_is_one_short_stage1_bound_command(self) -> None:
        gate = load_gate(self)
        nonce = "0123456789abcdef0123456789abcdef"
        command = gate.physical_system_probe_command(nonce)
        serial_commands = gate.physical_system_probe_serial_commands(nonce)
        script = (
            REPOSITORY_ROOT / "tools/riscv/debian/rootfs/physical_system_probe.sh"
        ).read_text()

        self.assertEqual(
            command,
            "/run/asterinas-tools/s "
            "0123456789abcdef0123456789abcdef",
        )
        self.assertLess(len(command.encode()), 128)
        self.assertEqual(
            serial_commands,
            (
                "N=0123456789abcdef",
                "N=${N}0123456789abcdef",
                "/run/asterinas-tools/s $N",
            ),
        )
        self.assertLessEqual(max(map(len, serial_commands)), 32)
        self.assertIn('nonce=${1-${N-}}', script)
        self.assertNotIn("systemctl", command)
        self.assertNotIn("systemctl", script)
        self.assertNotIn("sleep", script)
        self.assertNotIn("GRAPHICAL", script)
        self.assertNotIn("DESKTOP", script)
        with self.assertRaises(ValueError):
            gate.physical_system_probe_command("not-a-nonce")

    def test_real_gate_requires_quiesced_services_before_preflight(self) -> None:
        gate = load_gate(self)

        class Serial:
            def checkpoint(self) -> int:
                return 7

            def send(self, payload: bytes, deadline: float) -> None:
                del deadline
                self.command = payload.decode()

        serial = Serial()
        operations = object.__new__(gate.RealPhysicalGraphicsOperations)
        operations._serial = serial
        with mock.patch.object(
            operations,
            "_next_line",
            return_value=(
                "__ASTERINAS_PHYSICAL_EXTERNAL__ status=0 setup_status=124 "
                "failure_step=none "
                "evidence_state=inactive evidence_pid=0 "
                "network_state=inactive network_pid=0",
                8,
            ),
        ):
            operations._quiesce_external_services(100.0)

        self.assertEqual(
            serial.command,
            "/run/asterinas-tools/q\n",
        )

    def test_graphics_start_ack_precedes_the_strong_readiness_probe(self) -> None:
        gate = load_gate(self)

        class Serial:
            def checkpoint(self) -> int:
                return 7

            def send(self, payload: bytes, deadline: float) -> None:
                del deadline
                self.command = payload.decode()

        serial = Serial()
        operations = object.__new__(gate.RealPhysicalGraphicsOperations)
        operations._serial = serial
        with mock.patch.object(
            operations,
            "_next_line",
            side_effect=[
                ("__ASTERINAS_PHYSICAL_BROWSER_START__ status=0", 8),
                AssertionError("waited for a quiet-console service log"),
            ],
        ) as next_line:
            operations._start_browser(100.0)

        self.assertEqual(serial.command, gate.physical_browser_start_command() + "\n")
        self.assertLess(len(serial.command.encode()), 128)
        next_line.assert_called_once()

    def test_graphics_start_ack_survives_an_interleaved_kernel_log_line(self) -> None:
        gate = load_gate(self)

        class Serial:
            def checkpoint(self) -> int:
                return 7

            def send(self, payload: bytes, deadline: float) -> None:
                del payload, deadline

        operations = object.__new__(gate.RealPhysicalGraphicsOperations)
        operations._serial = Serial()
        mixed_line = (
            "\x1b[32m[ 33.066]\x1b[0m "
            "__ASTERINAS_PHYSICAL_BROWSER_START__ status=0"
        )
        with mock.patch.object(
            operations,
            "_next_line",
            side_effect=[
                (mixed_line, 8),
                AssertionError("ignored an interleaved browser start marker"),
            ],
        ) as next_line:
            operations._start_browser(100.0)

        next_line.assert_called_once()

    def test_isolated_readiness_has_no_runtime_service_reconfiguration(self) -> None:
        gate = load_gate(self)
        events: list[str] = []

        class Serial:
            transcript = (gate.DEBUG_CONSOLE_READY + "\n").encode()

            def wait_for(self, marker: bytes, _deadline: float) -> None:
                self_test.assertEqual(marker, gate.DEBUG_CONSOLE_READY.encode())
                events.append("ready")

        self_test = self
        operations = object.__new__(gate.RealPhysicalGraphicsOperations)
        operations._serial = Serial()
        operations._guest_deadline = time.monotonic() + 60
        operations._browser_pid = None
        readiness = gate.GraphicalReadinessEvidence(
            browser_pid=41,
            input_nodes=2,
            framebuffer=True,
            xorg_fbdev=True,
            openbox=True,
            firefox=True,
            browser_service="active",
            browser_restarts=0,
            usb_inputs=2,
            usb_keyboard=True,
            usb_mouse=True,
            keyboard_node="event1",
            mouse_node="event0",
            xorg_keyboard=True,
            xorg_mouse=True,
        )

        with (
            mock.patch.object(
                gate,
                "validate_debug_console_readiness",
                side_effect=lambda _transcript: events.append("validate"),
            ),
            mock.patch.object(
                operations,
                "_probe_system_readiness",
                side_effect=lambda _deadline: events.append("debug"),
            ),
            mock.patch.object(
                operations,
                "_probe_graphical_readiness",
                side_effect=lambda _deadline: (events.append("probe"), readiness)[1],
            ),
            mock.patch.object(
                operations,
                "_start_browser",
                side_effect=lambda _deadline: events.append("start-browser"),
            ),
            mock.patch.object(
                operations,
                "_sync_serial_log",
                side_effect=lambda: events.append("sync"),
            ),
        ):
            result = operations.prove_graphical_readiness(30)

        self.assertEqual(result, readiness)
        self.assertEqual(
            events,
            [
                "ready",
                "validate",
                "debug",
                "start-browser",
                "probe",
                "sync",
            ],
        )

    def test_readiness_requires_two_linux_evdev_usb_hid_devices(self) -> None:
        gate = load_gate(self)
        values = {
            "browser_pid": 41,
            "input_nodes": 2,
            "framebuffer": True,
            "xorg_fbdev": True,
            "openbox": True,
            "firefox": True,
            "browser_service": "active",
            "browser_restarts": 0,
            "usb_inputs": 2,
            "usb_keyboard": True,
            "usb_mouse": True,
            "keyboard_node": "event1",
            "mouse_node": "event0",
            "xorg_keyboard": True,
            "xorg_mouse": True,
        }
        gate.GraphicalReadinessEvidence(**values)
        for mutation in (
            {"usb_inputs": 1},
            {"usb_keyboard": False},
            {"usb_mouse": False},
            {"keyboard_node": "missing"},
            {"mouse_node": "missing"},
            {"keyboard_node": "event0", "mouse_node": "event0"},
            {"xorg_keyboard": False},
            {"xorg_mouse": False},
            {"framebuffer": False},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(gate.HostGateError):
                gate.GraphicalReadinessEvidence(**(values | mutation))

    def test_cycle_command_joins_firefox_namespace_without_synthesizing_input(
        self,
    ) -> None:
        gate = load_gate(self)
        command = gate.physical_cycle_command(2, "0123", 180.0)
        self.assertEqual(
            command,
            "/run/asterinas-tools/physical-graphics-control cycle "
            "2 0123 180 540 0 1920 1080",
        )
        self.assertLess(len(command.encode()), 128)
        for forbidden in (
            "xdotool",
            "PerformActions",
            "ElementClick",
            "key ",
            "mouse_move",
        ):
            self.assertNotIn(forbidden, command)
        for cycle, nonce, timeout in (
            (0, "0123", 180.0),
            (1, "not-a-nonce", 180.0),
            (1, "0123", 301.0),
        ):
            with (
                self.subTest(cycle=cycle, nonce=nonce, timeout=timeout),
                self.assertRaises(ValueError),
            ):
                gate.physical_cycle_command(cycle, nonce, timeout)
        with self.assertRaises(ValueError):
            gate.physical_cycle_command(
                1, "0123", 180.0, setup_timeout=901.0
            )

    def test_final_command_rechecks_cycle_three_without_navigation_or_input(
        self,
    ) -> None:
        gate = load_gate(self)
        command = gate.physical_final_command("0123", 41, 180.0)
        script = (
            REPOSITORY_ROOT / "tools/riscv/debian/rootfs/physical_graphics_control.sh"
        ).read_text()
        self.assertEqual(
            command,
            "/run/asterinas-tools/physical-graphics-control final "
            "3 0123 180 540 41",
        )
        self.assertLess(len(command.encode()), 128)
        for fragment in (
            "nsenter",
            "PYTHONDONTWRITEBYTECODE=1",
            'case "$cycle" in 1 | 3)',
            "--firefox-pid",
            "--verify-final",
            '"$setup_timeout"',
            "__ASTERINAS_PHYSICAL_FINAL_STATUS__",
        ):
            self.assertIn(fragment, script)
        for forbidden in ("xdotool", "PerformActions", "ElementClick", "mouse_move"):
            self.assertNotIn(forbidden, command)


class ScreenshotTransferTests(unittest.TestCase):
    def test_extracts_one_bounded_hash_bound_serial_png(self) -> None:
        gate = load_gate(self)
        payload = png_payload(value=0x42)
        digest = hashlib.sha256(payload).hexdigest()
        transcript = (
            "shell echo noise\n"
            f"__ASTERINAS_PHYSICAL_SCREENSHOT_BEGIN__ cycle=2 size={len(payload)} sha256={digest}\n"
            f"{base64.b64encode(payload).decode()}\n"
            "__ASTERINAS_PHYSICAL_SCREENSHOT_END__ cycle=2\n"
        )
        self.assertEqual(gate.extract_screenshot_frame(transcript, 2), payload)

    def test_extracts_frame_from_serial_transcript_with_double_carriage_returns(
        self,
    ) -> None:
        gate = load_gate(self)
        payload = png_payload(value=0x43)
        digest = hashlib.sha256(payload).hexdigest()
        transcript = (
            "shell echo noise\r\r\n"
            f"__ASTERINAS_PHYSICAL_SCREENSHOT_BEGIN__ cycle=2 size={len(payload)} sha256={digest}\r\r\n"
            f"{base64.b64encode(payload).decode()}\r\r\n"
            "__ASTERINAS_PHYSICAL_SCREENSHOT_END__ cycle=2\r\r\n"
        )
        self.assertEqual(gate.extract_screenshot_frame(transcript, 2), payload)

    def test_rejects_duplicate_mismatched_and_oversized_screenshot_frames(self) -> None:
        gate = load_gate(self)
        payload = png_payload(value=0x42)
        digest = hashlib.sha256(payload).hexdigest()
        frame = (
            f"__ASTERINAS_PHYSICAL_SCREENSHOT_BEGIN__ cycle=1 size={len(payload)} sha256={digest}\n"
            f"{base64.b64encode(payload).decode()}\n"
            "__ASTERINAS_PHYSICAL_SCREENSHOT_END__ cycle=1\n"
        )
        variants = (
            frame + frame,
            frame.replace(digest, "0" * 64),
            frame.replace(
                f"size={len(payload)}", f"size={gate.MAX_GUEST_SCREENSHOT_BYTES + 1}"
            ),
        )
        for transcript in variants:
            with self.assertRaises(gate.HostGateError):
                gate.extract_screenshot_frame(transcript, 1)

    def test_rejects_structurally_invalid_or_wrong_size_png_frame(self) -> None:
        gate = load_gate(self)
        for payload in (png_payload(width=1280, height=1024), b"\x89PNG\r\n\x1a\nbad"):
            digest = hashlib.sha256(payload).hexdigest()
            frame = (
                f"__ASTERINAS_PHYSICAL_SCREENSHOT_BEGIN__ cycle=1 "
                f"size={len(payload)} sha256={digest}\n"
                f"{base64.b64encode(payload).decode()}\n"
                "__ASTERINAS_PHYSICAL_SCREENSHOT_END__ cycle=1\n"
            )
            with self.subTest(size=len(payload)), self.assertRaises(gate.HostGateError):
                gate.extract_screenshot_frame(frame, 1)


class PublicationTests(unittest.TestCase):
    def test_operator_publication_has_private_hashed_attestation_and_no_hdmi(
        self,
    ) -> None:
        gate = load_gate(self)

        class Output:
            path = Path("/retained")

            def __init__(self) -> None:
                self.payloads: dict[str, bytes] = {}

            def atomic_write(self, name: str, payload: bytes, *, mode: int) -> None:
                self_test.assertEqual(mode, 0o600)
                self.payloads[name] = payload

            def sha256(self, name: str) -> str:
                return hashlib.sha256(self.payloads[name]).hexdigest()

        self_test = self
        operator_display = gate.OperatorDisplayEvidence(
            kind="operator-attested",
            nonce_sha256="c" * 64,
            state="cyan-final-cycle-pass",
            confirmed=True,
        )
        result = gate.PhysicalGraphicsResult(
            schema_version=2,
            cycles_requested=1,
            passed=False,
            physical=True,
            reason="diagnostic",
            plan_sha256="a" * 64,
            bootargs_sha256="b" * 64,
            recovered=False,
            readiness=None,
            cycles=(),
            hdmi=None,
            operator_display=operator_display,
            transport=(),
        )
        operations = gate.RealPhysicalGraphicsOperations(
            SimpleNamespace(artifacts=(), plan_sha256="a" * 64),
            "/dev/null",
            Path("/unused"),
            None,
            display_mode=gate.DisplayEvidenceMode.OPERATOR_ATTESTED,
            cycles_requested=1,
        )
        output = Output()
        operations._output = output

        operations.publish(result, (), None, ())

        self.assertIn("operator-display-attestation.json", output.payloads)
        self.assertNotIn("hdmi-evidence.png", output.payloads)
        self.assertNotIn("hdmi-evidence.jpg", output.payloads)
        attestation = output.payloads["operator-display-attestation.json"]
        self.assertEqual(json.loads(attestation), asdict(operator_display))
        self.assertIn(
            hashlib.sha256(attestation).hexdigest()
            + "  operator-display-attestation.json",
            output.payloads["sha256sums.txt"].decode(),
        )

    def test_result_requires_schema_two_and_mutually_exclusive_display_evidence(
        self,
    ) -> None:
        gate = load_gate(self)
        operator_display = gate.OperatorDisplayEvidence(
            kind="operator-attested",
            nonce_sha256="c" * 64,
            state="cyan-final-cycle-pass",
            confirmed=True,
        )
        hdmi = gate.FileEvidence(
            path=Path("/retained/hdmi-evidence.png"),
            size=9,
            sha256="d" * 64,
            format="png",
        )
        common = {
            "cycles_requested": 1,
            "passed": False,
            "physical": True,
            "reason": "diagnostic",
            "plan_sha256": "a" * 64,
            "bootargs_sha256": "b" * 64,
            "recovered": False,
            "readiness": None,
            "cycles": (),
            "transport": (),
        }

        with self.assertRaisesRegex(gate.HostGateError, "schema version"):
            gate.PhysicalGraphicsResult(
                schema_version=1,
                hdmi=None,
                operator_display=None,
                **common,
            )
        with self.assertRaisesRegex(gate.HostGateError, "conflicting"):
            gate.PhysicalGraphicsResult(
                schema_version=2,
                hdmi=hdmi,
                operator_display=operator_display,
                **common,
            )

    def test_result_is_the_last_commit_marker_and_is_listed_in_hashes(self) -> None:
        gate = load_gate(self)

        class Output:
            path = Path("/retained")

            def __init__(self) -> None:
                self.payloads: dict[str, bytes] = {}
                self.writes: list[str] = []

            def atomic_write(self, name: str, payload: bytes, *, mode: int) -> None:
                self.assert_private(mode)
                self.payloads[name] = payload
                self.writes.append(name)

            @staticmethod
            def assert_private(mode: int) -> None:
                if mode != 0o600:
                    raise AssertionError("published evidence is not private")

            def sha256(self, name: str) -> str:
                return hashlib.sha256(self.payloads[name]).hexdigest()

        plan = SimpleNamespace(artifacts=(), plan_sha256="a" * 64)
        operations = gate.RealPhysicalGraphicsOperations(
            plan,
            "/dev/null",
            Path("/unused"),
            Path("/unused-capture"),
        )
        output = Output()
        operations._output = output
        result = gate.PhysicalGraphicsResult(
            schema_version=2,
            cycles_requested=3,
            passed=False,
            physical=True,
            reason="diagnostic",
            plan_sha256="a" * 64,
            bootargs_sha256="b" * 64,
            recovered=False,
            readiness=None,
            cycles=(),
            hdmi=None,
            operator_display=None,
            transport=(),
        )
        operations.publish(result, (), None, ())
        self.assertEqual(output.writes[-2:], ["sha256sums.txt", "result.json"])
        expected = hashlib.sha256(result.canonical_bytes()).hexdigest()
        self.assertIn(
            f"{expected}  result.json\n",
            output.payloads["sha256sums.txt"].decode(),
        )

    def test_physical_result_cannot_be_mislabeled_as_simulated(self) -> None:
        gate = load_gate(self)
        with self.assertRaisesRegex(gate.HostGateError, "simulated"):
            gate.PhysicalGraphicsResult(
                schema_version=2,
                cycles_requested=3,
                passed=False,
                physical=False,
                reason="diagnostic",
                plan_sha256="a" * 64,
                bootargs_sha256="b" * 64,
                recovered=False,
                readiness=None,
                cycles=(),
                hdmi=None,
                operator_display=None,
                transport=(),
            )


if __name__ == "__main__":
    unittest.main()
