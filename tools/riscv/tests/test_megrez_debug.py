"""Fast contracts for the simulation-first Megrez debug workflow."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tools.riscv.megrez_debug_contract import (
    MAX_ARTIFACT_BYTES,
    ArtifactIdentity,
    DebugContractError,
    DebugPlan,
    StageResult,
)

REPOSITORY_ROOT = Path(__file__).parents[3]


class MegrezDebugArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def test_identity_hashes_one_held_regular_file(self) -> None:
        artifact = self.directory / "kernel"
        replacement = self.directory / "replacement"
        payload = b"asterinas-megrez-kernel"
        artifact.write_bytes(payload)
        replacement.write_bytes(b"different-path-bytes")
        original_open = Path.open
        open_count = 0

        def replace_after_open(path: Path, *args: object, **kwargs: object):
            nonlocal open_count
            stream = original_open(path, *args, **kwargs)
            open_count += 1
            os.replace(replacement, artifact)
            return stream

        with mock.patch.object(Path, "open", new=replace_after_open):
            identity = ArtifactIdentity.from_path("kernel", artifact, 0x80200000)

        self.assertEqual(open_count, 1)
        self.assertEqual(identity.name, "kernel")
        self.assertEqual(identity.path, str(artifact.absolute()))
        self.assertEqual(identity.load_address, 0x80200000)
        self.assertEqual(identity.size, len(payload))
        self.assertEqual(identity.sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(identity.crc32, f"{zlib.crc32(payload):08x}")
        self.assertEqual(artifact.read_bytes(), b"different-path-bytes")

    def test_identity_rejects_non_regular_and_out_of_bounds_inputs(self) -> None:
        empty = self.directory / "empty"
        empty.touch()
        directory = self.directory / "directory"
        directory.mkdir()
        target = self.directory / "target"
        target.write_bytes(b"target")
        symlink = self.directory / "symlink"
        symlink.symlink_to(target)
        oversized = self.directory / "oversized"
        with oversized.open("wb") as stream:
            stream.truncate(MAX_ARTIFACT_BYTES + 1)

        for path, message in (
            (empty, "empty"),
            (directory, "regular non-symlink"),
            (symlink, "regular non-symlink"),
            (oversized, "64 MiB"),
        ):
            with (
                self.subTest(path=path.name),
                self.assertRaisesRegex(DebugContractError, message),
            ):
                ArtifactIdentity.from_path("kernel", path, 0x80200000)

    def test_identity_rejects_invalid_name_and_address(self) -> None:
        artifact = self.directory / "artifact"
        artifact.write_bytes(b"data")

        for name, address in (
            ("other", 0x80200000),
            ("kernel", 0),
            ("kernel", 0x80200001),
            ("kernel", True),
        ):
            with (
                self.subTest(name=name, address=address),
                self.assertRaises(DebugContractError),
            ):
                ArtifactIdentity.from_path(name, artifact, address)

    def test_identity_rejects_a_different_inode_opened_after_lstat(self) -> None:
        artifact = self.directory / "artifact"
        other = self.directory / "other"
        artifact.write_bytes(b"original")
        other.write_bytes(b"other")
        original_open = Path.open

        def open_other(_path: Path, *args: object, **kwargs: object):
            return original_open(other, *args, **kwargs)

        with (
            mock.patch.object(Path, "open", new=open_other),
            self.assertRaisesRegex(DebugContractError, "identity changed"),
        ):
            ArtifactIdentity.from_path("kernel", artifact, 0x80200000)


class MegrezDebugPlanTests(unittest.TestCase):
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
        self.artifacts = tuple(
            self._artifact(name, addresses[name])
            for name in ("kernel", "initramfs", "qemu_dtb", "megrez_dtb")
        )

    def _artifact(self, name: str, address: int) -> ArtifactIdentity:
        path = self.directory / name
        path.write_bytes(f"{name}-bytes".encode())
        return ArtifactIdentity.from_path(name, path, address)

    def _plan(self) -> DebugPlan:
        return DebugPlan(
            schema_version=1,
            profile="tcp-probe",
            artifacts=self.artifacts,
            bootargs=(
                "cpu_no_boost_1_6ghz loglevel=info init=/init "
                "asterinas.reboot_after=180"
            ),
            smp=4,
            sv39=True,
            markers=(
                "Enter riscv_boot",
                "Presented by the Asterinas developers",
                "ASTERINAS_GMAC_TCP_PROBE_READY",
            ),
            reboot_after=180,
        )

    def test_plan_round_trip_is_canonical_and_hash_bound(self) -> None:
        plan = self._plan()
        encoded = plan.canonical_bytes()

        self.assertTrue(encoded.endswith(b"\n"))
        self.assertEqual(encoded, DebugPlan.from_bytes(encoded).canonical_bytes())
        self.assertEqual(plan.plan_sha256, hashlib.sha256(encoded).hexdigest())
        self.assertEqual(
            tuple(artifact.name for artifact in plan.artifacts),
            ("kernel", "initramfs", "qemu_dtb", "megrez_dtb"),
        )
        self.assertEqual(plan.smp, 4)
        self.assertIs(plan.sv39, True)

    def test_plan_rejects_duplicate_keys_at_every_depth(self) -> None:
        valid = json.loads(self._plan().canonical_bytes())
        artifact = json.dumps(valid["artifacts"][0], separators=(",", ":"))
        duplicate_top = (
            self._plan()
            .canonical_bytes()
            .decode()
            .replace('{"artifacts":', '{"schema_version":1,"artifacts":', 1)
        )
        duplicate_nested_artifact = artifact.replace(
            '{"crc32":', '{"name":"kernel","crc32":', 1
        )
        nested = (
            self._plan()
            .canonical_bytes()
            .decode()
            .replace(artifact, duplicate_nested_artifact, 1)
        )

        for encoded in (duplicate_top.encode(), nested.encode()):
            with self.assertRaisesRegex(DebugContractError, "duplicate JSON key"):
                DebugPlan.from_bytes(encoded)

    def test_plan_rejects_wrong_architecture_and_unsafe_values(self) -> None:
        plan = self._plan()
        invalid = (
            replace(plan, schema_version=True),
            replace(plan, smp=2),
            replace(plan, sv39=False),
            replace(plan, reboot_after=True),
            replace(plan, reboot_after=0),
            replace(plan, bootargs="init=/init; saveenv"),
            replace(plan, markers=()),
            replace(plan, markers=("same", "same")),
            replace(plan, artifacts=tuple(reversed(plan.artifacts))),
            replace(
                plan,
                artifacts=(replace(plan.artifacts[0], sha256="0" * 63),)
                + plan.artifacts[1:],
            ),
            replace(
                plan,
                artifacts=(replace(plan.artifacts[0], crc32="xyzxyzxy"),)
                + plan.artifacts[1:],
            ),
        )

        for value in invalid:
            with self.subTest(value=value), self.assertRaises(DebugContractError):
                value.validate()

    def test_plan_loader_rejects_unknown_missing_and_wrongly_typed_fields(self) -> None:
        payload = json.loads(self._plan().canonical_bytes())
        variants: list[dict[str, object]] = []
        unknown = dict(payload)
        unknown["unknown"] = 1
        variants.append(unknown)
        missing = dict(payload)
        del missing["profile"]
        variants.append(missing)
        wrong_type = dict(payload)
        wrong_type["markers"] = "marker"
        variants.append(wrong_type)

        for value in variants:
            with self.assertRaises(DebugContractError):
                DebugPlan.from_bytes(json.dumps(value).encode())

    def test_stage_result_round_trip_binds_the_plan_hash(self) -> None:
        plan = self._plan()
        result = StageResult(
            schema_version=1,
            stage="fast",
            passed=True,
            reason="pass",
            plan_sha256=plan.plan_sha256,
            evidence=("serial.log", "result.json"),
        )

        encoded = result.canonical_bytes()

        self.assertEqual(result, StageResult.from_bytes(encoded))
        self.assertTrue(encoded.endswith(b"\n"))
        with self.assertRaises(DebugContractError):
            replace(result, plan_sha256="f" * 63).validate()


class MegrezDebugCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.artifact_directory = self.directory / "artifacts with spaces"
        self.artifact_directory.mkdir()
        self.artifacts: dict[str, Path] = {}
        for name in ("kernel", "initramfs", "qemu_dtb", "megrez_dtb"):
            path = self.artifact_directory / f"{name} image"
            path.write_bytes(f"{name}-payload".encode())
            self.artifacts[name] = path
        self.plan_path = self.directory / "plan output" / "debug-plan.json"

    def _run(self, *arguments: object) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(REPOSITORY_ROOT)
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.riscv.megrez_debug",
                *(str(value) for value in arguments),
            ],
            cwd=self.directory,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

    def _create_plan(self) -> DebugPlan:
        result = self._run(
            "plan",
            "--kernel",
            self.artifacts["kernel"],
            "--initramfs",
            self.artifacts["initramfs"],
            "--qemu-dtb",
            self.artifacts["qemu_dtb"],
            "--megrez-dtb",
            self.artifacts["megrez_dtb"],
            "--bootargs",
            ("cpu_no_boost_1_6ghz loglevel=info init=/init asterinas.reboot_after=180"),
            "--marker",
            "Enter riscv_boot",
            "--marker",
            "ASTERINAS_GMAC_TCP_PROBE_READY",
            "--output",
            self.plan_path,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return DebugPlan.from_bytes(self.plan_path.read_bytes())

    def test_plan_and_check_work_from_an_arbitrary_directory(self) -> None:
        plan = self._create_plan()

        self.assertEqual(stat.S_IMODE(self.plan_path.stat().st_mode), 0o644)
        self.assertEqual(plan.profile, "tcp-probe")
        self.assertEqual(plan.reboot_after, 180)
        check = self._run("check", self.plan_path)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertEqual(
            check.stdout,
            f"MEGREZ_DEBUG_CHECK_PASS plan={plan.plan_sha256}\n",
        )
        self.assertEqual(check.stderr, "")

    def test_plan_rejects_directory_and_symlink_outputs_without_mutation(self) -> None:
        output_directory = self.directory / "output-directory"
        output_directory.mkdir()
        protected = self.directory / "protected"
        protected.write_bytes(b"keep")
        output_symlink = self.directory / "output-symlink"
        output_symlink.symlink_to(protected)

        for output in (output_directory, output_symlink):
            with self.subTest(output=output.name):
                self.plan_path = output
                result = self._run(
                    "plan",
                    "--kernel",
                    self.artifacts["kernel"],
                    "--initramfs",
                    self.artifacts["initramfs"],
                    "--qemu-dtb",
                    self.artifacts["qemu_dtb"],
                    "--megrez-dtb",
                    self.artifacts["megrez_dtb"],
                    "--bootargs",
                    "init=/init asterinas.reboot_after=180",
                    "--marker",
                    "READY",
                    "--output",
                    output,
                )
                self.assertEqual(result.returncode, 2)
        self.assertEqual(protected.read_bytes(), b"keep")
        self.assertEqual(list(output_directory.iterdir()), [])

    def test_check_detects_artifact_drift(self) -> None:
        self._create_plan()
        self.artifacts["kernel"].write_bytes(b"changed-kernel")

        result = self._run("check", self.plan_path)

        self.assertEqual(result.returncode, 2)
        self.assertIn("plan-artifact-drift", result.stderr)

    def test_board_dry_run_is_complete_and_never_requires_serial(self) -> None:
        plan = self._create_plan()

        result = self._run(
            "board",
            self.plan_path,
            self.directory / "missing-serial-device",
            "--simulation-result",
            self.directory / "missing-result.json",
            "--dry-run",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [
                {"action": "require-simulation", "tier": "fast"},
                {"action": "probe-uboot-baud", "choices": [115200, 1500000]},
                {
                    "action": "cache-or-transfer",
                    "artifact": "kernel",
                    "address": 0x80200000,
                },
                {
                    "action": "cache-or-transfer",
                    "artifact": "initramfs",
                    "address": 0x83000000,
                },
                {
                    "action": "cache-or-transfer",
                    "artifact": "megrez_dtb",
                    "address": 0xF0000000,
                },
                {"action": "boot-once", "reboot_after": plan.reboot_after},
                {"action": "capture-markers"},
                {"action": "await-automatic-recovery"},
            ],
        )

    def test_board_refuses_missing_simulation_before_serial_access(self) -> None:
        self._create_plan()

        result = self._run(
            "board",
            self.plan_path,
            self.directory / "missing-serial-device",
            "--simulation-result",
            self.directory / "missing-result.json",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("plan-simulation-missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
