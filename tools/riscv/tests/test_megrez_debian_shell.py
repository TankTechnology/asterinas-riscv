#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest import mock
import zlib

from tools.riscv.megrez_debian_shell_contract import (
    P2_NR_SECTORS,
    P2_START_LBA,
    SHELL_ARTIFACT_ORDER,
    FrozenArtifact,
    PersistentShellPlan,
    ShellContractError,
    validate_rootfs_identity,
)
from tools.riscv.megrez_debian_shell_evidence import (
    QemuShellEvidence,
    ShellPermit,
    ShellPermitError,
    issue_shell_permit,
    validate_qemu_result,
)
from tools.riscv.megrez_debian_shell import qemu_gate_argv, run_qemu_gate
from tools.riscv.debian.rootfs.gate_protocol import shell_commands
from tools.riscv.megrez_debian_shell_board import (
    InventoryError,
    InventoryResult,
    classify_inventory_log,
    install_if_needed,
    installer_bootargs,
    run_inventory,
    verifier_bootargs,
)
from tools.riscv.megrez_board_session import PartitionGeometry
from tools.riscv.megrez_debug_contract import StageResult
from tools.riscv.megrez_preboard import PreboardError
from tools.riscv import megrez_debian_shell_physical_io as physical_io
from tools.riscv.megrez_debian_shell_physical import (
    PhysicalBoot,
    PhysicalShellResult,
    dnsmasq_tftp_argv,
    run_physical_gate,
)
from tools.riscv.megrez_debian_shell_physical_io import (
    PhysicalBoardOperations,
    TftpOnlyServer,
    _copy_verified,
)


ROOT_IMAGE_SIZE_BYTES = 1024 * 1024 * 1024


def _artifact_fixture(directory: Path) -> tuple[FrozenArtifact, ...]:
    artifacts = []
    for index, name in enumerate(SHELL_ARTIFACT_ORDER):
        path = directory / name
        if name == "root_image":
            with path.open("wb") as output_file:
                output_file.truncate(ROOT_IMAGE_SIZE_BYTES)
            size = ROOT_IMAGE_SIZE_BYTES
        else:
            payload = f"{name}-{index}\n".encode()
            path.write_bytes(payload)
            size = len(payload)
        artifacts.append(
            FrozenArtifact(
                name=name,
                path=str(path),
                size=size,
                sha256=f"{index + 1:064x}",
                crc32=f"{index + 1:08x}",
            )
        )
    return tuple(artifacts)


def _valid_plan(artifacts: tuple[FrozenArtifact, ...]) -> PersistentShellPlan:
    return PersistentShellPlan(
        schema_version=1,
        git_commit="1" * 40,
        artifacts=artifacts,
        smp=4,
        qemu_paging="sv39",
        megrez_paging="sv48",
        gate_bootargs=(
            "console=ttyS0 cpu_no_boost_1_6ghz loglevel=info init=/init "
            "asterinas.reboot_after=180"
        ),
        final_bootargs=("console=ttyS0 cpu_no_boost_1_6ghz loglevel=info init=/init"),
        gate_reboot_after=180,
        long_operation_reboot_after=600,
        partition_start_lba=P2_START_LBA,
        partition_nr_sectors=P2_NR_SECTORS,
    )


def _native_qemu_argv(directory: Path, boot_number: int) -> list[str]:
    runtime = directory / f"runtime-{boot_number}"
    return [
        "qemu-system-riscv64",
        "-machine",
        "virt",
        "-cpu",
        "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m",
        "2G",
        "-smp",
        "4",
        "-display",
        "none",
        "-nic",
        "none",
        "-serial",
        "stdio",
        "-no-reboot",
        "-kernel",
        str(runtime / "u-boot"),
        "-drive",
        f"if=none,format=raw,file={directory}/boot.ext4,id=bootdisk,readonly=on",
        "-device",
        "virtio-blk-device,drive=bootdisk",
        "-drive",
        f"if=none,format=raw,file={directory}/root.ext2,id=rootdisk,cache=directsync",
        "-device",
        "virtio-blk-device,drive=rootdisk",
        "-monitor",
        f"unix:{runtime}/monitor.sock,server=on,wait=off",
    ]


def _valid_permit(plan: PersistentShellPlan) -> ShellPermit:
    artifacts = plan.artifact_map()
    return ShellPermit(
        schema_version=1,
        passed=True,
        reason="pass",
        plan_sha256=plan.plan_sha256,
        qemu_evidence_sha256="a" * 64,
        git_commit=plan.git_commit,
        megrez_kernel_sha256=artifacts["megrez_kernel"].sha256,
        stage1_crc32=artifacts["stage1"].crc32,
        megrez_dtb_crc32=artifacts["megrez_dtb"].crc32,
        root_image_sha256=artifacts["root_image"].sha256,
        gate_bootargs=plan.gate_bootargs,
        gate_reboot_after=plan.gate_reboot_after,
        long_operation_reboot_after=plan.long_operation_reboot_after,
    )


class PersistentShellContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.artifacts = _artifact_fixture(self.directory)

    def valid_plan(self) -> PersistentShellPlan:
        return _valid_plan(self.artifacts)

    def test_plan_separates_sv39_qemu_from_sv48_megrez(self) -> None:
        plan = self.valid_plan()

        plan.validate()

        self.assertEqual(plan.qemu_paging, "sv39")
        self.assertEqual(plan.megrez_paging, "sv48")
        self.assertEqual(plan.smp, 4)
        self.assertEqual(
            tuple(item.name for item in plan.artifacts), SHELL_ARTIFACT_ORDER
        )

    def test_plan_rejects_wrong_fixed_identity_fields(self) -> None:
        plan = self.valid_plan()
        broken_plans = (
            replace(plan, schema_version=True),
            replace(plan, schema_version=2),
            replace(plan, qemu_paging="sv48"),
            replace(plan, megrez_paging="sv39"),
            replace(plan, git_commit="dirty"),
            replace(plan, git_commit="A" * 40),
            replace(plan, smp=2),
            replace(plan, partition_start_lba=P2_START_LBA + 1),
            replace(plan, partition_nr_sectors=P2_NR_SECTORS - 1),
            replace(plan, gate_reboot_after=181),
            replace(plan, long_operation_reboot_after=601),
        )

        for broken in broken_plans:
            with self.subTest(broken=broken):
                with self.assertRaises(ShellContractError):
                    broken.validate()

    def test_plan_rejects_unsafe_or_write_capable_boot_arguments(self) -> None:
        plan = self.valid_plan()
        broken_plans = (
            replace(plan, gate_bootargs=plan.gate_bootargs.replace("180", "179")),
            replace(
                plan, gate_bootargs=plan.gate_bootargs + " asterinas.reboot_after=180"
            ),
            replace(plan, gate_bootargs=plan.gate_bootargs + "; saveenv"),
            replace(plan, final_bootargs=plan.final_bootargs + "\nreset"),
            replace(
                plan, final_bootargs=plan.final_bootargs + " asterinas.reboot_after=180"
            ),
            replace(
                plan,
                final_bootargs=plan.final_bootargs + " asterinas.mmc_write_partition2",
            ),
        )

        for broken in broken_plans:
            with self.subTest(broken=broken):
                with self.assertRaises(ShellContractError):
                    broken.validate()

    def test_plan_rejects_invalid_artifact_identity_or_order(self) -> None:
        plan = self.valid_plan()
        first = plan.artifacts[0]
        symlink = self.directory / "kernel-link"
        symlink.symlink_to(first.path)
        invalid_artifacts = (
            (object(),) + plan.artifacts[1:],
            (replace(first, path="relative/kernel"),) + plan.artifacts[1:],
            (replace(first, path=str(symlink)),) + plan.artifacts[1:],
            (replace(first, size=0),) + plan.artifacts[1:],
            (replace(first, sha256="A" * 64),) + plan.artifacts[1:],
            (replace(first, crc32="0000000G"),) + plan.artifacts[1:],
            (plan.artifacts[1], plan.artifacts[0]) + plan.artifacts[2:],
            (plan.artifacts[0], plan.artifacts[0]) + plan.artifacts[2:],
        )

        for artifacts in invalid_artifacts:
            with self.subTest(artifacts=artifacts[:2]):
                with self.assertRaises(ShellContractError):
                    replace(plan, artifacts=artifacts).validate()

    def test_plan_json_is_exact_canonical_and_duplicate_key_safe(self) -> None:
        plan = self.valid_plan()

        payload = plan.canonical_bytes()
        decoded = json.loads(payload)

        self.assertEqual(PersistentShellPlan.from_bytes(payload), plan)
        self.assertEqual(
            PersistentShellPlan.from_bytes(payload).canonical_bytes(), payload
        )
        self.assertEqual(
            payload,
            json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode() + b"\n",
        )
        self.assertEqual(plan.plan_sha256, hashlib.sha256(payload).hexdigest())
        with self.assertRaisesRegex(ShellContractError, "duplicate JSON key"):
            PersistentShellPlan.from_bytes(
                payload.replace(b'"smp":4', b'"smp":4,"smp":4')
            )

    def test_plan_json_rejects_unknown_or_missing_nested_fields(self) -> None:
        document = json.loads(self.valid_plan().canonical_bytes())
        mutations = []
        missing_top = dict(document)
        del missing_top["smp"]
        mutations.append(missing_top)
        unknown_top = dict(document)
        unknown_top["extra"] = 1
        mutations.append(unknown_top)
        missing_artifact = json.loads(json.dumps(document))
        del missing_artifact["artifacts"][0]["crc32"]
        mutations.append(missing_artifact)
        unknown_artifact = json.loads(json.dumps(document))
        unknown_artifact["artifacts"][0]["extra"] = 1
        mutations.append(unknown_artifact)

        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(ShellContractError):
                    PersistentShellPlan.from_bytes(
                        json.dumps(mutation, separators=(",", ":")).encode()
                    )

    def test_from_path_hashes_one_regular_file_and_rejects_symlink(self) -> None:
        path = self.directory / "payload"
        payload = b"asterinas-debian-shell\n"
        path.write_bytes(payload)

        with mock.patch(
            "tools.riscv.megrez_debian_shell_contract.os.lstat",
            side_effect=AssertionError("path was reopened after hashing"),
        ):
            artifact = FrozenArtifact.from_path("stage1", path)

        self.assertEqual(artifact.path, str(path.absolute()))
        self.assertEqual(artifact.size, len(payload))
        self.assertEqual(artifact.sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(artifact.crc32, f"{zlib.crc32(payload):08x}")
        symlink = self.directory / "payload-link"
        symlink.symlink_to(path)
        with self.assertRaises(OSError):
            FrozenArtifact.from_path("stage1", symlink)

    def test_validate_rootfs_identity_binds_all_signed_sidecars(self) -> None:
        plan = self.valid_plan()
        root_artifact = plan.artifact_map()["root_image"]
        in_release_artifact = plan.artifact_map()["in_release"]
        downloaded_packages = (("bash", "riscv64", "1", "a" * 64),)
        manifest = SimpleNamespace(
            schema_version=1,
            profile="minimal-m1",
            root_image_sha256=root_artifact.sha256,
            signed_metadata_sha256=in_release_artifact.sha256,
            downloaded_packages=downloaded_packages,
        )

        with (
            mock.patch(
                "tools.riscv.megrez_debian_shell_contract.load_manifest",
                return_value=manifest,
            ) as load_manifest_mock,
            mock.patch(
                "tools.riscv.megrez_debian_shell_contract.validate_frozen_root",
                return_value=manifest,
            ) as validate_root_mock,
            mock.patch(
                "tools.riscv.megrez_debian_shell_contract.load_package_checksums",
                return_value=downloaded_packages,
            ) as load_checksums_mock,
        ):
            validated = validate_rootfs_identity(plan)

        self.assertIs(validated, manifest)
        load_manifest_mock.assert_called_once_with(
            Path(plan.artifact_map()["root_manifest"].path)
        )
        validate_root_mock.assert_called_once_with(
            Path(root_artifact.path),
            manifest,
            Path(plan.artifact_map()["packages_lock"].path),
        )
        load_checksums_mock.assert_called_once_with(
            Path(plan.artifact_map()["package_checksums"].path)
        )

    def test_validate_rootfs_identity_rejects_wrong_binding(self) -> None:
        plan = self.valid_plan()
        root_artifact = plan.artifact_map()["root_image"]
        in_release_artifact = plan.artifact_map()["in_release"]
        downloaded_packages = (("bash", "riscv64", "1", "a" * 64),)
        valid = {
            "schema_version": 1,
            "profile": "minimal-m1",
            "root_image_sha256": root_artifact.sha256,
            "signed_metadata_sha256": in_release_artifact.sha256,
            "downloaded_packages": downloaded_packages,
        }
        broken_values = (
            {**valid, "schema_version": 2},
            {**valid, "profile": "desktop-m5-network"},
            {**valid, "root_image_sha256": "f" * 64},
            {**valid, "signed_metadata_sha256": "e" * 64},
        )

        for values in broken_values:
            manifest = SimpleNamespace(**values)
            with (
                self.subTest(values=values),
                mock.patch(
                    "tools.riscv.megrez_debian_shell_contract.load_manifest",
                    return_value=manifest,
                ),
                mock.patch(
                    "tools.riscv.megrez_debian_shell_contract.validate_frozen_root",
                    return_value=manifest,
                ),
                mock.patch(
                    "tools.riscv.megrez_debian_shell_contract.load_package_checksums",
                    return_value=downloaded_packages,
                ),
            ):
                with self.assertRaises(ShellContractError):
                    validate_rootfs_identity(plan)

        manifest = SimpleNamespace(**valid)
        with (
            mock.patch(
                "tools.riscv.megrez_debian_shell_contract.load_manifest",
                return_value=manifest,
            ),
            mock.patch(
                "tools.riscv.megrez_debian_shell_contract.validate_frozen_root",
                return_value=manifest,
            ),
            mock.patch(
                "tools.riscv.megrez_debian_shell_contract.load_package_checksums",
                return_value=(("dash", "riscv64", "1", "b" * 64),),
            ),
        ):
            with self.assertRaises(ShellContractError):
                validate_rootfs_identity(plan)


class PersistentShellQemuPermitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.plan = _valid_plan(_artifact_fixture(self.directory))
        self.native_output = self.directory / "native"
        self.native_output.mkdir()
        self.native_result_path = self.native_output / "result.json"
        self.qemu_evidence_path = self.directory / "qemu-evidence.json"
        self.permit_path = self.directory / "permit.json"
        self._write_native_result(self._native_result())

    def _native_result(self) -> dict[str, object]:
        artifacts = self.plan.artifact_map()
        return {
            "passed": True,
            "reason": "pass",
            "nonce_sha256": "d" * 64,
            "qemu_argv": [
                _native_qemu_argv(self.native_output, 1),
                _native_qemu_argv(self.native_output, 2),
            ],
            "input_sha256": {
                "kernel": artifacts["qemu_kernel"].sha256,
                "u_boot": artifacts["qemu_uboot"].sha256,
                "dtb": artifacts["qemu_dtb"].sha256,
                "stage1_initramfs": artifacts["stage1"].sha256,
                "root_image": artifacts["root_image"].sha256,
                "manifest": artifacts["root_manifest"].sha256,
                "packages_lock": artifacts["packages_lock"].sha256,
                "package_checksums": artifacts["package_checksums"].sha256,
            },
            "final_root_sha256": "e" * 64,
            "manifest_identity": {
                "suite": "trixie",
                "architecture": "riscv64",
                "debian_release": "13.6",
                "root_image_sha256": artifacts["root_image"].sha256,
                "packages_lock_sha256": artifacts["packages_lock"].sha256,
            },
            "package_identity": [["bash", "5.2"]],
            "phase_durations_seconds": {
                "snapshot": 0.1,
                "validate": 0.1,
                "prepare": 0.1,
                "boot1": 1.0,
                "boot2": 1.0,
                "hash-final-root": 0.1,
            },
        }

    def _write_native_result(self, result: dict[str, object]) -> None:
        self.native_result_path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (self.native_output / "boot1.serial.log").write_bytes(b"boot one complete\n")
        (self.native_output / "boot2.serial.log").write_bytes(b"boot two complete\n")

    def _valid_evidence(self) -> QemuShellEvidence:
        return validate_qemu_result(self.plan, self.native_result_path)

    def test_qemu_result_requires_two_sv39_smp4_boots_and_exact_inputs(self) -> None:
        evidence = self._valid_evidence()

        self.assertTrue(evidence.passed)
        self.assertEqual(evidence.reason, "pass")
        self.assertEqual(evidence.plan_sha256, self.plan.plan_sha256)
        self.assertEqual(evidence.boot_count, 2)
        self.assertEqual(
            evidence.qemu_kernel_sha256,
            self.plan.artifact_map()["qemu_kernel"].sha256,
        )
        self.assertEqual(
            evidence.root_image_sha256,
            self.plan.artifact_map()["root_image"].sha256,
        )

    def test_qemu_result_rejects_failure_stale_inputs_or_unsafe_argv(self) -> None:
        base = self._native_result()
        mutations = []
        failed = json.loads(json.dumps(base))
        failed["passed"] = False
        failed["reason"] = "commands2"
        mutations.append(failed)
        wrong_input = json.loads(json.dumps(base))
        wrong_input["input_sha256"]["root_image"] = "f" * 64
        mutations.append(wrong_input)
        bad_nonce = json.loads(json.dumps(base))
        bad_nonce["nonce_sha256"] = "not-a-hash"
        mutations.append(bad_nonce)
        one_boot = json.loads(json.dumps(base))
        one_boot["qemu_argv"] = one_boot["qemu_argv"][:1]
        mutations.append(one_boot)
        wrong_cpu = json.loads(json.dumps(base))
        cpu_index = wrong_cpu["qemu_argv"][0].index("-cpu") + 1
        wrong_cpu["qemu_argv"][0][cpu_index] = "rv64"
        mutations.append(wrong_cpu)
        networked = json.loads(json.dumps(base))
        networked["qemu_argv"][1].extend(["-device", "e1000"])
        mutations.append(networked)
        graphical = json.loads(json.dumps(base))
        display_index = graphical["qemu_argv"][0].index("-display") + 1
        graphical["qemu_argv"][0][display_index] = "gtk"
        mutations.append(graphical)
        accelerated = json.loads(json.dumps(base))
        accelerated["qemu_argv"][0].append("-enable-kvm")
        mutations.append(accelerated)

        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self._write_native_result(mutation)
                with self.assertRaises(ShellPermitError):
                    validate_qemu_result(self.plan, self.native_result_path)

    def test_qemu_evidence_and_permit_use_exact_canonical_json(self) -> None:
        evidence = self._valid_evidence()
        evidence_payload = evidence.canonical_bytes()
        self.assertEqual(QemuShellEvidence.from_bytes(evidence_payload), evidence)
        with self.assertRaisesRegex(ShellPermitError, "duplicate JSON key"):
            QemuShellEvidence.from_bytes(
                evidence_payload.replace(
                    b'"boot_count":2', b'"boot_count":2,"boot_count":2'
                )
            )

        permit = ShellPermit(
            schema_version=1,
            passed=True,
            reason="pass",
            plan_sha256=self.plan.plan_sha256,
            qemu_evidence_sha256=hashlib.sha256(evidence_payload).hexdigest(),
            git_commit=self.plan.git_commit,
            megrez_kernel_sha256=self.plan.artifact_map()["megrez_kernel"].sha256,
            stage1_crc32=self.plan.artifact_map()["stage1"].crc32,
            megrez_dtb_crc32=self.plan.artifact_map()["megrez_dtb"].crc32,
            root_image_sha256=self.plan.artifact_map()["root_image"].sha256,
            gate_bootargs=self.plan.gate_bootargs,
            gate_reboot_after=180,
            long_operation_reboot_after=600,
        )
        payload = permit.canonical_bytes()
        self.assertEqual(ShellPermit.from_bytes(payload), permit)
        with self.assertRaises(ShellPermitError):
            replace(permit, gate_bootargs=permit.gate_bootargs + "; saveenv").validate()

    def test_qemu_result_rejects_symlinked_result_or_serial_log(self) -> None:
        result_target = self.native_output / "result-target.json"
        result_target.write_bytes(self.native_result_path.read_bytes())
        self.native_result_path.unlink()
        self.native_result_path.symlink_to(result_target)
        with self.assertRaises(ShellPermitError):
            validate_qemu_result(self.plan, self.native_result_path)

        self.native_result_path.unlink()
        self._write_native_result(self._native_result())
        log = self.native_output / "boot2.serial.log"
        log_target = self.native_output / "boot2-target.log"
        log_target.write_bytes(log.read_bytes())
        log.unlink()
        log.symlink_to(log_target)
        with self.assertRaises(ShellPermitError):
            validate_qemu_result(self.plan, self.native_result_path)

    def test_permit_reopens_artifacts_and_rejects_dirty_or_stale_results(self) -> None:
        evidence = self._valid_evidence()
        self.qemu_evidence_path.write_bytes(evidence.canonical_bytes())
        self.permit_path.write_text("stale\n", encoding="utf-8")
        artifact_reads = []

        def artifact_reader(name: str, path: Path) -> FrozenArtifact:
            artifact_reads.append((name, path))
            return self.plan.artifact_map()[name]

        permit = issue_shell_permit(
            self.plan,
            self.qemu_evidence_path,
            self.permit_path,
            repository=self.directory,
            artifact_reader=artifact_reader,
            rootfs_validator=lambda plan: plan,
            dtb_validator=lambda _path: 4,
            git_identity=lambda _repository: self.plan.git_commit,
        )

        self.assertEqual(len(artifact_reads), len(SHELL_ARTIFACT_ORDER))
        self.assertEqual(ShellPermit.from_bytes(self.permit_path.read_bytes()), permit)
        self.assertTrue(permit.passed)

        self.permit_path.write_text("stale-again\n", encoding="utf-8")
        dirty_plan = replace(self.plan, git_commit="0" * 40)
        with self.assertRaises(ShellPermitError):
            issue_shell_permit(
                dirty_plan,
                self.qemu_evidence_path,
                self.permit_path,
                repository=self.directory,
                artifact_reader=artifact_reader,
                rootfs_validator=lambda plan: plan,
                dtb_validator=lambda _path: 4,
                git_identity=lambda _repository: self.plan.git_commit,
            )
        self.assertFalse(self.permit_path.exists())

        self.permit_path.write_text("stale-git-error\n", encoding="utf-8")
        with self.assertRaises(ShellPermitError):
            issue_shell_permit(
                self.plan,
                self.qemu_evidence_path,
                self.permit_path,
                repository=self.directory,
                artifact_reader=artifact_reader,
                rootfs_validator=lambda plan: plan,
                dtb_validator=lambda _path: 4,
                git_identity=lambda _repository: (_ for _ in ()).throw(
                    subprocess.CalledProcessError(128, ["git", "status"])
                ),
            )
        self.assertFalse(self.permit_path.exists())

    def test_permit_rejects_output_symlink_without_modifying_target(self) -> None:
        evidence = self._valid_evidence()
        self.qemu_evidence_path.write_bytes(evidence.canonical_bytes())
        target = self.directory / "target"
        target.write_text("keep\n", encoding="utf-8")
        self.permit_path.symlink_to(target)

        with self.assertRaises(ShellPermitError):
            issue_shell_permit(
                self.plan,
                self.qemu_evidence_path,
                self.permit_path,
                repository=self.directory,
                artifact_reader=lambda name, _path: self.plan.artifact_map()[name],
                rootfs_validator=lambda plan: plan,
                dtb_validator=lambda _path: 4,
                git_identity=lambda _repository: self.plan.git_commit,
            )

        self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")

    def test_qemu_adapter_passes_only_the_frozen_gate_inputs(self) -> None:
        argv = qemu_gate_argv(self.plan, self.native_output)
        artifacts = self.plan.artifact_map()

        self.assertEqual(
            argv[:3], (sys.executable, "-m", "tools.riscv.debian.rootfs.rootfs_gate")
        )
        for option, value in (
            ("--kernel", artifacts["qemu_kernel"].path),
            ("--uboot", artifacts["qemu_uboot"].path),
            ("--dtb", artifacts["qemu_dtb"].path),
            ("--stage1-initramfs", artifacts["stage1"].path),
            ("--root-image", artifacts["root_image"].path),
            ("--root-manifest", artifacts["root_manifest"].path),
            ("--packages-lock", artifacts["packages_lock"].path),
            ("--package-checksums", artifacts["package_checksums"].path),
            ("--output-directory", str(self.native_output)),
            ("--smp", "4"),
        ):
            index = argv.index(option)
            self.assertEqual(argv[index + 1], value)

    def test_run_qemu_gate_invalidates_stale_evidence_and_runs_once(self) -> None:
        self.qemu_evidence_path.write_text("stale\n", encoding="utf-8")
        calls = []

        def run_command(argv: tuple[str, ...], **kwargs: object):
            self.assertFalse(self.qemu_evidence_path.exists())
            calls.append((argv, kwargs))
            self._write_native_result(self._native_result())
            return subprocess.CompletedProcess(argv, 0)

        evidence = run_qemu_gate(
            self.plan,
            self.native_output,
            evidence_path=self.qemu_evidence_path,
            run_command=run_command,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], qemu_gate_argv(self.plan, self.native_output))
        self.assertTrue(calls[0][1]["check"])
        self.assertEqual(
            QemuShellEvidence.from_bytes(self.qemu_evidence_path.read_bytes()),
            evidence,
        )


