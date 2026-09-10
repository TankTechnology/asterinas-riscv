#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for controlled RockOS deployment measurement."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from tools.riscv import megrez_rockos_attestation as rockos


MMC_ARTIFACTS = {
    "kernel": "asterinas-current.Image",
    "initramfs": "asterinas-current-stage1.cpio",
    "megrez_dtb": "dtbs/linux/eswin/eic7700-milkv-megrez.dtb",
}


def _plan():
    return SimpleNamespace(
        plan_sha256="a" * 64,
        artifacts=tuple(
            SimpleNamespace(name=name, size=size, sha256=digest)
            for name, size, digest in (
                ("kernel", 101, "1" * 64),
                ("initramfs", 202, "2" * 64),
                ("megrez_dtb", 303, "3" * 64),
            )
        ),
        validate=lambda: None,
    )


def _measurement_log(plan=None, nonce="4" * 32):
    plan = plan or _plan()
    identities = {identity.name: identity for identity in plan.artifacts}
    boot_id = "12345678-1234-1234-1234-123456789abc"
    lines = [
        "__ASTERINAS_ROCKOS_MEASUREMENT_BEGIN__ "
        f"nonce={nonce} plan_sha256={plan.plan_sha256} "
        f"partition=/dev/mmcblk1p1 boot_id={boot_id} status=0",
    ]
    for name in ("kernel", "initramfs", "megrez_dtb"):
        identity = identities[name]
        path = MMC_ARTIFACTS[name]
        lines.extend(
            (
                f"{identity.size} /boot/{path}",
                f"{identity.sha256}  /boot/{path}",
                "__ASTERINAS_ROCKOS_ARTIFACT__ "
                f"nonce={nonce} name={name} mmc_path={path} "
                f"size={identity.size} sha256={identity.sha256} status=0",
            )
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


class _Operations:
    def __init__(self, measurement: bytes) -> None:
        self.measurement = measurement
        self.events: list[str] = []

    def open(self, _timeout: float) -> None:
        self.events.append("open")

    def boot_rockos(self, _timeout: float) -> None:
        self.events.append("boot")

    def login(self, username: str, password: str, _timeout: float) -> None:
        self.events.append(f"login:{username}:{len(password)}")

    def measure(self, _plan, _artifacts, nonce: str, _timeout: float) -> None:
        self.events.append(f"measure:{nonce}")

    def reboot_and_recover(self, password: str, _timeout: float) -> None:
        self.events.append(f"recover:{len(password)}")

    @property
    def measurement_transcript(self) -> bytes:
        return self.measurement

    def close(self) -> None:
        self.events.append("close")


class RockOsAttestationTests(unittest.TestCase):
    def test_commands_expose_native_measurements_and_nonce_bound_frame(self) -> None:
        commands = rockos.measurement_commands(
            _plan().plan_sha256, MMC_ARTIFACTS, "4" * 32
        )
        script = "\n".join(commands)

        self.assertIn("findmnt -n -o SOURCE -- /boot", script)
        self.assertIn("stat -c '%s %n' --", script)
        self.assertIn("sha256sum --", script)
        self.assertIn("__ASTERINAS_ROCKOS_MEASUREMENT_BEGIN__", script)
        self.assertIn("__ASTERINAS_ROCKOS_MEASUREMENT_END__", script)
        self.assertNotIn("password", script.lower())
        self.assertLess(max(map(len, commands)), rockos.MAX_ROCKOS_COMMAND_BYTES)

    def test_run_publishes_only_after_measurement_and_fresh_recovery(self) -> None:
        plan = _plan()
        measurement = _measurement_log(plan)
        operations = _Operations(measurement)
        published = []

        attestation = rockos.run_rockos_attestation(
            plan,
            MMC_ARTIFACTS,
            "debian",
            "secret",
            rockos.RockOsAttestationConfig(),
            operations,
            lambda receipt, transcript: published.append((receipt, transcript)),
            nonce="4" * 32,
        )

        self.assertEqual(operations.events[-1], "close")
        self.assertEqual(published, [(attestation, measurement)])
        self.assertEqual(attestation.schema_version, 2)
        self.assertEqual(
            attestation.measurement_log_sha256,
            hashlib.sha256(measurement).hexdigest(),
        )

    def test_measurement_failure_still_attempts_normal_recovery(self) -> None:
        class FailedMeasurementOperations(_Operations):
            def measure(self, *_args) -> None:
                self.events.append("measure-failed")
                raise TimeoutError("measurement timed out")

        operations = FailedMeasurementOperations(b"")
        with self.assertRaisesRegex(TimeoutError, "measurement timed out"):
            rockos.run_rockos_attestation(
                _plan(),
                MMC_ARTIFACTS,
                "debian",
                "secret",
                rockos.RockOsAttestationConfig(),
                operations,
                lambda *_args: self.fail("failed measurement was published"),
                nonce="4" * 32,
            )

        self.assertEqual(
            operations.events,
            ["open", "boot", "login:debian:6", "measure-failed", "recover:6", "close"],
        )

    def test_atomic_publisher_retains_receipt_raw_log_and_hashes(self) -> None:
        plan = _plan()
        measurement = _measurement_log(plan)
        attestation = rockos.gate.DeploymentAttestation.from_measurement_log(
            measurement
        )
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            output = (
                repository
                / "target/current-main-physical-graphics/physical/rockos-attestation"
            )
            publisher = rockos.RealRockOsAttestationPublisher(
                output, repository=repository
            )

            publisher(attestation, measurement)

            self.assertEqual(
                {path.name for path in output.iterdir()},
                {
                    "deployment-attestation.json",
                    "deployment-measurement.serial.log",
                    "sha256sums.txt",
                },
            )
            self.assertEqual(
                json.loads((output / "deployment-attestation.json").read_text())[
                    "plan_sha256"
                ],
                plan.plan_sha256,
            )
            self.assertIn(
                hashlib.sha256(measurement).hexdigest(),
                (output / "sha256sums.txt").read_text(),
            )

    def test_publisher_locks_the_output_directory_for_the_complete_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            output = (
                repository
                / "target/current-main-physical-graphics/physical/rockos-attestation"
            )
            first = rockos.RealRockOsAttestationPublisher(output, repository=repository)
            second = rockos.RealRockOsAttestationPublisher(
                output, repository=repository
            )
            self.addCleanup(first.close)
            self.addCleanup(second.close)

            first.invalidate()
            with self.assertRaisesRegex(rockos.HostGateError, "already active"):
                second.invalidate()

    def test_cli_accepts_password_fd_but_no_password_argument(self) -> None:
        parser = rockos.argument_parser()
        option_strings = {
            option for action in parser._actions for option in action.option_strings
        }

        self.assertIn("--password-fd", option_strings)
        self.assertNotIn("--password", option_strings)


if __name__ == "__main__":
    unittest.main()
