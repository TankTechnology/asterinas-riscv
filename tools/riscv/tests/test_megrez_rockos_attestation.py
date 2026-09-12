#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for controlled RockOS deployment measurement."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from tools.riscv import megrez_rockos_attestation as rockos
from tools.riscv.megrez_boot_manifest import ExtlinuxGeneration
from tools.riscv.megrez_debug_contract import ArtifactIdentity, DebugPlan


MMC_ARTIFACTS = {
    "kernel": "asterinas-current.Image",
    "initramfs": "asterinas-current-stage1.cpio",
    "megrez_dtb": "dtbs/linux/eswin/eic7700-milkv-megrez.dtb",
}


def _plan():
    return SimpleNamespace(
        plan_sha256="a" * 64,
        bootargs="console=ttyS0 init=/init",
        artifacts=tuple(
            SimpleNamespace(
                name=name,
                size=size,
                sha256=digest,
                crc32=crc32,
                load_address=load_address,
            )
            for name, size, digest, crc32, load_address in (
                ("kernel", 101, "1" * 64, "11111111", 0x80200000),
                ("initramfs", 202, "2" * 64, "22222222", 0x83000000),
                ("megrez_dtb", 303, "3" * 64, "33333333", 0xF0000000),
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


def _generation(plan=None):
    selected = plan or _plan()
    identities = {identity.name: identity for identity in selected.artifacts}
    label = f"asterinas-{selected.plan_sha256[:12]}"
    return ExtlinuxGeneration.from_bytes(
        (
            f"default {label}\n"
            f"label {label}\n"
            f"linux /asterinas-{identities['kernel'].sha256[:12]}.booti\n"
            f"initrd /stage1-{identities['initramfs'].sha256[:12]}.cpio\n"
            f"fdt /dtb/megrez-{identities['megrez_dtb'].sha256[:12]}.dtb\n"
            "append console=ttyS0 init=/init\n"
        ).encode()
    )


def _publication_log(nonce="4" * 32):
    lines = [
        "__ASTERINAS_ROCKOS_PUBLISH_BEGIN__ "
        f"nonce={nonce} partition=/dev/mmcblk1p1 status=0"
    ]
    lines.extend(
        "__ASTERINAS_ROCKOS_PUBLISH_ITEM__ "
        f"nonce={nonce} name={name} status=0"
        for name in ("kernel", "initramfs", "megrez_dtb", "extlinux")
    )
    lines.append(
        "__ASTERINAS_ROCKOS_PUBLISH_END__ "
        f"nonce={nonce} artifacts=3 config=1 status=0"
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


class _PublicationOperations(_Operations):
    def __init__(self, transcript: bytes) -> None:
        super().__init__(b"")
        self.publication = transcript
        self.commands = ()

    def publish(self, commands, _password: str, _timeout: float) -> None:
        self.commands = tuple(commands)
        self.events.append("publish")

    @property
    def publication_transcript(self) -> bytes:
        return self.publication


class RockOsAttestationTests(unittest.TestCase):
    def test_publication_plan_reader_accepts_a_lightweight_probe_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            addresses = {
                "kernel": 0x80200000,
                "initramfs": 0x83000000,
                "qemu_dtb": 0xF0000000,
                "megrez_dtb": 0xF0000000,
            }
            artifacts = []
            for name, address in addresses.items():
                path = root / name
                path.write_bytes(name.encode())
                artifacts.append(ArtifactIdentity.from_path(name, path, address))
            plan = DebugPlan(
                schema_version=1,
                profile="tcp-probe",
                artifacts=tuple(artifacts),
                bootargs="console=ttyS0 init=/init asterinas.reboot_after=180",
                smp=4,
                sv39=True,
                markers=("ASTERINAS_PROBE_READY v=1 pid=1",),
                reboot_after=180,
            )
            path = root / "plan.json"
            path.write_bytes(plan.canonical_bytes())

            loaded = rockos._read_publication_plan(path)

            self.assertEqual(loaded, plan)

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

    def test_real_publication_enters_root_shell_before_recording_commands(self) -> None:
        class Session:
            def __init__(self) -> None:
                self.events = []

            def send(self, command: str) -> None:
                self.events.append(("send", command))

            def wait_for(self, expected: str, _timeout: float) -> None:
                self.events.append(("wait", expected))

            def wait_for_uboot_prompt(self, _timeout: float) -> None:
                self.events.append(("wait", "uboot"))

        session = Session()
        operations = rockos.RealRockOsAttestationOperations("/dev/unused")
        operations._session = session
        operations._log = io.StringIO("sudo setup output")

        operations.publish(("sudo -n true",), "secret", 10.0)

        self.assertEqual(
            session.events,
            [
                ("send", "sudo -k -s"),
                ("wait", "password for"),
                ("send", "secret"),
                ("wait", "# "),
                (
                    "send",
                    "PS1='__ASTERINAS_ROCKOS_ROOT_''PROMPT__ '; export PS1",
                ),
                ("wait", rockos.ROCKOS_ROOT_PROMPT),
                ("send", "sudo -n true"),
                ("wait", rockos.ROCKOS_ROOT_PROMPT),
            ],
        )
        self.assertEqual(operations._publication_start, len("sudo setup output"))

        session.events.clear()
        operations.reboot_and_recover("secret", 10.0)

        self.assertEqual(session.events, [("send", "reboot"), ("wait", "uboot")])

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

    def test_publication_is_retained_only_after_normal_recovery(self) -> None:
        plan = _plan()
        generation = _generation(plan)
        transcript = _publication_log()
        operations = _PublicationOperations(transcript)
        published = []

        receipt = rockos.run_rockos_publication(
            plan,
            generation,
            "http://10.100.19.216:18081/generation",
            "debian",
            "secret",
            rockos.RockOsAttestationConfig(),
            operations,
            lambda manifest, log: published.append((manifest, log)),
            nonce="4" * 32,
        )

        self.assertEqual(
            operations.events,
            ["open", "boot", "login:debian:6", "publish", "recover:6", "close"],
        )
        self.assertEqual(published, [(receipt, transcript)])
        self.assertIn(b'"extlinux"', receipt)
        self.assertIn(b'"plan_sha256"', receipt)

    def test_failed_publication_still_recovers_and_is_not_retained(self) -> None:
        failed = _publication_log().replace(
            b"name=initramfs status=0", b"name=initramfs status=1"
        )
        operations = _PublicationOperations(failed)
        published = []

        with self.assertRaisesRegex(rockos.HostGateError, "publication failed"):
            rockos.run_rockos_publication(
                _plan(),
                _generation(),
                "http://10.100.19.216:18081/generation",
                "debian",
                "secret",
                rockos.RockOsAttestationConfig(),
                operations,
                lambda *values: published.append(values),
                nonce="4" * 32,
            )

        self.assertEqual(published, [])
        self.assertEqual(operations.events[-2:], ["recover:6", "close"])

    def test_publication_publisher_atomically_retains_receipt_and_log(self) -> None:
        plan = _plan()
        receipt = rockos.publication_manifest_bytes(
            _generation(plan),
            plan,
            "http://10.100.19.216:18081/generation",
            "4" * 32,
        )
        transcript = _publication_log()
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            output = (
                repository
                / "target/current-main-physical-graphics/physical/publication"
            )
            publisher = rockos.RealRockOsPublicationPublisher(
                output, repository=repository
            )

            publisher(receipt, transcript)

            self.assertEqual(
                {path.name for path in output.iterdir()},
                {
                    "generation.json",
                    "publication.serial.log",
                    "sha256sums.txt",
                },
            )
            for line in (output / "sha256sums.txt").read_text().splitlines():
                digest, name = line.split("  ", 1)
                self.assertEqual(
                    hashlib.sha256((output / name).read_bytes()).hexdigest(),
                    digest,
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

    def test_cli_has_an_explicit_publication_action(self) -> None:
        values = rockos.parse_args(
            (
                "publish",
                "/dev/serial/by-id/usb-test",
                "--plan",
                "/plan.json",
                "--extlinux-config",
                "/asterinas.conf",
                "--staged-directory",
                "/staged",
                "--base-url",
                "http://10.100.19.216:18081/generation",
                "--output-directory",
                "target/current-main-physical-graphics/physical/publication",
            )
        )

        self.assertEqual(values.action, "publish")
        self.assertEqual(values.extlinux_config, Path("/asterinas.conf"))
        option_strings = {
            option
            for action in rockos.publication_argument_parser()._actions
            for option in action.option_strings
        }
        self.assertIn("--password-fd", option_strings)
        self.assertNotIn("--password", option_strings)


if __name__ == "__main__":
    unittest.main()