class PersistentShellInventoryTests(unittest.TestCase):
    GEOMETRY = (
        PartitionGeometry(1, 0x8000, 0xF2022),
        PartitionGeometry(2, P2_START_LBA, P2_NR_SECTORS),
        PartitionGeometry(3, 0x8FA022, 0x100000),
    )

    class Operations:
        def __init__(
            self,
            *,
            transcript: bytes = b"",
            install_result_sha256: str | None = None,
            failure: str | None = None,
        ) -> None:
            self.transcript = transcript
            self.install_result_sha256 = install_result_sha256
            self.failure = failure
            self.events: list[str] = []
            self.published: InventoryResult | None = None

        def invalidate(self) -> None:
            self.events.append("invalidate")

        def read_partition_geometry(self) -> tuple[PartitionGeometry, ...]:
            self.events.append("geometry")
            if self.failure == "geometry":
                raise RuntimeError("geometry unavailable")
            if self.failure == "timeout":
                raise TimeoutError("serial timeout")
            return PersistentShellInventoryTests.GEOMETRY

        def matching_install_result(
            self,
            plan: PersistentShellPlan,
            permit: ShellPermit,
            geometry: tuple[PartitionGeometry, ...],
        ) -> str | None:
            del plan, permit, geometry
            self.events.append("install-result")
            if self.failure == "install-result":
                raise RuntimeError("install result unreadable")
            return self.install_result_sha256

        def run_verifier(self, bootargs: str) -> bytes:
            self.events.append(f"verify:{bootargs}")
            if self.failure == "verifier":
                raise RuntimeError("verifier failed")
            return self.transcript

        def publish(self, result: InventoryResult) -> None:
            self.events.append("publish")
            self.published = result

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.plan = _valid_plan(_artifact_fixture(self.directory))
        self.permit = _valid_permit(self.plan)
        self.root_sha256 = self.plan.artifact_map()["root_image"].sha256

    def _transcript(self, outcome: str) -> bytes:
        ready = (
            "DEBIAN_INVENTORY_READY target=/dev/mmcblk0p2 "
            "bytes=4294967296 write=disabled\n"
        )
        return (ready + outcome + "\nreboot: Restarting system\n").encode()

    def test_inventory_classifier_accepts_only_ordered_exact_root_evidence(
        self,
    ) -> None:
        matching = self._transcript(
            f"DEBIAN_VERIFY_PASS sha256={self.root_sha256} bytes=1073741824"
        ).decode()
        needs_install = self._transcript(
            "DEBIAN_VERIFY_FAIL reason=image-hash"
        ).decode()

        self.assertEqual(classify_inventory_log(matching, self.root_sha256), "matching")
        self.assertEqual(
            classify_inventory_log(
                "Kernel command line: asterinas.reboot_after=600\n" + matching,
                self.root_sha256,
            ),
            "matching",
        )
        self.assertEqual(
            classify_inventory_log(needs_install, self.root_sha256), "needs-install"
        )
        invalid = (
            f"DEBIAN_VERIFY_PASS sha256={self.root_sha256} bytes=1073741824",
            "DEBIAN_VERIFY_FAIL reason=image-hash\n"
            "DEBIAN_INVENTORY_READY target=/dev/mmcblk0p2 bytes=4294967296 write=disabled",
            self._transcript("DEBIAN_VERIFY_FAIL reason=target-size-mismatch").decode(),
            self._transcript("DEBIAN_VERIFY_FAIL reason=image-hash-output").decode(),
            self._transcript(
                f"DEBIAN_VERIFY_PASS sha256={'f' * 64} bytes=1073741824"
            ).decode(),
            self._transcript("DEBIAN_VERIFY_FAIL reason=image-hash").decode()
            + f"DEBIAN_VERIFY_PASS sha256={self.root_sha256} bytes=1073741824\n",
            "reboot: Restarting system\n" + matching,
        )
        for transcript in invalid:
            with self.subTest(transcript=transcript), self.assertRaises(InventoryError):
                classify_inventory_log(transcript, self.root_sha256)

    def test_verifier_bootargs_are_read_only_and_use_long_recovery(self) -> None:
        bootargs = verifier_bootargs(self.plan)

        self.assertEqual(
            bootargs,
            "console=ttyS0 cpu_no_boost_1_6ghz loglevel=info init=/init "
            "asterinas.reboot_after=600",
        )
        for forbidden in (
            "asterinas.mmc_write_partition2",
            "asterinas.net=",
            "asterinas.neighbor=",
            "hardware_watchdog",
        ):
            self.assertNotIn(forbidden, bootargs)

    def test_inventory_result_is_exact_canonical_and_bound_to_geometry(self) -> None:
        result = InventoryResult(
            schema_version=1,
            status="matching",
            reason="verified-root",
            plan_sha256=self.plan.plan_sha256,
            permit_sha256=hashlib.sha256(self.permit.canonical_bytes()).hexdigest(),
            partitions=self.GEOMETRY,
            expected_root_sha256=self.root_sha256,
            install_result_sha256=None,
            serial_sha256="b" * 64,
        )

        payload = result.canonical_bytes()

        self.assertEqual(InventoryResult.from_bytes(payload), result)
        with self.assertRaisesRegex(InventoryError, "duplicate JSON key"):
            InventoryResult.from_bytes(
                payload.replace(
                    b'"status":"matching"', b'"status":"matching","status":"matching"'
                )
            )
        with self.assertRaises(InventoryError):
            replace(
                result,
                partitions=(
                    self.GEOMETRY[0],
                    PartitionGeometry(2, P2_START_LBA + 1, P2_NR_SECTORS),
                    self.GEOMETRY[2],
                ),
            ).validate()
        with self.assertRaises(InventoryError):
            replace(result, reason="unrelated").validate()
        with self.assertRaises(InventoryError):
            replace(
                result,
                status="not-measurable",
                reason="verifier-evidence",
                install_result_sha256="c" * 64,
            ).validate()

    def test_matching_install_result_skips_the_full_device_verifier(self) -> None:
        operations = self.Operations(install_result_sha256="c" * 64)

        result = run_inventory(self.plan, self.permit, operations)

        self.assertEqual(result.status, "matching")
        self.assertEqual(result.reason, "install-result")
        self.assertEqual(result.install_result_sha256, "c" * 64)
        self.assertIsNone(result.serial_sha256)
        self.assertEqual(
            operations.events,
            ["invalidate", "geometry", "install-result", "publish"],
        )
        self.assertIs(operations.published, result)

    def test_verifier_distinguishes_matching_from_install_needed(self) -> None:
        cases = (
            (
                f"DEBIAN_VERIFY_PASS sha256={self.root_sha256} bytes=1073741824",
                "matching",
                "verified-root",
            ),
            ("DEBIAN_VERIFY_FAIL reason=image-hash", "needs-install", "image-hash"),
        )
        for marker, status, reason in cases:
            operations = self.Operations(transcript=self._transcript(marker))
            with self.subTest(status=status):
                result = run_inventory(self.plan, self.permit, operations)
            self.assertEqual(result.status, status)
            self.assertEqual(result.reason, reason)
            self.assertEqual(
                result.serial_sha256,
                hashlib.sha256(operations.transcript).hexdigest(),
            )
            self.assertEqual(
                operations.events[:3], ["invalidate", "geometry", "install-result"]
            )
            self.assertTrue(operations.events[3].startswith("verify:"))
            self.assertEqual(operations.events[4], "publish")

    def test_ambiguous_failures_publish_not_measurable_never_needs_install(
        self,
    ) -> None:
        for failure in ("geometry", "timeout", "install-result", "verifier"):
            operations = self.Operations(failure=failure)
            with self.subTest(failure=failure):
                result = run_inventory(self.plan, self.permit, operations)
            self.assertEqual(result.status, "not-measurable")
            self.assertNotEqual(result.status, "needs-install")
            self.assertIs(operations.published, result)

        operations = self.Operations(
            transcript=self._transcript(
                "DEBIAN_VERIFY_FAIL reason=target-size-mismatch"
            )
        )
        result = run_inventory(self.plan, self.permit, operations)
        self.assertEqual(result.status, "not-measurable")
        self.assertEqual(result.reason, "verifier-evidence")

        operations = self.Operations()
        operations.read_partition_geometry = lambda: (
            self.GEOMETRY[0],
            PartitionGeometry(2, P2_START_LBA + 1, P2_NR_SECTORS),
            self.GEOMETRY[2],
        )
        result = run_inventory(self.plan, self.permit, operations)
        self.assertEqual(result.status, "not-measurable")
        self.assertEqual(result.partitions, ())


