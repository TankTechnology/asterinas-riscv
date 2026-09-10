#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the configured Megrez desktop and Firefox diagnostic runner."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
