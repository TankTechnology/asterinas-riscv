#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the unattended Megrez physical boot-stability gate."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.riscv import megrez_boot_stability as gate


class _Publisher:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.published = None

    def invalidate(self) -> None:
        self.events.append("invalidate")

    def publish(self, result, records) -> None:
        self.events.append(f"publish:{result.passed}")
        self.published = (result, records)


class _Operations:
    def __init__(
        self,
        cycle: int,
        events: list[str],
        *,
        fail_readiness: int | None,
        diagnostics: dict[int, bytes],
        recover: bool,
    ) -> None:
        self.cycle = cycle
        self.events = events
        self.fail_readiness = fail_readiness
        self.diagnostics = diagnostics
        self.recover = recover
        self._guest_started = False
        self._transcript = f"serial-cycle-{cycle}\n".encode()

    @property
    def guest_started(self) -> bool:
        return self._guest_started

    @property
    def transcript(self) -> bytes:
        return self._transcript

    def open(self, _timeout: float) -> None:
        self.events.append(f"open-{self.cycle}")

    def ensure_artifacts(self, _plan, _timeout: float) -> tuple[str, ...]:
        self.events.append(f"ensure-artifacts-{self.cycle}")
        return ("kernel:mmc", "initramfs:mmc", "megrez_dtb:mmc")

    def boot(self, _plan, bootargs: str, _timeout: float) -> None:
        self.events.append(f"boot-{self.cycle}")
        if "asterinas.mmc_write_partition2" in bootargs:
            raise AssertionError("boot stability gate may not write partition 2")
        self._guest_started = True

    def prove_boot_readiness(self, _timeout: float):
        self.events.append(f"readiness-{self.cycle}")
        if self.fail_readiness == self.cycle:
            raise gate.HostGateError("injected readiness failure")
        return gate.BootReadinessEvidence(
            browser_pid=40 + self.cycle,
            framebuffer=True,
            xorg_fbdev=True,
            openbox=True,
            firefox=True,
            browser_service="active",
            browser_restarts=0,
        )

    def collect_diagnostics(self, _timeout: float) -> bytes:
        self.events.append(f"collect-diagnostics-{self.cycle}")
        return self.diagnostics.get(self.cycle, b"boot healthy\n")

    def request_reboot(self, _timeout: float) -> None:
        self.events.append(f"request-reboot-{self.cycle}")

    def await_recovery(self, _timeout: float) -> None:
        self.events.append(f"recovery-{self.cycle}")
        if not self.recover:
            raise TimeoutError("fresh U-Boot prompt not observed")
        self._transcript += b"OpenSBI\nU-Boot\n=> \n"

    def close(self) -> None:
        self.events.append(f"close-{self.cycle}")


class _OperationsFactory:
    def __init__(
        self,
        *,
        fail_readiness: int | None = None,
        diagnostics: dict[int, bytes] | None = None,
        recover: bool = True,
    ) -> None:
        self.events: list[str] = []
        self.fail_readiness = fail_readiness
        self.diagnostics = diagnostics or {}
        self.recover = recover

    def __call__(self, cycle: int) -> _Operations:
        return _Operations(
            cycle,
            self.events,
            fail_readiness=self.fail_readiness,
            diagnostics=self.diagnostics,
            recover=self.recover,
        )


def _plan():
    return SimpleNamespace(
        bootargs=(
            "console=ttyS0 init=/init asterinas.mmc_write_partition2 "
            "asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1 "
            "asterinas.neighbor=eic7700-rj45,10.100.16.1,4c:d6:29:18:93:43 "
            "systemd.setenv=ASTERINAS_DESKTOP_PROXY_HOST=10.100.19.216 "
            "systemd.setenv=ASTERINAS_DESKTOP_PROXY_PORT=17893 "
            "systemd.setenv=ASTERINAS_DESKTOP_FIXTURE_URL=http://example/test "
            "-- --root-init=systemd"
        ),
        plan_sha256="a" * 64,
        validate=lambda: None,
    )


def _deployment_plan():
    plan = _plan()
    plan.artifacts = tuple(
        SimpleNamespace(
            name=name,
            size=size,
            sha256=digest,
            crc32=crc32,
            load_address=address,
        )
        for name, size, digest, crc32, address in (
            ("kernel", 101, "1" * 64, "11111111", 0x80200000),
            ("initramfs", 202, "2" * 64, "22222222", 0x83000000),
            ("megrez_dtb", 303, "3" * 64, "33333333", 0xF0000000),
        )
    )
    return plan


