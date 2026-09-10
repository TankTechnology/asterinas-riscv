#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the configured Megrez desktop and Firefox diagnostic runner."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from tools.riscv import megrez_desktop as desktop


MMC_ARTIFACTS = {
    "kernel": "asterinas-current.Image",
    "initramfs": "asterinas-current-stage1.cpio",
    "megrez_dtb": "dtbs/linux/eswin/eic7700-milkv-megrez.dtb",
}


class DesktopBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.plan = self.root / "plan.json"
        self.attestation = self.root / "deployment-attestation.json"
        self.measurement = self.root / "deployment-measurement.serial.log"
        self.evidence = self.root / "evidence"
        self.destination = self.root / "current.json"
        self.plan.write_bytes(b'{"schema_version":2}\n')
        self.attestation.write_bytes(b'{"schema_version":2}\n')
        self.measurement.write_bytes(b"measured deployment\n")
        plan_digest = hashlib.sha256(self.plan.read_bytes()).hexdigest()
        self.plan_value = SimpleNamespace(
            plan_sha256=plan_digest,
            validate=lambda: None,
        )
        self.attestation_value = SimpleNamespace(plan_sha256=plan_digest)

    def configure(self, **overrides):
        arguments = {
            "plan_path": self.plan,
            "device": "/dev/serial/by-id/usb-test",
            "deployment_attestation_path": self.attestation,
            "deployment_measurement_log_path": self.measurement,
            "mmc_artifacts": MMC_ARTIFACTS,
            "evidence_root": self.evidence,
            "destination": self.destination,
        }
        arguments.update(overrides)
        with (
            mock.patch.object(
                desktop, "_read_plan", return_value=self.plan_value
            ) as read_plan,
            mock.patch.object(
                desktop,
                "_read_deployment_attestation",
                return_value=self.attestation_value,
            ) as read_attestation,
        ):
            bundle = desktop.configure_bundle(**arguments)
        read_plan.assert_called_once_with(self.plan)
        read_attestation.assert_called_once_with(self.attestation)
        return bundle

    def test_configure_round_trips_one_private_strict_bundle(self) -> None:
        bundle = self.configure()

        self.assertEqual(desktop.DesktopBundle.from_path(self.destination), bundle)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(bundle.schema_version, 1)
        self.assertEqual(bundle.device, "/dev/serial/by-id/usb-test")
        self.assertEqual(bundle.mmc_artifacts, MMC_ARTIFACTS)
        self.assertEqual(bundle.evidence_root, str(self.evidence))
        self.assertEqual(self.destination.read_bytes(), bundle.canonical_bytes())

    def test_bundle_detects_changed_bound_inputs(self) -> None:
        self.configure()

        self.plan.write_bytes(b'{"schema_version":3}\n')

        with self.assertRaisesRegex(ValueError, "plan.*changed"):
            desktop.DesktopBundle.from_path(self.destination)

    def test_bundle_rejects_unknown_duplicate_and_boolean_schema_fields(self) -> None:
        bundle = self.configure()
        value = json.loads(bundle.canonical_bytes())

        value["unknown"] = 1
        self.destination.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "fields"):
            desktop.DesktopBundle.from_path(self.destination)

        encoded = bundle.canonical_bytes().decode().rstrip("\n")
        self.destination.write_text(
            encoded[:-1] + ',"schema_version":1}\n', encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            desktop.DesktopBundle.from_path(self.destination)

        value = json.loads(bundle.canonical_bytes())
        value["schema_version"] = True
        self.destination.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "schema"):
            desktop.DesktopBundle.from_path(self.destination)

    def test_configure_rejects_unsafe_paths_and_artifact_names(self) -> None:
        with self.assertRaisesRegex(ValueError, "serial device"):
            self.configure(device="ttyUSB0")

        unsafe = dict(MMC_ARTIFACTS)
        unsafe["kernel"] = "../kernel.Image"
        with self.assertRaisesRegex((ValueError, TypeError), "artifact"):
            self.configure(mmc_artifacts=unsafe)

        link = self.root / "plan-link.json"
        link.symlink_to(self.plan)
        with self.assertRaisesRegex(ValueError, "plan"):
            self.configure(plan_path=link)

    def test_failed_configuration_does_not_replace_previous_bundle(self) -> None:
        original = self.configure().canonical_bytes()
        self.measurement.unlink()

        with self.assertRaisesRegex(ValueError, "measurement"):
            self.configure()

        self.assertEqual(self.destination.read_bytes(), original)


