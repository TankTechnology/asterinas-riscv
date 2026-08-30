"""Plan-bound adapter tests for the Megrez Debian desktop QEMU gate."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tools.riscv.debian.rootfs.desktop_m4_gate import DESKTOP_M4_MILESTONES
from tools.riscv.debian.rootfs.desktop_m5_network_gate import (
    DESKTOP_M5_QEMU_MILESTONES,
)
from tools.riscv.debian.rootfs.desktop_m6_browser_gate import (
    DESKTOP_M6_REMOTE_MARKER,
)
from tools.riscv.debian.rootfs.desktop_m7_baidu_gate import (
    DESKTOP_M7_HOME_MARKER,
    DESKTOP_M7_READY_MARKER,
    DESKTOP_M7_SEARCH_MARKER,
)
from tools.riscv.megrez_debug_contract import (
    DEBIAN_BROWSER_ARTIFACT_ORDER,
    DEBIAN_BROWSER_MARKERS,
    ROOT_IMAGE_BYTES,
    ArtifactIdentity,
    DebugPlan,
    StageResult,
)
from tools.riscv.megrez_debug_desktop import (
    DesktopSimulationError,
    simulate_desktop,
)
from tools.riscv.megrez_gmac_gate import physical_bootargs


class MegrezDebugDesktopSimulationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.repository = Path(self.temporary_directory.name)
        self.output = self.repository / "target/megrez-debian-preboard/desktop"
        artifact_directory = self.repository / "artifacts"
        artifact_directory.mkdir()
        addresses = {
            "kernel": 0x80200000,
            "initramfs": 0x83000000,
            "qemu_dtb": 0xF0000000,
            "megrez_dtb": 0xF0000000,
        }
        self.artifacts = tuple(
            ArtifactIdentity(
                name=name,
                path=str((artifact_directory / name).absolute()),
                load_address=addresses.get(name, 0),
                size=ROOT_IMAGE_BYTES if name == "root_image" else 4096,
                sha256=hashlib.sha256(name.encode()).hexdigest(),
                crc32=f"{zlib.crc32(name.encode()):08x}",
            )
            for name in DEBIAN_BROWSER_ARTIFACT_ORDER
        )
        for identity in self.artifacts:
            Path(identity.path).write_bytes(identity.name.encode())
        self.plan = DebugPlan(
            schema_version=2,
            profile="debian-browser",
            artifacts=self.artifacts,
            bootargs=physical_bootargs(600),
            smp=4,
            sv39=True,
            markers=DEBIAN_BROWSER_MARKERS,
            reboot_after=600,
        )

    def _identities(self, _plan: DebugPlan) -> dict[str, ArtifactIdentity]:
        self.assertIs(_plan, self.plan)
        return {identity.name: identity for identity in self.artifacts}

    def _native_result(self) -> dict[str, object]:
        identities = {identity.name: identity for identity in self.artifacts}
        return {
            "debian_release": "13.6",
            "final_root_sha256": "f" * 64,
            "input_sha256": {
                "dtb": identities["qemu_dtb"].sha256,
                "kernel": identities["kernel"].sha256,
                "manifest": identities["root_manifest"].sha256,
                "package_checksums": identities["package_checksums"].sha256,
                "packages_lock": identities["packages_lock"].sha256,
                "root_image": identities["root_image"].sha256,
                "stage1_initramfs": identities["initramfs"].sha256,
                "u_boot": identities["u_boot"].sha256,
            },
            "javascript_screenshot": {
                "distinct_sampled_colors": 200,
                "height": 1024,
                "non_background_pixels": 900000,
                "pixel_count": 1310720,
                "width": 1280,
            },
            "javascript_status": "limited-pass",
            "homepage_screenshot": {
                "distinct_sampled_colors": 200,
                "height": 1024,
                "non_background_pixels": 900000,
                "pixel_count": 1310720,
                "width": 1280,
            },
            "search_screenshot": {
                "distinct_sampled_colors": 200,
                "height": 1024,
                "non_background_pixels": 900000,
                "pixel_count": 1310720,
                "width": 1280,
            },
            "failure_screenshot": {},
            "passed": True,
            "profile": "desktop-m5-network",
            "qemu_argv": [
                [
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
                    "-netdev",
                    "user,id=net0",
                    "-device",
                    "virtio-net-device,netdev=net0",
                    "-device",
                    "bochs-display",
                    "-device",
                    "virtio-keyboard-device",
                    "-device",
                    "virtio-tablet-device",
                    "-device",
                    "virtio-blk-device,drive=bootdisk",
                    "-device",
                    "virtio-blk-device,drive=rootdisk",
                ]
            ],
            "reason": "pass",
            "remote_evidence": True,
            "screenshot": {
                "distinct_sampled_colors": 200,
                "height": 1024,
                "non_background_pixels": 900000,
                "pixel_count": 1310720,
                "width": 1280,
            },
        }

    @staticmethod
    def _transcript() -> bytes:
        markers = (
            *DESKTOP_M5_QEMU_MILESTONES,
            *DESKTOP_M4_MILESTONES,
            DESKTOP_M6_REMOTE_MARKER,
            "DEBIAN_BROWSER_M6_JAVASCRIPT status=limited-pass",
            "DEBIAN_BROWSER_M6_READY remote=baidu javascript=limited-pass",
            DESKTOP_M7_HOME_MARKER,
            DESKTOP_M7_SEARCH_MARKER,
            DESKTOP_M7_READY_MARKER,
        )
        return ("\n".join(markers) + "\n").encode()

    def _runner(
        self,
        calls: list[tuple[tuple[str, ...], dict[str, object]]],
        *,
        native_result: dict[str, object] | None = None,
        returncode: int = 0,
    ):
        def run(
            arguments: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append((tuple(arguments), kwargs))
            output = Path(arguments[arguments.index("--output-directory") + 1])
            output.mkdir(parents=True, exist_ok=True)
            (output / "result.json").write_text(
                json.dumps(
                    self._native_result() if native_result is None else native_result
                )
            )
            (output / "desktop-m7-baidu.serial.log").write_bytes(self._transcript())
            (output / "desktop-m7-baidu.ppm").write_bytes(b"P6\n1 1\n255\n\0\0\0")
            (output / "desktop-m6-javascript.ppm").write_bytes(
                b"P6\n1 1\n255\n\xff\xff\xff"
            )
            (output / "desktop-m7-baidu-home.ppm").write_bytes(
                b"P6\n1 1\n255\n\x80\x80\x80"
            )
            (output / "desktop-m7-baidu-search.ppm").write_bytes(
                b"P6\n1 1\n255\n\x40\x40\x40"
            )
            return subprocess.CompletedProcess(arguments, returncode, "", "")

        return run

    def test_adapter_invokes_m7_with_only_plan_paths_and_binds_result(self) -> None:
        calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
        self.output.mkdir(parents=True, mode=0o700)
        (self.output / "result.json").write_text('{"passed":true}\n')

        result = simulate_desktop(
            self.plan,
            self.output,
            run_command=self._runner(calls),
            artifact_validator=self._identities,
            repository_root=self.repository,
            timeout=840,
        )

        self.assertEqual(result.stage, "desktop")
        self.assertTrue(result.passed)
        self.assertEqual(result.reason, "desktop-pass")
        self.assertEqual(result.plan_sha256, self.plan.plan_sha256)
        self.assertEqual(
            result.evidence,
            (
                "native/result.json",
                "native/desktop-m7-baidu.serial.log",
                "native/desktop-m7-baidu.ppm",
                "native/desktop-m6-javascript.ppm",
                "native/desktop-m7-baidu-home.ppm",
                "native/desktop-m7-baidu-search.ppm",
            ),
        )
        self.assertFalse((self.output / "result.json").exists())
        self.assertEqual(len(calls), 1)
        command, options = calls[0]
        self.assertEqual(
            command[:3],
            (
                sys.executable,
                "-m",
                "tools.riscv.debian.rootfs.desktop_m7_baidu_gate",
            ),
        )
        for option, name in (
            ("--kernel", "kernel"),
            ("--uboot", "u_boot"),
            ("--dtb", "qemu_dtb"),
            ("--stage1-initramfs", "initramfs"),
            ("--root-image", "root_image"),
            ("--root-manifest", "root_manifest"),
            ("--packages-lock", "packages_lock"),
            ("--package-checksums", "package_checksums"),
        ):
            self.assertEqual(
                command[command.index(option) + 1],
                self._identities(self.plan)[name].path,
            )
        self.assertEqual(command[command.index("--smp") + 1], "4")
        self.assertEqual(command[command.index("--boot-timeout") + 1], "720")
        self.assertEqual(options["cwd"], self.repository)
        self.assertLessEqual(float(options["timeout"]), 840)

    def test_adapter_timeout_reserves_image_preparation_budget(self) -> None:
        with self.assertRaisesRegex(DesktopSimulationError, "setup grace"):
            simulate_desktop(
                self.plan,
                self.output,
                run_command=self._runner([]),
                artifact_validator=self._identities,
                repository_root=self.repository,
                timeout=839,
            )

    def test_adapter_rejects_native_identity_or_qemu_contract_drift(self) -> None:
        variants: list[dict[str, object]] = []
        wrong_hash = self._native_result()
        wrong_hash["input_sha256"] = {
            **wrong_hash["input_sha256"],
            "root_image": "0" * 64,
        }
        variants.append(wrong_hash)
        wrong_cpu = self._native_result()
        wrong_cpu["qemu_argv"][0][4] = "rv64"
        variants.append(wrong_cpu)
        no_remote = self._native_result()
        no_remote["remote_evidence"] = False
        variants.append(no_remote)
        weak_screenshot = self._native_result()
        weak_screenshot["screenshot"] = {**weak_screenshot["screenshot"], "width": 1}
        variants.append(weak_screenshot)
        weak_homepage = self._native_result()
        weak_homepage["homepage_screenshot"] = {
            **weak_homepage["homepage_screenshot"],
            "non_background_pixels": 1,
        }
        variants.append(weak_homepage)
        retained_failure = self._native_result()
        retained_failure["failure_screenshot"] = {
            **retained_failure["homepage_screenshot"]
        }
        variants.append(retained_failure)

        for native in variants:
            with (
                self.subTest(native=native),
                self.assertRaises(DesktopSimulationError),
            ):
                simulate_desktop(
                    self.plan,
                    self.output,
                    run_command=self._runner([], native_result=native),
                    artifact_validator=self._identities,
                    repository_root=self.repository,
                )

    def test_adapter_rejects_failed_process_and_incomplete_transcript(self) -> None:
        with self.assertRaisesRegex(DesktopSimulationError, "desktop-qemu-failed"):
            simulate_desktop(
                self.plan,
                self.output,
                run_command=self._runner([], returncode=1),
                artifact_validator=self._identities,
                repository_root=self.repository,
            )

        def incomplete(
            arguments: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            result = self._runner([])(arguments, **kwargs)
            native = Path(arguments[arguments.index("--output-directory") + 1])
            (native / "desktop-m7-baidu.serial.log").write_text("incomplete\n")
            return result

        with self.assertRaisesRegex(DesktopSimulationError, "desktop-evidence"):
            simulate_desktop(
                self.plan,
                self.output,
                run_command=incomplete,
                artifact_validator=self._identities,
                repository_root=self.repository,
            )

    def test_adapter_rejects_non_browser_plan_and_unsafe_output(self) -> None:
        with self.assertRaisesRegex(DesktopSimulationError, "profile"):
            simulate_desktop(
                replace(self.plan, profile="tcp-probe"),
                self.output,
                run_command=self._runner([]),
                artifact_validator=self._identities,
                repository_root=self.repository,
            )

    def test_unified_cli_dispatches_desktop_without_probe_or_uboot_build(self) -> None:
        from tools.riscv import megrez_debug

        plan_path = self.repository / "plan.json"
        plan_path.write_bytes(self.plan.canonical_bytes())
        expected = StageResult(
            schema_version=1,
            stage="desktop",
            passed=True,
            reason="desktop-pass",
            plan_sha256=self.plan.plan_sha256,
            evidence=("native/result.json",),
        )

        class ForbiddenProbe:
            def __init__(self) -> None:
                raise AssertionError("desktop simulation must not start the fast probe")

        with (
            mock.patch.object(megrez_debug, "_check_artifacts"),
            mock.patch.object(
                megrez_debug, "simulate_desktop", return_value=expected, create=True
            ) as simulate,
        ):
            status = megrez_debug.main(
                (
                    "simulate",
                    str(plan_path),
                    "--tier",
                    "desktop",
                    "--output-directory",
                    str(self.output),
                ),
                probe_server_factory=ForbiddenProbe,
            )

        self.assertEqual(status, 0)
        simulate.assert_called_once_with(self.plan, self.output)
        self.assertEqual(
            StageResult.from_bytes((self.output / "result.json").read_bytes()),
            expected,
        )
        unsafe = self.repository / "outside"
        with self.assertRaisesRegex(DesktopSimulationError, "outside-target"):
            simulate_desktop(
                self.plan,
                unsafe,
                run_command=self._runner([]),
                artifact_validator=self._identities,
                repository_root=self.repository,
            )


if __name__ == "__main__":
    unittest.main()