class PersistentShellInstallTests(unittest.TestCase):
    GEOMETRY = PersistentShellInventoryTests.GEOMETRY

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.repository = Path(self.temporary_directory.name)
        self.plan = _valid_plan(_artifact_fixture(self.repository))
        self.permit = _valid_permit(self.plan)
        self.output = self.repository / "target/install"
        self.root_sha256 = self.plan.artifact_map()["root_image"].sha256
        self.permit_sha256 = hashlib.sha256(self.permit.canonical_bytes()).hexdigest()
        self.matching_inventory = self._inventory("matching", "verified-root")
        self.needs_install_inventory = self._inventory("needs-install", "image-hash")

    def _inventory(self, status: str, reason: str) -> InventoryResult:
        return InventoryResult(
            schema_version=1,
            status=status,
            reason=reason,
            plan_sha256=self.plan.plan_sha256,
            permit_sha256=self.permit_sha256,
            partitions=self.GEOMETRY,
            expected_root_sha256=self.root_sha256,
            install_result_sha256=None,
            serial_sha256="b" * 64,
        )

    def _artifact_reader(self, name: str, _path: Path) -> FrozenArtifact:
        return self.plan.artifact_map()[name]

    def _install_result(self) -> StageResult:
        return StageResult(
            1,
            "install",
            True,
            "install-pass",
            self.plan.plan_sha256,
            ("installer.serial.log", "debian-current-network-installer.cpio"),
        )

    def test_matching_inventory_skips_install_without_consuming_permit(self) -> None:
        calls: list[str] = []

        def forbidden(_request: object) -> StageResult:
            calls.append("run")
            raise AssertionError("matching root must not start an installer")

        result = install_if_needed(
            self.plan,
            self.permit,
            self.matching_inventory,
            self.output,
            repository=self.repository,
            run=forbidden,
            artifact_reader=self._artifact_reader,
        )

        self.assertEqual(result.reason, "already-matching")
        self.assertEqual(calls, [])
        self.assertFalse((self.output / "attempt.json").exists())
        self.assertEqual(
            StageResult.from_bytes((self.output / "result.json").read_bytes()),
            result,
        )

    def test_needs_install_publishes_attempt_then_runs_exactly_once(self) -> None:
        requests: list[object] = []

        def success(request: object) -> StageResult:
            requests.append(request)
            self.assertTrue((self.output / "attempt.json").is_file())
            self.assertFalse((self.output / "result.json").exists())
            return self._install_result()

        result = install_if_needed(
            self.plan,
            self.permit,
            self.needs_install_inventory,
            self.output,
            repository=self.repository,
            run=success,
            artifact_reader=self._artifact_reader,
        )

        self.assertTrue(result.passed)
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(
            request.kernel,
            Path(self.plan.artifact_map()["megrez_kernel"].path),
        )
        self.assertEqual(
            request.installer_base,
            Path(self.plan.artifact_map()["installer_base"].path),
        )
        self.assertEqual(request.root_sha256, self.root_sha256)
        self.assertEqual(
            request.bootargs,
            installer_bootargs(self.plan, self.root_sha256),
        )
        attempt = StageResult.from_bytes((self.output / "attempt.json").read_bytes())
        self.assertFalse(attempt.passed)
        self.assertEqual(attempt.reason, "attempt-started")
        self.assertIn(f"permit-sha256:{self.permit_sha256}", attempt.evidence)

    def test_failed_attempt_cannot_relaunch_with_the_same_permit(self) -> None:
        run_count = 0

        def fail(_request: object) -> StageResult:
            nonlocal run_count
            run_count += 1
            raise RuntimeError("board write failed")

        with self.assertRaisesRegex(RuntimeError, "board write failed"):
            install_if_needed(
                self.plan,
                self.permit,
                self.needs_install_inventory,
                self.output,
                repository=self.repository,
                run=fail,
                artifact_reader=self._artifact_reader,
            )
        with self.assertRaises(PreboardError):
            install_if_needed(
                self.plan,
                self.permit,
                self.needs_install_inventory,
                self.output,
                repository=self.repository,
                run=fail,
                artifact_reader=self._artifact_reader,
            )

        self.assertEqual(run_count, 1)
        self.assertTrue((self.output / "attempt.json").is_file())
        self.assertFalse((self.output / "result.json").exists())

    def test_install_rejects_untrusted_or_changed_inputs_before_attempt(self) -> None:
        calls: list[str] = []

        def forbidden(_request: object) -> StageResult:
            calls.append("run")
            raise AssertionError("invalid input reached the installer")

        wrong_geometry = replace(
            self.needs_install_inventory,
            partitions=(
                self.GEOMETRY[0],
                PartitionGeometry(2, P2_START_LBA + 1, P2_NR_SECTORS),
                self.GEOMETRY[2],
            ),
        )
        variants = (
            replace(
                self.needs_install_inventory,
                status="not-measurable",
                reason="verifier-failed",
            ),
            replace(self.needs_install_inventory, plan_sha256="f" * 64),
            replace(self.needs_install_inventory, permit_sha256="f" * 64),
            replace(self.needs_install_inventory, expected_root_sha256="f" * 64),
            wrong_geometry,
        )
        for inventory in variants:
            with self.subTest(inventory=inventory), self.assertRaises(InventoryError):
                install_if_needed(
                    self.plan,
                    self.permit,
                    inventory,
                    self.output,
                    repository=self.repository,
                    run=forbidden,
                    artifact_reader=self._artifact_reader,
                )
        changed_root = replace(self.plan.artifact_map()["root_image"], sha256="f" * 64)

        def changed_reader(name: str, _path: Path) -> FrozenArtifact:
            if name == "root_image":
                return changed_root
            return self.plan.artifact_map()[name]

        with self.assertRaises(InventoryError):
            install_if_needed(
                self.plan,
                self.permit,
                self.needs_install_inventory,
                self.output,
                repository=self.repository,
                run=forbidden,
                artifact_reader=changed_reader,
            )

        self.assertEqual(calls, [])
        self.assertFalse((self.output / "attempt.json").exists())