def _start_plan():
    return SimpleNamespace(
        bootargs=(
            "console=ttyS0 console=hvc0 loglevel=debug init=/init "
            "asterinas.mmc-write-partition2 "
            "asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1 "
            "asterinas.neighbor=eic7700-rj45,10.100.16.1,00:11:22:33:44:55 "
            "asterinas.reboot_after=15 "
            "systemd.setenv=ASTERINAS_DESKTOP_FIXTURE_URL=http://example.test/ "
            "systemd.setenv=ASTERINAS_BROWSER_WEB_BASIC_ONLY=0 "
            "-- --root-init=systemd"
        ),
        plan_sha256="a" * 64,
        validate=lambda: None,
    )


class _StartPublisher:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.published = None

    def invalidate(self) -> None:
        self.events.append("invalidate")

    def publish(self, result, serial: bytes, diagnostics: bytes) -> None:
        self.events.append(f"publish:{result.reason}")
        self.published = (result, serial, diagnostics)


class _StartOperations:
    def __init__(
        self,
        events: list[str],
        *,
        fail_at: str | None = None,
        recover: bool = True,
    ) -> None:
        self.events = events
        self.fail_at = fail_at
        self.recover = recover
        self._guest_started = False
        self._transcript = b"Asterinas desktop serial\n"

    @property
    def guest_started(self) -> bool:
        return self._guest_started

    @property
    def transcript(self) -> bytes:
        return self._transcript

    def open(self, _timeout: float) -> None:
        self.events.append("open")
        if self.fail_at == "open":
            raise OSError("serial device unavailable")

    def ensure_artifacts(self, _plan, _timeout: float) -> tuple[str, ...]:
        self.events.append("ensure-artifacts")
        if self.fail_at == "ensure-artifacts":
            raise RuntimeError("artifact identity mismatch")
        return ("kernel:mmc", "initramfs:mmc", "megrez_dtb:mmc")

    def boot(self, _plan, bootargs: str, _timeout: float) -> None:
        self.events.append("boot")
        if "asterinas.reboot_after" in bootargs:
            raise AssertionError("desktop start must not arm a recovery timer")
        self._guest_started = True
        if self.fail_at == "boot":
            raise TimeoutError("boot marker timed out")

    def prove_boot_readiness(self, _timeout: float):
        self.events.append("readiness")
        if self.fail_at in {"readiness", "readiness-invalid-diagnostics"}:
            raise TimeoutError("desktop readiness timed out")
        return desktop.BootReadinessEvidence(
            browser_pid=41,
            framebuffer=True,
            xorg_fbdev=True,
            openbox=True,
            firefox=True,
            browser_service="active",
            browser_restarts=0,
        )

    def collect_diagnostics(self, _timeout: float) -> bytes:
        self.events.append("collect-diagnostics")
        if self.fail_at == "readiness-invalid-diagnostics":
            return "not bytes"
        return b"bounded failure diagnostics\n"

    def request_reboot(self, _timeout: float) -> None:
        self.events.append("request-reboot")

    def await_recovery(self, _timeout: float) -> None:
        self.events.append("recovery")
        if not self.recover:
            raise TimeoutError("fresh U-Boot prompt not observed")
        self._transcript += b"OpenSBI\nU-Boot\n=> \n"

    def close(self) -> None:
        self.events.append("close")


