#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
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


ROOT_IMAGE_SIZE_BYTES = 1024 * 1024 * 1024


class PersistentShellContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.artifacts = self._artifact_fixture()

    def _artifact_fixture(self) -> tuple[FrozenArtifact, ...]:
        artifacts = []
        for index, name in enumerate(SHELL_ARTIFACT_ORDER):
            path = self.directory / name
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

    def valid_plan(self) -> PersistentShellPlan:
        return PersistentShellPlan(
            schema_version=1,
            git_commit="1" * 40,
            artifacts=self.artifacts,
            smp=4,
            qemu_paging="sv39",
            megrez_paging="sv48",
            gate_bootargs=(
                "console=ttyS0 cpu_no_boost_1_6ghz loglevel=info init=/init "
                "asterinas.reboot_after=180"
            ),
            final_bootargs=(
                "console=ttyS0 cpu_no_boost_1_6ghz loglevel=info init=/init"
            ),
            gate_reboot_after=180,
            long_operation_reboot_after=600,
            partition_start_lba=P2_START_LBA,
            partition_nr_sectors=P2_NR_SECTORS,
        )

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


if __name__ == "__main__":
    unittest.main()