class PersistentShellPhysicalGateTests(unittest.TestCase):
    NONCE = "0123456789abcdef" * 4
    PACKAGES = (
        ("base-files", "13.8+deb13u2"),
        ("bash", "5.2.37-2+b5"),
        ("coreutils", "9.7-3"),
        ("libc6", "2.41-12"),
        ("util-linux", "2.41-5"),
    )
    GEOMETRY = PersistentShellInventoryTests.GEOMETRY

    class Operations:
        def __init__(
            self,
            owner: PersistentShellPhysicalGateTests,
            *,
            recovered: tuple[bool, bool] = (True, True),
            fatal_boot: int | None = None,
        ) -> None:
            self.owner = owner
            self.recovered = recovered
            self.fatal_boot = fatal_boot
            self.events: list[str] = []
            self.boot_numbers: list[int] = []
            self.nonces: list[str] = []
            self.published: tuple[tuple[bytes, bytes], PhysicalShellResult] | None = (
                None
            )

        def invalidate(self) -> None:
            self.events.append("invalidate")

        def validate_artifacts(
            self,
            plan: PersistentShellPlan,
        ) -> tuple[str, tuple[tuple[str, str], ...]]:
            self.events.append("validate")
            self.owner.assertIs(plan, self.owner.plan)
            return "13.6", self.owner.PACKAGES

        def run_boot(
            self,
            plan: PersistentShellPlan,
            boot_number: int,
            nonce: str,
        ) -> PhysicalBoot:
            self.owner.assertIs(plan, self.owner.plan)
            self.events.append(f"boot{boot_number}")
            self.boot_numbers.append(boot_number)
            self.nonces.append(nonce)
            protocol = self.owner._transcript(boot_number, nonce)
            complete = protocol + b"OpenSBI v1.7\r\nU-Boot 2026.07\r\n=> "
            if self.fatal_boot == boot_number:
                complete += b"Kernel panic - not syncing\r\n"
            return PhysicalBoot(
                protocol_transcript=protocol,
                complete_transcript=complete,
                recovered=self.recovered[boot_number - 1],
            )

        def publish(
            self,
            logs: tuple[bytes, bytes],
            result: PhysicalShellResult,
        ) -> None:
            self.events.append("publish")
            self.published = logs, result

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.plan = _valid_plan(_artifact_fixture(self.directory))
        self.permit = _valid_permit(self.plan)
        self.inventory = InventoryResult(
            schema_version=1,
            status="matching",
            reason="verified-root",
            plan_sha256=self.plan.plan_sha256,
            permit_sha256=hashlib.sha256(self.permit.canonical_bytes()).hexdigest(),
            partitions=self.GEOMETRY,
            expected_root_sha256=self.plan.artifact_map()["root_image"].sha256,
            install_result_sha256=None,
            serial_sha256="a" * 64,
        )

    def _transcript(self, boot_number: int, nonce: str) -> bytes:
        outputs = {
            "architecture": "riscv64",
            "debian-release": "13.6",
            "bash-version": "5.2.37(1)-release",
            "packages": "\n".join(
                f"{name}\t{version}" for name, version in self.PACKAGES
            ),
            "root-filesystem": "ext2/ext3",
            "persistence": nonce,
            "second-probe": "boot2-probe-created",
        }
        lines = ["__DEBIAN_ROOTFS_SHELL_READY__"]
        for command in shell_commands(boot_number=boot_number, nonce=nonce):
            lines.extend(
                (
                    command.payload,
                    command.begin_marker,
                    outputs[command.name],
                    f"{command.status_prefix}0",
                    command.end_marker,
                )
            )
        return ("\n".join(lines) + "\n").encode()

    def test_two_boot_gate_resets_epoch_and_reuses_one_nonce(self) -> None:
        operations = self.Operations(self)

        result = run_physical_gate(
            self.plan,
            self.permit,
            self.inventory,
            operations,
            nonce_factory=lambda: self.NONCE,
        )

        self.assertTrue(result.passed)
        self.assertEqual(operations.boot_numbers, [1, 2])
        self.assertEqual(operations.nonces, [self.NONCE, self.NONCE])
        self.assertEqual(
            operations.events,
            ["invalidate", "validate", "boot1", "boot2", "publish"],
        )
        logs, published = operations.published
        self.assertIs(published, result)
        self.assertNotIn(self.NONCE.encode(), logs[0] + logs[1])
        self.assertEqual(
            PhysicalShellResult.from_bytes(result.canonical_bytes()), result
        )

    def test_failed_recovery_or_fatal_log_never_starts_or_passes_boot_two(self) -> None:
        for operations in (
            self.Operations(self, recovered=(False, True)),
            self.Operations(self, fatal_boot=1),
        ):
            with self.subTest(operations=operations):
                result = run_physical_gate(
                    self.plan,
                    self.permit,
                    self.inventory,
                    operations,
                    nonce_factory=lambda: self.NONCE,
                )
            self.assertFalse(result.passed)
            self.assertEqual(operations.boot_numbers, [1])
            self.assertIs(operations.published[1], result)

    def test_gate_rejects_unmatched_inventory_before_artifact_or_serial_use(
        self,
    ) -> None:
        operations = self.Operations(self)
        inventory = replace(
            self.inventory,
            status="not-measurable",
            reason="verifier-failed",
            serial_sha256=None,
        )

        result = run_physical_gate(
            self.plan,
            self.permit,
            inventory,
            operations,
            nonce_factory=lambda: self.NONCE,
        )

        self.assertFalse(result.passed)
        self.assertEqual(operations.events, ["invalidate", "publish"])

    def test_dnsmasq_tftp_contract_has_no_dhcp_or_host_network_mutation(self) -> None:
        root = self.directory / "tftp"
        argv = dnsmasq_tftp_argv("enp12s0", root)

        self.assertEqual(
            argv,
            (
                "/usr/sbin/dnsmasq",
                "--no-daemon",
                "--port=0",
                "--no-hosts",
                "--no-resolv",
                "--interface=enp12s0",
                "--bind-interfaces",
                "--enable-tftp",
                f"--tftp-root={root}",
                "--log-facility=-",
            ),
        )
        self.assertFalse(any("dhcp" in argument for argument in argv))