class DesktopStartLifecycleTests(unittest.TestCase):
    def run_start(
        self,
        *,
        fail_at: str | None = None,
        recover: bool = True,
    ):
        events: list[str] = []
        operations = _StartOperations(events, fail_at=fail_at, recover=recover)
        publisher = _StartPublisher(events)
        result = desktop.run_desktop_start(
            _start_plan(),
            desktop.DesktopStartConfig(),
            operations,
            publisher,
            clock=lambda: 10.0,
        )
        return result, events, publisher

    def test_success_publishes_readiness_and_leaves_guest_running(self) -> None:
        result, events, publisher = self.run_start()

        self.assertEqual(
            events,
            [
                "invalidate",
                "open",
                "ensure-artifacts",
                "boot",
                "readiness",
                "publish:desktop-ready",
                "close",
            ],
        )
        self.assertTrue(result.passed)
        self.assertFalse(result.recovered)
        self.assertNotIn("request-reboot", events)
        self.assertEqual(result.readiness.browser_pid, 41)
        self.assertEqual(publisher.published[1], b"Asterinas desktop serial\n")
        self.assertEqual(publisher.published[2], b"")

    def test_start_bootargs_are_local_read_only_and_leave_desktop_running(self) -> None:
        bootargs = desktop.desktop_start_bootargs(_start_plan())
        tokens = bootargs.split()

        self.assertEqual(tokens.count("console=tty0"), 1)
        self.assertEqual(tokens.count("loglevel=off"), 1)
        self.assertEqual(tokens.count("asterinas.klog_capture=info"), 1)
        self.assertEqual(tokens.count("--"), 1)
        self.assertEqual(
            tokens[tokens.index("--") + 1 :],
            ["--root-init=systemd", "--debug-console=isolated-root"],
        )
        self.assertIn("systemd.mask=asterinas-browser-web-evidence.service", tokens)
        self.assertIn("systemd.mask=asterinas-desktop-m5-network.service", tokens)
        self.assertIn("systemd.setenv=ASTERINAS_BROWSER_WEB_BASIC_ONLY=1", tokens)
        forbidden = (
            "asterinas.mmc_write_partition2",
            "asterinas.mmc-write-partition2",
            "asterinas.net=",
            "asterinas.neighbor=",
            "asterinas.reboot_after=",
            "systemd.setenv=ASTERINAS_DESKTOP_FIXTURE_",
        )
        self.assertFalse(any(token.startswith(forbidden) for token in tokens))
        self.assertFalse(
            any(token in {"console=ttyS0", "console=hvc0"} for token in tokens)
        )

    def test_post_boot_failure_collects_diagnostics_and_recovers(self) -> None:
        result, events, publisher = self.run_start(fail_at="readiness")

        self.assertEqual(
            events,
            [
                "invalidate",
                "open",
                "ensure-artifacts",
                "boot",
                "readiness",
                "collect-diagnostics",
                "request-reboot",
                "recovery",
                f"publish:{result.reason}",
                "close",
            ],
        )
        self.assertFalse(result.passed)
        self.assertTrue(result.recovered)
        self.assertIn("readiness", result.failure)
        self.assertEqual(publisher.published[2], b"bounded failure diagnostics\n")

    def test_preboot_failure_never_sends_guest_recovery_commands(self) -> None:
        result, events, _publisher = self.run_start(fail_at="open")

        self.assertFalse(result.passed)
        self.assertFalse(result.recovered)
        self.assertEqual(
            events,
            ["invalidate", "open", f"publish:{result.reason}", "close"],
        )
        self.assertNotIn("collect-diagnostics", events)
        self.assertNotIn("request-reboot", events)
        self.assertNotIn("recovery", events)

    def test_invalid_diagnostics_cannot_skip_recovery_publication_or_close(
        self,
    ) -> None:
        result, events, publisher = self.run_start(
            fail_at="readiness-invalid-diagnostics"
        )

        self.assertFalse(result.passed)
        self.assertTrue(result.recovered)
        self.assertIn("diagnostics", result.failure)
        self.assertEqual(publisher.published[2], b"")
        self.assertEqual(
            events[-4:],
            ["request-reboot", "recovery", f"publish:{result.reason}", "close"],
        )

    def test_firmware_recovery_loss_requires_manual_reset(self) -> None:
        result, events, _publisher = self.run_start(fail_at="readiness", recover=False)

        self.assertFalse(result.passed)
        self.assertFalse(result.recovered)
        self.assertEqual(result.reason, "manual-reset-required")
        self.assertIn("fresh-u-boot-prompt", result.failure)
        self.assertIn("request-reboot", events)
        self.assertIn("recovery", events)

    def test_deadlines_reject_boolean_nonfinite_and_unbounded_values(self) -> None:
        for value in (True, 0, float("inf"), 1200.1):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "deadlines"):
                    desktop.DesktopStartConfig(open_timeout=value)