MMC_ARTIFACTS = {
    "kernel": "asterinas-current.Image",
    "initramfs": "asterinas-current-stage1.cpio",
    "megrez_dtb": "dtbs/linux/eswin/eic7700-milkv-megrez.dtb",
}


def _deployment_measurement_log(plan=None):
    plan = plan or _deployment_plan()
    identities = {identity.name: identity for identity in plan.artifacts}
    nonce = "4" * 32
    boot_id = "12345678-1234-1234-1234-123456789abc"
    lines = [
        "RockOS controlled measurement transcript",
        "__ASTERINAS_ROCKOS_MEASUREMENT_BEGIN__ "
        f"nonce={nonce} plan_sha256={plan.plan_sha256} "
        f"partition=/dev/mmcblk1p1 boot_id={boot_id} status=0",
    ]
    for name in ("kernel", "initramfs", "megrez_dtb"):
        lines.extend(
            (
                f"{identities[name].size} /boot/{MMC_ARTIFACTS[name]}",
                f"{identities[name].sha256}  /boot/{MMC_ARTIFACTS[name]}",
            )
        )
        lines.append(
            "__ASTERINAS_ROCKOS_ARTIFACT__ "
            f"nonce={nonce} name={name} mmc_path={MMC_ARTIFACTS[name]} "
            f"size={identities[name].size} sha256={identities[name].sha256} status=0"
        )
    lines.extend(
        (
            f"__ASTERINAS_ROCKOS_MEASUREMENT_END__ nonce={nonce} artifacts=3 status=0",
            "OpenSBI v1.5",
            "U-Boot 2024.01",
            "=> ",
        )
    )
    return ("\r\n".join(lines) + "\r\n").encode()


def _deployment_attestation(plan=None):
    plan = plan or _deployment_plan()
    identities = {identity.name: identity for identity in plan.artifacts}
    measurement_log = _deployment_measurement_log(plan)
    return gate.DeploymentAttestation(
        schema_version=2,
        plan_sha256=plan.plan_sha256,
        observer="rockos-sha256sum",
        partition="/dev/mmcblk1p1",
        rockos_boot_id="12345678-1234-1234-1234-123456789abc",
        measurement_nonce="4" * 32,
        measurement_log_sha256=hashlib.sha256(measurement_log).hexdigest(),
        rockos_recovered=True,
        artifacts=tuple(
            gate.AttestedArtifact(
                name=name,
                mmc_path=MMC_ARTIFACTS[name],
                size=identities[name].size,
                sha256=identities[name].sha256,
            )
            for name in ("kernel", "initramfs", "megrez_dtb")
        ),
    )