class PersistentShellPhysicalIoTests(unittest.TestCase):
    class Process:
        def __init__(self) -> None:
            self.terminated = 0

        def poll(self) -> None:
            return None

        def terminate_group(self, term_deadline: float, kill_deadline: float) -> None:
            self.terminated += 1
            if not 0 < term_deadline < kill_deadline:
                raise AssertionError("invalid process-group deadlines")

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def test_tftp_server_waits_for_ready_and_cleans_process_group(self) -> None:
        process = self.Process()
        launched: list[tuple[str, ...]] = []

        def launcher(argv: tuple[str, ...], *, stdio_fd: int) -> object:
            launched.append(argv)
            os.write(stdio_fd, b"dnsmasq: started, version 2.90\n")
            return process

        server = TftpOnlyServer("enp12s0", self.directory, launcher=launcher)
        server.start(time.monotonic() + 1.0)
        server.stop()

        self.assertEqual(launched, [server.argv])
        self.assertEqual(process.terminated, 1)
        self.assertFalse(any("dhcp" in argument for argument in server.argv))

        def failed_launcher(argv: tuple[str, ...], *, stdio_fd: int) -> object:
            del argv, stdio_fd
            raise OSError("spawn failed")

        failed = TftpOnlyServer("enp12s0", self.directory, launcher=failed_launcher)
        with self.assertRaisesRegex(OSError, "spawn failed"):
            failed.start(time.monotonic() + 1.0)
        self.assertEqual(failed._reader, -1)

    def test_tftp_staging_rechecks_held_source_identity(self) -> None:
        source = self.directory / "kernel"
        source.write_bytes(b"asterinas-kernel")
        artifact = FrozenArtifact.from_path("megrez_kernel", source)
        destination = self.directory / "staged"

        _copy_verified(artifact, destination)
        self.assertEqual(destination.read_bytes(), source.read_bytes())

        destination.unlink()
        original_write = physical_io.os.write

        def short_write(descriptor: int, payload: bytes) -> int:
            amount = max(1, len(payload) // 2)
            return original_write(descriptor, payload[:amount])

        with mock.patch.object(physical_io.os, "write", side_effect=short_write):
            _copy_verified(artifact, destination)
        self.assertEqual(destination.read_bytes(), source.read_bytes())
        self.assertEqual(destination.stat().st_mode & 0o777, 0o644)

        destination.unlink()
        with self.assertRaisesRegex(RuntimeError, "changed while staging"):
            _copy_verified(replace(artifact, sha256="f" * 64), destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.directory.glob(".staged.tmp")), [])

    def test_publication_invalidates_stale_result_and_writes_result_last(self) -> None:
        output = self.directory / "output"
        output.mkdir()
        (output / "result.json").write_text('{"passed":true}\n')
        result = PhysicalShellResult(
            schema_version=1,
            passed=False,
            reason="boot1-failed",
            plan_sha256="1" * 64,
            permit_sha256="2" * 64,
            inventory_sha256="3" * 64,
            nonce_sha256="4" * 64,
            boot1_serial_sha256=hashlib.sha256(b"first").hexdigest(),
            boot2_serial_sha256=hashlib.sha256(b"second").hexdigest(),
            boot1_recovered=False,
            boot2_recovered=False,
        )
        operations = PhysicalBoardOperations(
            device="/dev/null",
            interface="enp12s0",
            output=output,
        )
        try:
            operations.invalidate()
            self.assertFalse((output / "result.json").exists())
            operations.publish((b"first", b"second"), result)
        finally:
            operations.close()

        self.assertEqual((output / "boot1.serial.log").read_bytes(), b"first")
        self.assertEqual((output / "boot2.serial.log").read_bytes(), b"second")
        self.assertEqual(
            PhysicalShellResult.from_bytes((output / "result.json").read_bytes()),
            result,
        )


if __name__ == "__main__":
    unittest.main()