def _transport_record(
    *,
    pid: int,
    monotonic_ns: int,
    request_id: int,
    command: str,
    event: str,
    stage: str,
    send_complete: bool = False,
    header_bytes: int = 0,
    body_expected: int | None = None,
    body_received: int = 0,
    error_type: str | None = None,
    errno: int | None = None,
) -> str:
    value = {
        "version": 1,
        "event": event,
        "pid": pid,
        "monotonic_ns": monotonic_ns,
        "request_id": request_id,
        "command": command,
        "stage": stage,
        "send_complete": send_complete,
        "header_bytes": header_bytes,
        "body_expected": body_expected,
        "body_received": body_received,
    }
    if event == "failure":
        value.update(error_type=error_type or "TimeoutError", errno=errno)
    return (
        "A_WEB_MARIONETTE_TRANSPORT " + json.dumps(value, separators=(",", ":")) + "\n"
    )


def _greeting(pid: int, monotonic_ns: int) -> list[str]:
    return [
        _transport_record(
            pid=pid,
            monotonic_ns=monotonic_ns,
            request_id=0,
            command="greeting",
            event="frame_header",
            stage="response_body",
            header_bytes=3,
            body_expected=52,
        )
    ]


def _complete_command(
    pid: int,
    command: str,
    start_ns: int,
    *,
    body_bytes: int = 24,
) -> list[str]:
    return [
        _transport_record(
            pid=pid,
            monotonic_ns=start_ns,
            request_id=1,
            command=command,
            event="begin",
            stage="send",
        ),
        _transport_record(
            pid=pid,
            monotonic_ns=start_ns + 10,
            request_id=1,
            command=command,
            event="send_complete",
            stage="send",
            send_complete=True,
        ),
        _transport_record(
            pid=pid,
            monotonic_ns=start_ns + 20,
            request_id=1,
            command=command,
            event="frame_header",
            stage="response_body",
            send_complete=True,
            header_bytes=3,
            body_expected=body_bytes,
        ),
        _transport_record(
            pid=pid,
            monotonic_ns=start_ns + 50,
            request_id=1,
            command=command,
            event="complete",
            stage="complete",
            send_complete=True,
            header_bytes=3,
            body_expected=body_bytes,
            body_received=body_bytes,
        ),
    ]


def _status_complete() -> list[str]:
    return [*_greeting(11, 100), *_complete_command(11, "WebDriver:Status", 200)]