class BootStabilityLifecycleTests(unittest.TestCase):
    def _run(self, factory: _OperationsFactory):
        publisher = _Publisher()
        result = gate.run_boot_stability(
            _plan(),
            gate.BootStabilityConfig(),
            factory,
            publisher,
            artifact_validator=lambda _plan: None,
            clock=lambda: 10.0,
        )
        return result, publisher

    def test_pass_requires_three_ready_recovered_cycles(self) -> None:
        factory = _OperationsFactory()

        result, publisher = self._run(factory)

        self.assertTrue(result.passed)
        self.assertEqual(result.reason, "boot-stability-pass")
        self.assertEqual(result.completed_cycles, 3)
        self.assertTrue(all(cycle.recovered for cycle in result.cycles))
        self.assertEqual(
            sum(event.startswith("collect-diagnostics-") for event in factory.events),
            3,
        )
        self.assertEqual(
            sum(event.startswith("request-reboot-") for event in factory.events),
            3,
        )
        self.assertEqual(publisher.events, ["invalidate", "publish:True"])

    def test_default_readiness_budget_covers_measured_cold_boot_variance(self) -> None:
        self.assertEqual(gate.BootStabilityConfig().readiness_timeout, 240.0)

    def test_frozen_mmc_gate_does_not_require_local_build_outputs(self) -> None:
        publisher = _Publisher()

        result = gate.run_boot_stability(
            _plan(),
            gate.BootStabilityConfig(),
            _OperationsFactory(),
            publisher,
            clock=lambda: 10.0,
        )

        self.assertTrue(result.passed)
        self.assertEqual(publisher.events, ["invalidate", "publish:True"])

    def test_failed_readiness_collects_diagnostics_recovers_and_stops(self) -> None:
        factory = _OperationsFactory(fail_readiness=2)

        result, publisher = self._run(factory)

        self.assertFalse(result.passed)
        self.assertEqual(result.completed_cycles, 1)
        self.assertIn("collect-diagnostics-2", factory.events)
        self.assertIn("request-reboot-2", factory.events)
        self.assertIn("recovery-2", factory.events)
        self.assertNotIn("open-3", factory.events)
        self.assertEqual(publisher.events, ["invalidate", "publish:False"])

    def test_fatal_diagnostics_cannot_publish_pass(self) -> None:
        for marker in (
            b"Kernel panic - not syncing\n",
            b"Uncaught panic:\n",
            b"oom-kill:constraint=CONSTRAINT_NONE\n",
            b"Killed process 17 (firefox)\n",
            b"blk_update_request: I/O error, dev mmcblk0\n",
            b"end_request: I/O error, dev mmcblk0\n",
        ):
            with self.subTest(marker=marker):
                factory = _OperationsFactory(diagnostics={2: marker})

                result, _publisher = self._run(factory)

                self.assertFalse(result.passed)
                self.assertEqual(result.reason, "cycle-2-fatal-diagnostics")
                self.assertEqual(result.completed_cycles, 1)
                self.assertIn("recovery-2", factory.events)

    def test_recovery_failure_cannot_publish_pass(self) -> None:
        factory = _OperationsFactory(recover=False)

        result, _publisher = self._run(factory)

        self.assertFalse(result.passed)
        self.assertEqual(result.completed_cycles, 0)
        self.assertIn("cycle-1", result.reason)
        self.assertNotIn("open-2", factory.events)

    def test_primary_and_recovery_failures_are_both_published(self) -> None:
        factory = _OperationsFactory(fail_readiness=1, recover=False)

        result, _publisher = self._run(factory)

        self.assertFalse(result.passed)
        self.assertIn("readiness", result.reason)
        self.assertIn("recovery", result.reason)
        self.assertEqual(len(result.attempts), 1)
        self.assertFalse(result.attempts[0].recovered)

    def test_early_failure_waits_through_the_software_reboot_deadline(self) -> None:
        class Clock:
            now = 100.0

            def __call__(self) -> float:
                return self.now

        clock = Clock()
        recovery_timeouts: list[float] = []

        class DeadlineRecoveryOperations(_Operations):
            def prove_boot_readiness(self, _timeout: float):
                clock.now += 60.0
                raise gate.HostGateError("injected early readiness failure")

            def request_reboot(self, _timeout: float) -> None:
                raise TimeoutError("guest shell stopped responding")

            def await_recovery(self, timeout: float) -> None:
                recovery_timeouts.append(timeout)
                remaining = gate.PHYSICAL_REBOOT_AFTER - 60.0
                if timeout < remaining + 180.0:
                    raise TimeoutError("host stopped before the kernel deadline")
                clock.now += remaining
                self._transcript += b"OpenSBI\nU-Boot\n=> \n"

        events: list[str] = []
        operations = DeadlineRecoveryOperations(
            1,
            events,
            fail_readiness=None,
            diagnostics={},
            recover=True,
        )
        publisher = _Publisher()

        result = gate.run_boot_stability(
            _plan(),
            gate.BootStabilityConfig(),
            lambda _cycle: operations,
            publisher,
            clock=clock,
        )

        self.assertFalse(result.passed)
        self.assertTrue(result.attempts[0].recovered)
        self.assertGreaterEqual(recovery_timeouts[0], 1020.0)


