"""Fail-closed permit tests for one Megrez Debian desktop board attempt."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.riscv.debian.rootfs.gate_protocol import GENERIC_SV39_CPU
from tools.riscv.megrez_debug_contract import (
    DEBIAN_BROWSER_ARTIFACT_ORDER,
    DEBIAN_BROWSER_MARKERS,
    ROOT_IMAGE_BYTES,
    ArtifactIdentity,
    DebugPlan,
    StageResult,
)
from tools.riscv.megrez_gmac_gate import physical_bootargs
from tools.riscv.megrez_preboard import (
    PreboardError,
    PreboardPermit,
    RecoveryEvidence,
    create_recovery_evidence,
    issue_preboard_permit,
)


class MegrezPreboardTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repository = Path(temporary.name)
        artifact_directory = self.repository / "artifacts"
        artifact_directory.mkdir()
        addresses = {
            "kernel": 0x80200000,
            "initramfs": 0x83000000,
            "qemu_dtb": 0xF0000000,
            "megrez_dtb": 0xF0000000,
        }
        self.identities = {
            name: ArtifactIdentity(
                name=name,
                path=str((artifact_directory / name).absolute()),
                load_address=addresses.get(name, 0),
                size=ROOT_IMAGE_BYTES if name == "root_image" else 4096,
                sha256=hashlib.sha256(name.encode()).hexdigest(),
                crc32=f"{zlib.crc32(name.encode()):08x}",
            )
            for name in DEBIAN_BROWSER_ARTIFACT_ORDER
        }
        for identity in self.identities.values():
            Path(identity.path).write_bytes(identity.name.encode())
        self.plan = DebugPlan(
            schema_version=2,
            profile="debian-browser",
            artifacts=tuple(
                self.identities[name] for name in DEBIAN_BROWSER_ARTIFACT_ORDER
            ),
            bootargs=physical_bootargs(600),
            smp=4,
            sv39=True,
            markers=DEBIAN_BROWSER_MARKERS,
            reboot_after=600,
        )
        self.desktop = StageResult(
            1,
            "desktop",
            True,
            "desktop-pass",
            self.plan.plan_sha256,
            ("native/result.json",),
        )
        self.recovery = RecoveryEvidence(
            schema_version=1,
            passed=True,
            reason="recovery-pass",
            plan_sha256=self.plan.plan_sha256,
            kernel_sha256=self.identities["kernel"].sha256,
            native_result_sha256="a" * 64,
            serial_sha256="b" * 64,
            second_firmware_epoch=True,
            fresh_uboot_prompt=True,
        )
        self.desktop_path = self.repository / "desktop.json"
        self.recovery_path = self.repository / "recovery.json"
        self.desktop_path.write_bytes(self.desktop.canonical_bytes())
        self.recovery_path.write_bytes(self.recovery.canonical_bytes())
        self.output = self.repository / "target/preboard/preboard.json"

    def _artifact_validator(self, plan: DebugPlan) -> dict[str, ArtifactIdentity]:
        self.assertIs(plan, self.plan)
        return self.identities

    def _native_recovery(self) -> dict[str, object]:
        kernel = self.identities["kernel"]
        return {
            "artifacts": {
                "kernel_crc32": kernel.crc32,
                "kernel_size": kernel.size,
            },
            "audit": {
                "booti_command_count": 1,
                "failures": [],
                "passed": True,
            },
            "boot_disk_sha256_after": "d" * 64,
            "boot_disk_sha256_before": "d" * 64,
            "effective_bootargs": (
                "console=ttyS0 loglevel=info init=/init "
                "asterinas.net=eic7700-rj45,10.100.19.200/21 "
                "asterinas.reboot_after=60"
            ),
            "passed": True,
            "profile": "generic-sv39-smp4-software-reboot",
            "qemu_argv": [
                "qemu-system-riscv64",
                "-cpu",
                GENERIC_SV39_CPU,
                "-m",
                "2G",
                "-smp",
                "4",
                "-display",
                "none",
                "-monitor",
                "none",
            ],
            "scenario": "positive",
            "validation_scenario": "megrez-tcp-probe-recovery",
            "session": {
                "booti_sent_count": 1,
                "cleanup_complete": True,
                "failure": None,
                "recovery_complete": True,
                "timed_out": False,
            },
        }

    @staticmethod
    def _recovery_transcript() -> bytes:
        return (
            b"OpenSBI v1.7\nU-Boot 2026.07\n=> \n"
            b"ASTERINAS_SOFTWARE_REBOOT_ARMED seconds=60\n"
            b"ASTERINAS_GMAC_TCP_PROBE_READY peer=10.100.19.216:18080 "
            b"status=200 sizes=16384,65536,1048576,16777216 "
            b"completed_bytes=17907712 pattern=mod251\n"
            b"OpenSBI v1.7\nU-Boot 2026.07\n=> \n"
        )

    def test_recovery_adapter_binds_kernel_and_second_firmware_epoch(self) -> None:
        native = self.repository / "native-recovery.json"
        serial = self.repository / "recovery.serial.log"
        sums = self.repository / "SHA256SUMS"
        native.write_text(json.dumps(self._native_recovery()))
        serial.write_bytes(self._recovery_transcript())
        sums.write_text(
            f"{self.identities['kernel'].sha256}  /tmp/fs-root/asterinas.booti\n"
        )

        result = create_recovery_evidence(self.plan, native, serial, sums)

        self.assertTrue(result.passed)
        self.assertEqual(result.reason, "recovery-pass")
        self.assertEqual(result.plan_sha256, self.plan.plan_sha256)
        self.assertEqual(result.kernel_sha256, self.identities["kernel"].sha256)
        self.assertTrue(result.second_firmware_epoch)
        self.assertTrue(result.fresh_uboot_prompt)

    def test_recovery_adapter_binds_the_preboard_tcp_probe_plan(self) -> None:
        tcp_plan = DebugPlan(
            schema_version=1,
            profile="tcp-probe",
            artifacts=tuple(
                self.identities[name]
                for name in ("kernel", "initramfs", "qemu_dtb", "megrez_dtb")
            ),
            bootargs=(
                "console=ttyS0 loglevel=info init=/init "
                "asterinas.net=eic7700-rj45,10.100.19.200/21 "
                "asterinas.reboot_after=60"
            ),
            smp=4,
            sv39=True,
            markers=("Enter riscv_boot", "ASTERINAS_GMAC_TCP_PROBE_READY"),
            reboot_after=60,
        )
        native = self.repository / "native-tcp-recovery.json"
        serial = self.repository / "tcp-recovery.serial.log"
        sums = self.repository / "TCP-SHA256SUMS"
        native.write_text(json.dumps(self._native_recovery()))
        serial.write_bytes(self._recovery_transcript())
        sums.write_text(
            f"{self.identities['kernel'].sha256}  /tmp/fs-root/asterinas.booti\n"
        )

        result = create_recovery_evidence(tcp_plan, native, serial, sums)

        self.assertTrue(result.passed)
        self.assertEqual(result.plan_sha256, tcp_plan.plan_sha256)
        self.assertEqual(result.kernel_sha256, self.identities["kernel"].sha256)

    def test_recovery_adapter_rejects_hash_qemu_and_epoch_drift(self) -> None:
        native = self.repository / "native-recovery.json"
        serial = self.repository / "recovery.serial.log"
        sums = self.repository / "SHA256SUMS"
        variants = (
            (self._native_recovery(), self._recovery_transcript(), "0" * 64),
            (
                {
                    **self._native_recovery(),
                    "qemu_argv": ["qemu-system-riscv64", "-cpu", "rv64"],
                },
                self._recovery_transcript(),
                self.identities["kernel"].sha256,
            ),
            (
                self._native_recovery(),
                b"ASTERINAS_GMAC_TCP_PROBE_READY peer=10.100.19.216:18080\n",
                self.identities["kernel"].sha256,
            ),
        )
        for document, transcript, kernel_hash in variants:
            with self.subTest(kernel_hash=kernel_hash, transcript=transcript):
                native.write_text(json.dumps(document))
                serial.write_bytes(transcript)
                sums.write_text(f"{kernel_hash}  /tmp/asterinas.booti\n")
                with self.assertRaises(PreboardError):
                    create_recovery_evidence(self.plan, native, serial, sums)

    def test_permit_binds_results_commit_transfers_and_boot_contract(self) -> None:
        checked_dtbs: list[Path] = []

        permit = issue_preboard_permit(
            self.plan,
            self.desktop_path,
            self.recovery_path,
            self.output,
            artifact_validator=self._artifact_validator,
            rootfs_validator=lambda _identities: None,
            dtb_validator=checked_dtbs.append,
            git_identity=lambda _repository: "c" * 40,
            repository_root=self.repository,
        )

        self.assertEqual(PreboardPermit.from_bytes(self.output.read_bytes()), permit)
        self.assertEqual(permit.plan_sha256, self.plan.plan_sha256)
        self.assertEqual(permit.git_commit, "c" * 40)
        self.assertEqual(permit.kernel_sha256, self.identities["kernel"].sha256)
        self.assertEqual(
            permit.transfer_crc32,
            tuple(
                (name, self.identities[name].crc32)
                for name in ("kernel", "initramfs", "megrez_dtb")
            ),
        )
        self.assertEqual(permit.bootargs, self.plan.bootargs)
        self.assertEqual(permit.reboot_after, 600)
        self.assertEqual(
            checked_dtbs,
            [Path(self.identities[name].path) for name in ("qemu_dtb", "megrez_dtb")],
        )

    def test_permit_rejects_result_plan_kernel_artifact_and_dtb_drift(self) -> None:
        variants = (
            (replace(self.desktop, plan_sha256="0" * 64), self.recovery),
            (replace(self.desktop, passed=False), self.recovery),
            (
                self.desktop,
                replace(self.recovery, kernel_sha256="0" * 64),
            ),
        )
        for desktop, recovery in variants:
            with self.subTest(desktop=desktop, recovery=recovery):
                self.desktop_path.write_bytes(desktop.canonical_bytes())
                self.recovery_path.write_bytes(recovery.canonical_bytes())
                with self.assertRaises(PreboardError):
                    issue_preboard_permit(
                        self.plan,
                        self.desktop_path,
                        self.recovery_path,
                        self.output,
                        artifact_validator=self._artifact_validator,
                        rootfs_validator=lambda _identities: None,
                        dtb_validator=lambda _path: None,
                        git_identity=lambda _repository: "c" * 40,
                        repository_root=self.repository,
                    )

        self.desktop_path.write_bytes(self.desktop.canonical_bytes())
        self.recovery_path.write_bytes(self.recovery.canonical_bytes())
        for validator in (
            lambda _plan: (_ for _ in ()).throw(ValueError("artifact drift")),
            self._artifact_validator,
        ):
            with self.assertRaises(PreboardError):
                issue_preboard_permit(
                    self.plan,
                    self.desktop_path,
                    self.recovery_path,
                    self.output,
                    artifact_validator=validator,
                    rootfs_validator=lambda _identities: None,
                    dtb_validator=lambda _path: (_ for _ in ()).throw(
                        ValueError("DTB drift")
                    ),
                    git_identity=lambda _repository: "c" * 40,
                    repository_root=self.repository,
                )

    def test_stale_permit_is_removed_before_validation_and_symlink_is_rejected(
        self,
    ) -> None:
        self.output.parent.mkdir(parents=True)
        self.output.write_text('{"passed":true}\n')
        with self.assertRaises(PreboardError):
            issue_preboard_permit(
                self.plan,
                self.desktop_path,
                self.recovery_path,
                self.output,
                artifact_validator=lambda _plan: (_ for _ in ()).throw(
                    ValueError("stop")
                ),
                rootfs_validator=lambda _identities: None,
                dtb_validator=lambda _path: None,
                git_identity=lambda _repository: "c" * 40,
                repository_root=self.repository,
            )
        self.assertFalse(self.output.exists())

        protected = self.repository / "protected"
        protected.write_text("keep")
        self.output.symlink_to(protected)
        with self.assertRaisesRegex(PreboardError, "unsafe"):
            issue_preboard_permit(
                self.plan,
                self.desktop_path,
                self.recovery_path,
                self.output,
                artifact_validator=self._artifact_validator,
                rootfs_validator=lambda _identities: None,
                dtb_validator=lambda _path: None,
                git_identity=lambda _repository: "c" * 40,
                repository_root=self.repository,
            )
        self.assertEqual(protected.read_text(), "keep")

    def test_unified_cli_translates_recovery_then_issues_permit(self) -> None:
        from tools.riscv import megrez_debug

        plan_path = self.repository / "plan.json"
        plan_path.write_bytes(self.plan.canonical_bytes())
        native = self.repository / "native.json"
        serial = self.repository / "serial.log"
        sums = self.repository / "SHA256SUMS"
        recovery_output = self.repository / "target/recovery.json"
        for path in (native, serial, sums):
            path.write_text("placeholder")

        with mock.patch.object(
            megrez_debug,
            "create_recovery_evidence",
            return_value=self.recovery,
            create=True,
        ) as create:
            status = megrez_debug.main(
                (
                    "recovery",
                    str(plan_path),
                    "--native-result",
                    str(native),
                    "--serial-log",
                    str(serial),
                    "--sha256sums",
                    str(sums),
                    "--output",
                    str(recovery_output),
                )
            )
        self.assertEqual(status, 0)
        create.assert_called_once_with(self.plan, native, serial, sums)
        self.assertEqual(
            RecoveryEvidence.from_bytes(recovery_output.read_bytes()), self.recovery
        )

        with mock.patch.object(
            megrez_debug, "issue_preboard_permit", return_value=mock.sentinel.permit
        ) as issue:
            status = megrez_debug.main(
                (
                    "preboard",
                    str(plan_path),
                    "--desktop-result",
                    str(self.desktop_path),
                    "--recovery-result",
                    str(recovery_output),
                    "--output",
                    str(self.output),
                )
            )
        self.assertEqual(status, 0)
        issue.assert_called_once_with(
            self.plan,
            self.desktop_path,
            recovery_output,
            self.output,
        )

    def test_output_parent_swap_fails_closed_without_writing_outside(self) -> None:
        self.output.parent.mkdir(parents=True)
        original = self.output.parent.with_name("pinned-original")
        outside = self.repository / "outside"
        outside.mkdir()
        swapped = False

        def swap(_path: Path) -> None:
            nonlocal swapped
            if swapped:
                return
            self.output.parent.rename(original)
            self.output.parent.symlink_to(outside, target_is_directory=True)
            swapped = True

        with self.assertRaisesRegex(PreboardError, "parent.*changed"):
            issue_preboard_permit(
                self.plan,
                self.desktop_path,
                self.recovery_path,
                self.output,
                artifact_validator=self._artifact_validator,
                rootfs_validator=lambda _identities: None,
                dtb_validator=swap,
                git_identity=lambda _repository: "c" * 40,
                repository_root=self.repository,
            )
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((original / self.output.name).exists())

    def test_git_identity_rejects_tracked_drift_but_ignores_untracked_files(
        self,
    ) -> None:
        from tools.riscv import megrez_preboard

        responses = (
            SimpleNamespace(stdout="c" * 40 + "\n"),
            SimpleNamespace(stdout=""),
        )
        with mock.patch.object(
            megrez_preboard.subprocess, "run", side_effect=responses
        ) as run:
            commit = megrez_preboard._git_identity(self.repository)

        self.assertEqual(commit, "c" * 40)
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["git", "status", "--porcelain", "--untracked-files=no"],
        )

        with (
            mock.patch.object(
                megrez_preboard.subprocess,
                "run",
                side_effect=(
                    responses[0],
                    SimpleNamespace(stdout=" M kernel/src/lib.rs\n"),
                ),
            ),
            self.assertRaisesRegex(PreboardError, "clean committed"),
        ):
            megrez_preboard._git_identity(self.repository)


if __name__ == "__main__":
    unittest.main()