class FirefoxBoundaryClassifierTests(unittest.TestCase):
    def classify(self, lines: list[str]):
        return desktop.classify_new_session_transcript(
            "serial preface\n" + "".join(lines) + "shell trailer\n"
        )

    def test_explicit_greeting_failure_is_listener_not_ready(self) -> None:
        evidence = self.classify(
            [
                _transport_record(
                    pid=11,
                    monotonic_ns=100,
                    request_id=0,
                    command="greeting",
                    event="failure",
                    stage="response_header",
                )
            ]
        )

        self.assertEqual(evidence.boundary, "listener-not-ready")
        self.assertFalse(evidence.status_complete)
        self.assertIsNone(evidence.new_session_request_id)

    def test_status_failure_is_status_command_stalled(self) -> None:
        lines = _greeting(11, 100)
        lines.extend(
            (
                _transport_record(
                    pid=11,
                    monotonic_ns=200,
                    request_id=1,
                    command="WebDriver:Status",
                    event="begin",
                    stage="send",
                ),
                _transport_record(
                    pid=11,
                    monotonic_ns=210,
                    request_id=1,
                    command="WebDriver:Status",
                    event="failure",
                    stage="response_header",
                    send_complete=True,
                ),
            )
        )

        evidence = self.classify(lines)

        self.assertEqual(evidence.boundary, "status-command-stalled")
        self.assertFalse(evidence.status_complete)

    def test_status_complete_without_new_session_is_not_sent(self) -> None:
        evidence = self.classify(_status_complete())

        self.assertEqual(evidence.boundary, "new-session-not-sent")
        self.assertTrue(evidence.status_complete)
        self.assertFalse(evidence.send_complete)

    def test_new_session_begin_without_send_is_not_sent(self) -> None:
        lines = _status_complete() + _greeting(22, 300)
        lines.append(
            _transport_record(
                pid=22,
                monotonic_ns=400,
                request_id=1,
                command="WebDriver:NewSession",
                event="begin",
                stage="send",
            )
        )

        evidence = self.classify(lines)

        self.assertEqual(evidence.boundary, "new-session-not-sent")
        self.assertEqual(evidence.new_session_request_id, 1)

    def test_send_complete_with_zero_header_bytes_is_response_absent(self) -> None:
        lines = _status_complete() + _greeting(22, 300)
        lines.extend(
            (
                _transport_record(
                    pid=22,
                    monotonic_ns=400,
                    request_id=1,
                    command="WebDriver:NewSession",
                    event="begin",
                    stage="send",
                ),
                _transport_record(
                    pid=22,
                    monotonic_ns=410,
                    request_id=1,
                    command="WebDriver:NewSession",
                    event="send_complete",
                    stage="send",
                    send_complete=True,
                ),
                _transport_record(
                    pid=22,
                    monotonic_ns=500,
                    request_id=1,
                    command="WebDriver:NewSession",
                    event="failure",
                    stage="response_header",
                    send_complete=True,
                ),
            )
        )

        evidence = self.classify(lines)

        self.assertEqual(evidence.boundary, "new-session-response-absent")
        self.assertEqual(evidence.new_session_request_id, 1)
        self.assertTrue(evidence.send_complete)
        self.assertEqual(evidence.response_header_bytes, 0)
        self.assertEqual(evidence.selected_command_seconds, 0.0000001)

    def test_partial_header_and_partial_body_are_response_partial(self) -> None:
        cases = (
            dict(stage="response_header", header_bytes=2),
            dict(
                stage="response_body",
                header_bytes=3,
                body_expected=50,
                body_received=3,
            ),
        )
        for progress in cases:
            with self.subTest(progress=progress):
                lines = _status_complete() + _greeting(22, 300)
                lines.extend(
                    (
                        _transport_record(
                            pid=22,
                            monotonic_ns=400,
                            request_id=1,
                            command="WebDriver:NewSession",
                            event="begin",
                            stage="send",
                        ),
                        _transport_record(
                            pid=22,
                            monotonic_ns=410,
                            request_id=1,
                            command="WebDriver:NewSession",
                            event="send_complete",
                            stage="send",
                            send_complete=True,
                        ),
                        _transport_record(
                            pid=22,
                            monotonic_ns=500,
                            request_id=1,
                            command="WebDriver:NewSession",
                            event="failure",
                            send_complete=True,
                            **progress,
                        ),
                    )
                )

                evidence = self.classify(lines)

                self.assertEqual(evidence.boundary, "new-session-response-partial")
                self.assertEqual(
                    evidence.response_body_received,
                    progress.get("body_received", 0),
                )

    def test_complete_response_reports_guest_selected_duration(self) -> None:
        lines = _status_complete() + _greeting(22, 300)
        lines.extend(_complete_command(22, "WebDriver:NewSession", 400, body_bytes=80))

        evidence = self.classify(lines)

        self.assertEqual(evidence.boundary, "new-session-complete")
        self.assertTrue(evidence.status_complete)
        self.assertEqual(evidence.response_body_expected, 80)
        self.assertEqual(evidence.response_body_received, 80)
        self.assertEqual(evidence.selected_command_seconds, 0.00000005)

    def test_old_failure_without_transport_records_is_only_incomplete(self) -> None:
        evidence = desktop.classify_new_session_transcript(
            "ASTERINAS_PHYSICAL_SETUP cycle=1 phase=WebDriver:NewSession state=start\n"
            "physical graphics gate failed: timeout\n"
        )

        self.assertEqual(evidence.boundary, "evidence-incomplete")
        self.assertFalse(evidence.send_complete)
        self.assertEqual(evidence.response_header_bytes, 0)

    def test_reordered_duplicate_malformed_and_wrong_records_are_rejected(self) -> None:
        complete_new_session = _complete_command(22, "WebDriver:NewSession", 400)
        invalid_cases = {
            "request order": _greeting(22, 300) + complete_new_session,
            "duplicate terminal": (
                _status_complete()
                + _greeting(22, 300)
                + complete_new_session
                + [complete_new_session[-1]]
            ),
            "malformed JSON": ['A_WEB_MARIONETTE_TRANSPORT {"version":1\n'],
            "oversized": [
                "A_WEB_MARIONETTE_TRANSPORT "
                + " " * (desktop.MAX_TRANSPORT_RECORD_BYTES + 1)
                + "\n"
            ],
            "wrong command": [
                _transport_record(
                    pid=11,
                    monotonic_ns=100,
                    request_id=1,
                    command="WebDriver:GetTitle",
                    event="begin",
                    stage="send",
                )
            ],
            "contradictory": [
                _transport_record(
                    pid=11,
                    monotonic_ns=100,
                    request_id=0,
                    command="greeting",
                    event="failure",
                    stage="response_header",
                    header_bytes=2,
                    body_received=1,
                )
            ],
        }
        for message, lines in invalid_cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(desktop.HostGateError, message):
                    self.classify(lines)
        with self.assertRaisesRegex(desktop.HostGateError, "truncated"):
            desktop.classify_new_session_transcript(_greeting(11, 100)[0].rstrip("\n"))