class BootStabilityProtocolTests(unittest.TestCase):
    NONCE = "0011223344556677"

    @staticmethod
    def _operations():
        operations = object.__new__(gate.RealBootCycleOperations)
        operations._serial = mock.Mock(transcript=b"")
        operations._serial.checkpoint.return_value = 0
        operations._guest_deadline = 100.0
        operations._logged_serial_bytes = 0
        operations._log = io.StringIO()
        return operations

    def test_boot_bootargs_remove_kernel_network_and_fixture_activation(self) -> None:
        bootargs = gate.boot_stability_bootargs(_plan()).split()

        self.assertFalse(any(token.startswith("asterinas.net=") for token in bootargs))
        self.assertFalse(
            any(token.startswith("asterinas.neighbor=") for token in bootargs)
        )
        self.assertFalse(
            any(
                token.startswith("systemd.setenv=ASTERINAS_DESKTOP_FIXTURE_")
                for token in bootargs
            )
        )
        self.assertIn("systemd.mask=asterinas-desktop-m5-network.service", bootargs)
        self.assertIn("systemd.setenv=ASTERINAS_BROWSER_WEB_BASIC_ONLY=1", bootargs)

    def test_boot_readiness_commands_are_short_and_input_independent(self) -> None:
        commands = gate.boot_readiness_commands(self.NONCE)
        command = " ".join(commands)

        self.assertNotIn("/dev/input", command)
        self.assertNotIn("network", command.lower())
        self.assertIn("/dev/fb0", command)
        self.assertIn("asterinas-browser-web.service", command)
        self.assertTrue(commands)
        self.assertLessEqual(
            max(len(item.encode()) for item in commands),
            gate.MAX_SERIAL_COMMAND_BYTES,
        )

    def test_diagnostics_commands_are_short_enough_for_the_serial_shell(self) -> None:
        commands = gate.boot_diagnostics_commands(self.NONCE)
        command = " ".join(commands)

        self.assertGreater(len(commands), 2)
        self.assertLessEqual(
            max(len(item.encode()) for item in commands),
            gate.MAX_SERIAL_COMMAND_BYTES,
        )
        self.assertIn("/proc/cmdline", command)
        self.assertIn("systemctl --failed", command)
        self.assertIn("ps -eo pid,ppid,stat", command)
        self.assertIn("systemctl status --no-pager --full", command)
        self.assertIn("journalctl -b --no-pager", command)
        self.assertIn("/home/asterinas/Xorg.0.log", command)
        self.assertIn("/home/asterinas/desktop-m5-session.log", command)
        self.assertIn("/home/asterinas/firefox-web-stderr.log", command)
        self.assertNotIn("tail -c", command)
        self.assertNotIn(
            'dmesg --color=never >>"$_asterinas_boot_diag" 2>&1 || true',
            command,
        )
        self.assertTrue(
            all(' >"$_asterinas_boot_diag.' in item for item in commands[1:-2])
        )
        self.assertIn(f"-le {gate.MAX_DIAGNOSTICS_BYTES}", command)
        for item in commands:
            syntax = subprocess.run(
                ["sh", "-n", "-c", item],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_parse_boot_readiness_accepts_complete_display_and_browser_state(
        self,
    ) -> None:
        evidence = gate.parse_boot_readiness_marker(
            f"__ASTERINAS_BOOT_PREFLIGHT__ nonce={self.NONCE} "
            "browser_pid=121 framebuffer=1 "
            "xorg_fbdev=1 openbox=1 firefox=1 browser_service=active "
            "browser_restarts=0",
            self.NONCE,
        )

        self.assertEqual(evidence.browser_pid, 121)
        self.assertTrue(evidence.xorg_fbdev)

        with self.assertRaisesRegex(gate.HostGateError, "nonce"):
            gate.parse_boot_readiness_marker(
                f"__ASTERINAS_BOOT_PREFLIGHT__ nonce={'f' * 16} "
                "browser_pid=121 framebuffer=1 xorg_fbdev=1 openbox=1 "
                "firefox=1 browser_service=active browser_restarts=0",
                self.NONCE,
            )

    def test_readiness_timeout_reports_the_last_incomplete_snapshot(self) -> None:
        operations = self._operations()
        marker = (
            f"__ASTERINAS_BOOT_PREFLIGHT__ nonce={self.NONCE} "
            "browser_pid=124 framebuffer=1 "
            "xorg_fbdev=0 openbox=0 firefox=1 browser_service=active "
            "browser_restarts=0"
        )
        successful_steps = len(gate.boot_readiness_commands(self.NONCE)) - 1

        with (
            mock.patch.object(gate.secrets, "token_hex", return_value=self.NONCE),
            mock.patch.object(gate, "validate_debug_console_readiness"),
            mock.patch.object(operations, "_quiesce_external_services"),
            mock.patch.object(
                operations,
                "_send_guest_step",
                side_effect=[None] * successful_steps
                + [TimeoutError("serial marker not seen")],
            ),
            mock.patch.object(operations, "_next_line", return_value=(marker, 1)),
            mock.patch.object(gate.time, "monotonic", return_value=10.0),
            mock.patch.object(gate.time, "sleep"),
            self.assertRaisesRegex(
                gate.HostGateError,
                "xorg_fbdev=0 openbox=0 firefox=1",
            ),
        ):
            operations.prove_boot_readiness(30)

        sent = b"".join(call.args[0] for call in operations._serial.send.call_args_list)
        self.assertIn(b"\x03\n", sent)

    def test_guest_step_aborts_and_retries_after_a_lost_acknowledgement(
        self,
    ) -> None:
        operations = self._operations()
        marker = f"__ASTERINAS_BOOT_STEP__ nonce={self.NONCE} step=readiness-1 status=0"

        with (
            mock.patch.object(
                operations,
                "_next_line",
                side_effect=[TimeoutError("lost ack"), (marker, 1)],
            ),
            mock.patch.object(gate.time, "monotonic", return_value=10.0),
        ):
            operations._send_guest_step(":", "readiness-1", self.NONCE, 100.0)

        sent = [call.args[0] for call in operations._serial.send.call_args_list]
        self.assertEqual(sent.count(b"\x03\n"), 1)
        self.assertEqual(sum(marker.encode() in payload for payload in sent), 2)

    def test_guest_step_rejects_an_explicit_command_failure(self) -> None:
        operations = self._operations()
        marker = (
            f"__ASTERINAS_BOOT_STEP__ nonce={self.NONCE} step=diagnostics-4 status=1"
        )

        with (
            mock.patch.object(operations, "_next_line", return_value=(marker, 1)),
            mock.patch.object(gate.time, "monotonic", return_value=10.0),
            self.assertRaisesRegex(gate.HostGateError, "diagnostics-4 failed"),
        ):
            operations._send_guest_step("false", "diagnostics-4", self.NONCE, 100.0)

    def test_collect_diagnostics_returns_only_nonce_delimited_payload(self) -> None:
        operations = self._operations()
        payload = b"kernel log\nfailed units\n"
        digest = hashlib.sha256(payload).hexdigest()
        acknowledgements = tuple(
            f"__ASTERINAS_BOOT_STEP__ nonce={self.NONCE} "
            f"step=diagnostics-{step} status=0"
            for step in range(1, len(gate.boot_diagnostics_commands(self.NONCE)))
        )
        lines = iter(
            acknowledgements
            + (
                f"__ASTERINAS_BOOT_DIAGNOSTICS_BEGIN__ nonce={self.NONCE} "
                f"size={len(payload)} sha256={digest}",
                base64.b64encode(payload).decode(),
                f"__ASTERINAS_BOOT_DIAGNOSTICS_END__ nonce={self.NONCE} status=0",
            )
        )
        with (
            mock.patch.object(gate.secrets, "token_hex", return_value=self.NONCE),
            mock.patch.object(
                operations,
                "_next_line",
                side_effect=lambda *args: (next(lines), args[1] + 1),
            ),
            mock.patch.object(operations, "_sync_serial_log"),
            mock.patch.object(gate.time, "monotonic", return_value=10.0),
        ):
            actual = operations.collect_diagnostics(30)

        self.assertEqual(actual, payload)
        sent = b" ".join(
            call.args[0] for call in operations._serial.send.call_args_list
        )
        self.assertIn(b"/proc/cmdline", sent)
        self.assertIn(b"dmesg", sent)
        self.assertIn(b"systemctl --failed", sent)

    def test_collect_diagnostics_rejects_digest_mismatch(self) -> None:
        operations = self._operations()
        payload = b"kernel log\n"
        acknowledgements = tuple(
            f"__ASTERINAS_BOOT_STEP__ nonce={self.NONCE} "
            f"step=diagnostics-{step} status=0"
            for step in range(1, len(gate.boot_diagnostics_commands(self.NONCE)))
        )
        lines = iter(
            acknowledgements
            + (
                f"__ASTERINAS_BOOT_DIAGNOSTICS_BEGIN__ nonce={self.NONCE} "
                f"size={len(payload)} sha256={'0' * 64}",
                base64.b64encode(payload).decode(),
                f"__ASTERINAS_BOOT_DIAGNOSTICS_END__ nonce={self.NONCE} status=0",
            )
        )
        with (
            mock.patch.object(gate.secrets, "token_hex", return_value=self.NONCE),
            mock.patch.object(
                operations,
                "_next_line",
                side_effect=lambda *args: (next(lines), args[1] + 1),
            ),
            mock.patch.object(operations, "_sync_serial_log"),
            mock.patch.object(gate.time, "monotonic", return_value=10.0),
            self.assertRaisesRegex(gate.HostGateError, "identity mismatch"),
        ):
            operations.collect_diagnostics(30)

    def test_collect_diagnostics_rejects_noncanonical_encoded_length_before_decode(
        self,
    ) -> None:
        operations = self._operations()
        payload = b"kernel log\n"
        acknowledgements = tuple(
            f"__ASTERINAS_BOOT_STEP__ nonce={self.NONCE} "
            f"step=diagnostics-{step} status=0"
            for step in range(1, len(gate.boot_diagnostics_commands(self.NONCE)))
        )
        lines = iter(
            acknowledgements
            + (
                f"__ASTERINAS_BOOT_DIAGNOSTICS_BEGIN__ nonce={self.NONCE} "
                f"size={len(payload)} sha256={hashlib.sha256(payload).hexdigest()}",
                base64.b64encode(payload).decode() + "AAAA",
                f"__ASTERINAS_BOOT_DIAGNOSTICS_END__ nonce={self.NONCE} status=0",
            )
        )
        with (
            mock.patch.object(gate.secrets, "token_hex", return_value=self.NONCE),
            mock.patch.object(
                operations,
                "_next_line",
                side_effect=lambda *args: (next(lines), args[1] + 1),
            ),
            mock.patch.object(gate.time, "monotonic", return_value=10.0),
            mock.patch.object(gate.base64, "b64decode") as decode,
            self.assertRaisesRegex(gate.HostGateError, "encoded length"),
        ):
            operations.collect_diagnostics(30)

        decode.assert_not_called()

    def test_request_reboot_requires_exact_acknowledgement(self) -> None:
        operations = self._operations()
        marker = f"__ASTERINAS_BOOT_REBOOT__ nonce={self.NONCE}"
        lines = iter(("unrelated", marker))
        with (
            mock.patch.object(gate.secrets, "token_hex", return_value=self.NONCE),
            mock.patch.object(
                operations,
                "_next_line",
                side_effect=lambda *args: (next(lines), args[1] + 1),
            ),
            mock.patch.object(operations, "_sync_serial_log"),
            mock.patch.object(gate.time, "monotonic", return_value=10.0),
        ):
            operations.request_reboot(30)

        command = operations._serial.send.call_args.args[0]
        self.assertIn(b"sync;", command)
        self.assertIn(b"reboot -f", command)
        self.assertIn(marker.encode(), command)

    def test_recovery_uses_opensbi_bytes_already_buffered_after_ack(self) -> None:
        operations = self._operations()
        operations._fd = 41
        operations._session = mock.Mock()
        operations._recovery_cursor = 0
        operations._serial.transcript = (
            b"__ASTERINAS_BOOT_REBOOT__ nonce=0011223344556677\n"
            b"OpenSBI v1.5\nU-Boot 2024.01\n"
            b"Hit any key to stop autoboot: 30\n=> "
        )
        operations._serial.wait_for.return_value = operations._serial.transcript

        operations.await_recovery(30)

        operations._serial.send.assert_called_once()
        self.assertEqual(operations._serial.send.call_args.args[0], b"\n")


class BootStabilityCliTests(unittest.TestCase):
    BASE_ARGUMENTS = (
        "/dev/serial/by-id/test",
        "--plan",
        "/tmp/plan.json",
        "--output-directory",
        "/tmp/evidence",
        "--deployment-attestation",
        "/tmp/deployment-attestation.json",
        "--deployment-measurement-log",
        "/tmp/deployment-measurement.serial.log",
    )

    def test_cli_requires_the_complete_mmc_deployment(self) -> None:
        with self.assertRaises(SystemExit):
            gate.parse_args(
                self.BASE_ARGUMENTS + ("--mmc-kernel", "asterinas-current.Image")
            )

    def test_cli_accepts_only_safe_mmc_artifact_names(self) -> None:
        values = gate.parse_args(
            self.BASE_ARGUMENTS
            + (
                "--mmc-kernel",
                "asterinas-current.Image",
                "--mmc-initramfs",
                "asterinas-current-stage1.cpio",
                "--mmc-dtb",
                "dtbs/linux/eswin/eic7700-milkv-megrez.dtb",
            )
        )
        self.assertEqual(values.mmc_kernel, "asterinas-current.Image")
        self.assertFalse(hasattr(values, "hdmi_capture"))
        with self.assertRaises(SystemExit):
            gate.parse_args(
                self.BASE_ARGUMENTS
                + (
                    "--mmc-kernel",
                    "kernel;reset",
                    "--mmc-initramfs",
                    "stage1.cpio",
                    "--mmc-dtb",
                    "board.dtb",
                )
            )

    def test_module_entrypoint_follows_the_lifecycle_definition(self) -> None:
        source = Path(gate.__file__).read_text()

        self.assertLess(
            source.index("def run_boot_stability("),
            source.index('if __name__ == "__main__":'),
        )


class BootStabilityPublicationTests(unittest.TestCase):
    MMC_ARTIFACTS = MMC_ARTIFACTS

    def test_publication_contains_deployment_logs_diagnostics_and_hashes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            output = (
                repository
                / "target"
                / "current-main-physical-graphics"
                / "physical"
                / "boot-stability"
            )
            plan = _deployment_plan()
            publisher = gate.RealBootStabilityPublisher(
                plan,
                output,
                self.MMC_ARTIFACTS,
                _deployment_attestation(plan),
                _deployment_measurement_log(plan),
                repository=repository,
            )

            result = gate.run_boot_stability(
                plan,
                gate.BootStabilityConfig(),
                _OperationsFactory(),
                publisher,
                artifact_validator=lambda _plan: None,
                clock=lambda: 10.0,
            )

            self.assertTrue(result.passed)
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {
                    "result.json",
                    "sha256sums.txt",
                    "deployment.json",
                    "deployment-attestation.json",
                    "deployment-measurement.serial.log",
                    "cycle-1.serial.log",
                    "cycle-1.diagnostics.log",
                    "cycle-2.serial.log",
                    "cycle-2.diagnostics.log",
                    "cycle-3.serial.log",
                    "cycle-3.diagnostics.log",
                },
            )
            deployment = json.loads((output / "deployment.json").read_text())
            self.assertEqual(deployment["plan_sha256"], plan.plan_sha256)
            self.assertEqual(
                deployment["artifacts"]["kernel"]["mmc_path"],
                "asterinas-current.Image",
            )
            self.assertNotIn("root_image", deployment["artifacts"])
            attestation = (output / "deployment-attestation.json").read_bytes()
            self.assertEqual(
                deployment["attestation_sha256"],
                hashlib.sha256(attestation).hexdigest(),
            )
            measurement = (output / "deployment-measurement.serial.log").read_bytes()
            self.assertEqual(
                deployment["measurement_log_sha256"],
                hashlib.sha256(measurement).hexdigest(),
            )
            sums = (output / "sha256sums.txt").read_text()
            self.assertIn("cycle-1.diagnostics.log", sums)
            self.assertIn("result.json", sums)

    def test_output_directory_is_exclusively_locked_for_one_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            output = (
                repository
                / "target"
                / "current-main-physical-graphics"
                / "physical"
                / "boot-stability"
            )
            plan = _deployment_plan()

            def new_publisher():
                return gate.RealBootStabilityPublisher(
                    plan,
                    output,
                    self.MMC_ARTIFACTS,
                    _deployment_attestation(plan),
                    _deployment_measurement_log(plan),
                    repository=repository,
                )

            first = new_publisher()
            second = new_publisher()
            self.addCleanup(lambda: getattr(first, "close", lambda: None)())
            self.addCleanup(lambda: getattr(second, "close", lambda: None)())
            first.invalidate()

            with self.assertRaisesRegex(gate.HostGateError, "already active"):
                second.invalidate()

    def test_attestation_must_match_plan_partition_and_mmc_names(self) -> None:
        plan = _deployment_plan()
        attestation = _deployment_attestation(plan)
        for replacement in (
            {"plan_sha256": "f" * 64},
            {"partition": "/dev/mmcblk1p2"},
        ):
            with self.subTest(replacement=replacement):
                values = {**attestation.__dict__, **replacement}
                with self.assertRaises(gate.HostGateError):
                    gate.DeploymentAttestation(**values).validate(
                        plan, self.MMC_ARTIFACTS, _deployment_measurement_log(plan)
                    )

        changed = dict(self.MMC_ARTIFACTS)
        changed["kernel"] = "different.Image"
        with self.assertRaisesRegex(gate.HostGateError, "MMC path"):
            attestation.validate(plan, changed, _deployment_measurement_log(plan))

        with self.assertRaisesRegex(gate.HostGateError, "measurement log identity"):
            attestation.validate(plan, self.MMC_ARTIFACTS, b"forged\n")

        malformed = json.loads(attestation.canonical_bytes())
        malformed["artifacts"][0]["sha256"] = None
        with self.assertRaises(gate.HostGateError):
            gate.DeploymentAttestation.from_mapping(malformed)

    def test_main_invalidates_stale_pass_before_reading_the_plan(self) -> None:
        repository = Path(gate.__file__).resolve().parents[2]
        physical = repository / "target/current-main-physical-graphics/physical"
        with tempfile.TemporaryDirectory(dir=physical) as temporary:
            output = Path(temporary)
            stale = output / "result.json"
            stale.write_text('{"passed":true}\n')

            with mock.patch.object(
                gate, "_read_plan", side_effect=gate.HostGateError("bad plan")
            ):
                status = gate.main(
                    (
                        "/dev/serial/by-id/test",
                        "--plan",
                        str(output / "bad-plan.json"),
                        "--output-directory",
                        str(output),
                        "--deployment-attestation",
                        str(output / "deployment-attestation.json"),
                        "--deployment-measurement-log",
                        str(output / "deployment-measurement.serial.log"),
                        "--mmc-kernel",
                        self.MMC_ARTIFACTS["kernel"],
                        "--mmc-initramfs",
                        self.MMC_ARTIFACTS["initramfs"],
                        "--mmc-dtb",
                        self.MMC_ARTIFACTS["megrez_dtb"],
                    )
                )

            self.assertEqual(status, 2)
            self.assertFalse(stale.exists())

    def test_attestation_requires_nonce_bound_ordered_raw_measurement(self) -> None:
        plan = _deployment_plan()
        measurement = _deployment_measurement_log(plan)
        attestation = _deployment_attestation(plan)

        attestation.validate(plan, self.MMC_ARTIFACTS, measurement)
        for changed in (
            measurement.replace(b"nonce=" + b"4" * 32, b"nonce=" + b"5" * 32, 1),
            measurement.replace(b" status=0", b" status=1", 1),
            measurement.replace(b"101 /boot/", b"100 /boot/", 1),
            measurement.replace(b"OpenSBI v1.5", b"missing recovery"),
        ):
            forged = gate.DeploymentAttestation(
                **{
                    **attestation.__dict__,
                    "measurement_log_sha256": hashlib.sha256(changed).hexdigest(),
                }
            )
            with self.subTest(changed=changed[-80:]):
                with self.assertRaises(gate.HostGateError):
                    forged.validate(plan, self.MMC_ARTIFACTS, changed)


class BootStabilityDocumentationTests(unittest.TestCase):
    def test_readme_documents_immutable_mmc_boot_stability_gate(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        readme = (repository / "tools/riscv/README.md").read_text()
        makefile = (repository / "Makefile").read_text()

        for required in (
            "## Megrez unattended boot stability",
            "RockOS",
            "partition 2 is never written",
            "test_riscv_megrez_boot_stability_unit",
            "tools.riscv.megrez_boot_stability",
            "tools.riscv.megrez_rockos_attestation",
            "deployment-measurement.serial.log",
            "deployment trust boundary",
            "cycle-1.diagnostics.log",
            "Run the gate directly on the host",
            "/run/asterinas-physical-home",
            "does not reread local build outputs",
            "process tree",
        ):
            self.assertIn(required, readme)
        self.assertIn("test_riscv_megrez_boot_stability_unit:", makefile)


if __name__ == "__main__":
    unittest.main()
