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
        self.assertEqual(
            self.destination.read_bytes(), bundle.canonical_bytes()
        )

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


if __name__ == "__main__":
    unittest.main()