class FirefoxGuestCommandTests(unittest.TestCase):
    NONCES = {
        "before": "1" * 16,
        "during": "2" * 16,
        "after": "3" * 16,
    }

    def test_commands_are_short_ordered_read_only_and_payload_free(self) -> None:
        commands = desktop.firefox_diagnostic_commands(41, self.NONCES)
        joined = "\n".join(commands)

        self.assertTrue(commands)
        self.assertTrue(
            all(len((command + "\n").encode()) <= 768 for command in commands)
        )
        for command in commands:
            subprocess.run(
                ["/bin/sh", "-n", "-c", command],
                check=True,
                capture_output=True,
                text=True,
            )
        self.assertIn(
            "systemctl show --property MainPID --value asterinas-browser-web.service",
            joined,
        )
        self.assertIn(
            "systemctl show --property NRestarts --value asterinas-browser-web.service",
            joined,
        )
        self.assertIn("/proc/$_asterinas_firefox_pid/stat", joined)
        self.assertIn("/home/asterinas/.mozilla/asterinas-browser-web", joined)
        status_index = next(
            index for index, command in enumerate(commands) if "status_once" in command
        )
        new_session_index = next(
            index
            for index, command in enumerate(commands)
            if "WebDriver:NewSession" in command
        )
        self.assertLess(status_index, new_session_index)
        self.assertEqual(joined.count("ASTERINAS_MARIONETTE_DIAGNOSTICS=1"), 2)
        self.assertNotIn("ASTERINAS_MARIONETTE_DEBUG_ERRORS", joined)
        self.assertIn("time.monotonic()+300", joined)
        self.assertIn(
            '{"pageLoadStrategy":"none","strictFileInteractability":True}',
            joined,
        )
        self.assertNotIn("acceptInsecureCerts", joined)
        background_index = next(
            index
            for index, command in enumerate(commands)
            if "sleep 5" in command and "&" in command
        )
        self.assertLess(background_index, new_session_index)
        self.assertIn('wait "$_asterinas_firefox_during_job"', joined)
        for phase, nonce in self.NONCES.items():
            self.assertIn(f".{phase}", joined)
            self.assertIn(f"phase={phase} nonce={nonce}", joined)
        for expected in (
            "/usr/lib/asterinas/firefox-diagnostic-snapshot",
            "--max-seconds 3",
            "--max-processes 16",
            "--max-threads 128",
            "--max-fds 64",
            "--max-file-bytes 8192",
            "--max-total-bytes 262144",
            "base64 -w 0",
            "sha256sum",
            "rm -f --",
        ):
            self.assertIn(expected, joined)
        lowered = joined.lower()
        for forbidden in (
            "password",
            "credential",
            "apt ",
            "cargo ",
            "nix ",
            "rockos",
            "u-boot",
            "fw_setenv",
            "mount ",
            "mmc write",
        ):
            self.assertNotIn(forbidden, lowered)

    def test_commands_reject_bad_pid_nonce_identity_and_deadline(self) -> None:
        invalid_nonces = dict(self.NONCES)
        invalid_nonces["during"] = invalid_nonces["before"]
        for pid, nonces, timeout in (
            (1, self.NONCES, 300),
            (True, self.NONCES, 300),
            (41, invalid_nonces, 300),
            (41, {"before": "1" * 16}, 300),
            (41, self.NONCES, 300.1),
        ):
            with self.subTest(pid=pid, nonces=nonces, timeout=timeout):
                with self.assertRaises((ValueError, desktop.HostGateError)):
                    desktop.firefox_diagnostic_commands(
                        pid, nonces, selected_timeout=timeout
                    )

    def frame(
        self,
        value: object,
        *,
        phase: str = "before",
        nonce: str = "1" * 16,
    ) -> str:
        payload = json.dumps(value, separators=(",", ":")).encode()
        return self.frame_payload(payload, phase=phase, nonce=nonce)

    def frame_payload(
        self,
        payload: bytes,
        *,
        phase: str = "before",
        nonce: str = "1" * 16,
    ) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        return (
            "serial preface\n"
            f"__ASTERINAS_FIREFOX_SNAPSHOT_BEGIN__ phase={phase} nonce={nonce} "
            f"size={len(payload)} sha256={digest}\n"
            + base64.b64encode(payload).decode()
            + "\n"
            f"__ASTERINAS_FIREFOX_SNAPSHOT_END__ phase={phase} nonce={nonce} status=0\n"
        )

    def snapshot(self) -> dict[str, object]:
        return {
            "version": 1,
            "physical": False,
            "root_pid": 41,
            "root_identity": {"pid": 41, "ppid": 1, "start_time_ticks": 100},
            "duration_seconds": 0.25,
            "bytes_read": 128,
            "complete": True,
            "limitations": [],
            "processes": [],
        }

    def test_snapshot_frame_verifies_identity_before_parsing_json(self) -> None:
        value = self.snapshot()

        parsed = desktop.parse_firefox_snapshot_frame(
            self.frame(value), "before", "1" * 16, expected_root_pid=41
        )

        self.assertEqual(parsed, value)

    def test_snapshot_frame_rejects_bad_hash_size_json_and_duplicate_key(self) -> None:
        payload = json.dumps(self.snapshot(), separators=(",", ":")).encode()
        valid = self.frame_payload(payload)
        digest = hashlib.sha256(payload).hexdigest()
        duplicate = payload.replace(b'"version":1', b'"version":1,"version":1')
        invalid = {
            "identity": valid.replace(digest, "0" * 64),
            "size": valid.replace(f"size={len(payload)}", "size=999999"),
            "JSON": self.frame("not an object"),
            "duplicate": self.frame_payload(duplicate),
        }
        for message, transcript in invalid.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(desktop.HostGateError, message):
                    desktop.parse_firefox_snapshot_frame(
                        transcript,
                        "before",
                        "1" * 16,
                        expected_root_pid=41,
                    )


if __name__ == "__main__":
    unittest.main()
