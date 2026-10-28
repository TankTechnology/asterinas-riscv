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

from tools.riscv.debian.rootfs.desktop_network_gate import NetworkMode
from tools.riscv.megrez_debug_contract import (
    BROWSER_ROOT_IMAGE_BYTES,
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
from tools.riscv.megrez_gmac_gate import GateTarget, physical_bootargs


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
                size=BROWSER_ROOT_IMAGE_BYTES if name == "root_image" else 4096,
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
            bootargs=physical_bootargs(
                600,
                target=GateTarget.FIREFOX,
                network_mode=NetworkMode.PROXY,
            ),
            smp=4,
            sv39=True,
            markers=DEBIAN_BROWSER_MARKERS,
            reboot_after=600,
        )

    def _identities(self, _plan: DebugPlan) -> dict[str, ArtifactIdentity]:
        self.assertIs(_plan, self.plan)
        return {identity.name: identity for identity in self.artifacts}

    def _browser_plan(self) -> DebugPlan:
        return self.plan

    @staticmethod
    def _browser_protocol() -> tuple[dict[str, object], ...]:
        cycles: list[dict[str, object]] = []
        for cycle in range(1, 4):
            cycles.append(
                {
                    "cycle": cycle,
                    "nonce_sha256": hashlib.sha256(
                        f"nonce-{cycle}".encode()
                    ).hexdigest(),
                    "key_downs": 4,
                    "relative_events": 0,
                    "absolute_events": 4,
                    "left_down": 1,
                    "left_up": 1,
                    "input_latency_count": 4,
                    "input_latency_min_ms": 1.0,
                    "input_latency_p50_ms": 2.0,
                    "input_latency_p95_ms": 4.0,
                    "input_latency_max_ms": 4.0,
                    "evdev_sha256": hashlib.sha256(
                        f"events-{cycle}".encode()
                    ).hexdigest(),
                    "screenshot_sha256": hashlib.sha256(
                        f"png-{cycle}".encode()
                    ).hexdigest(),
                }
            )
        return tuple(cycles)

    def _browser_native_result(self, plan: DebugPlan) -> dict[str, object]:
        identities = {identity.name: identity for identity in plan.artifacts}
        cycles = self._browser_protocol()
        rendered = {
            "distinct_sampled_colors": 200,
            "height": 1024,
            "non_background_pixels": 900000,
            "pixel_count": 1310720,
            "width": 1280,
        }
        return {
            "cycle_artifacts": [
                {
                    "cycle": cycle["cycle"],
                    "nonce_sha256": cycle["nonce_sha256"],
                    "guest_png_sha256": cycle["screenshot_sha256"],
                    "rendered_ppm_sha256": hashlib.sha256(
                        f"ppm-{cycle['cycle']}".encode()
                    ).hexdigest(),
                    "rendered": rendered,
                }
                for cycle in cycles
            ],
            "debian_release": "13.6",
            "debug_console": {
                "desktop_state": "active",
                "graphical_state": "active",
                "pid1": "systemd",
                "root_device": "/dev/vdb",
                "root_filesystem": "ext2",
                "uid": 0,
            },
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
            "interaction_cycles": list(cycles),
            "passed": True,
            "physical": False,
            "profile": "browser-web",
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
            "screenshot": {},
            "target": "browser",
        }

    def _browser_runner(
        self,
        calls: list[tuple[tuple[str, ...], dict[str, object]]],
        plan: DebugPlan,
        *,
        native_result: dict[str, object] | None = None,
    ):
        def run(
            arguments: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append((tuple(arguments), kwargs))
            output = Path(arguments[arguments.index("--output-directory") + 1])
            output.mkdir(parents=True, exist_ok=True)
            native = (
                self._browser_native_result(plan)
                if native_result is None
                else native_result
            )
            (output / "result.json").write_text(json.dumps(native))
            lines: list[str] = []
            for cycle in self._browser_protocol():
                number = cycle["cycle"]
                nonce_hash = cycle["nonce_sha256"]
                lines.extend(
                    (
                        f"ASTERINAS_PHYSICAL_GRAPHICS_READY cycle={number} nonce_sha256={nonce_hash}",
                        f"ASTERINAS_PHYSICAL_GRAPHICS_KEY_READY cycle={number} nonce_sha256={nonce_hash}",
                        f"ASTERINAS_PHYSICAL_GRAPHICS_POINTER_READY cycle={number}",
                        f"ASTERINAS_PHYSICAL_GRAPHICS_INPUT cycle={number} key_downs=4 relative_events=0 absolute_events=4 left_down=1 left_up=1 digest={cycle['evdev_sha256']}",
                        f"ASTERINAS_PHYSICAL_GRAPHICS_DOM cycle={number} nonce_sha256={nonce_hash} trusted_key=1 trusted_input=1 trusted_pointer=1 trusted_click=1 click_count=1 color=cyan",
                        f"ASTERINAS_PHYSICAL_GRAPHICS_LATENCY cycle={number} count=4 min_ms=1.000 p50_ms=2.000 p95_ms=4.000 max_ms=4.000",
                        f"ASTERINAS_PHYSICAL_GRAPHICS_SCREENSHOT cycle={number} sha256={cycle['screenshot_sha256']}",
                        f"ASTERINAS_PHYSICAL_GRAPHICS_PASS cycle={number}",
                    )
                )
                (output / f"physical-graphics-qemu-cycle-{number}.png").write_bytes(
                    f"png-{number}".encode()
                )
                (output / f"physical-graphics-qemu-cycle-{number}.ppm").write_bytes(
                    f"ppm-{number}".encode()
                )
            lines.extend(
                (
                    "__ASTERINAS_PHYSICAL_FINAL__ cycle=3 "
                    f"nonce_sha256={self._browser_protocol()[-1]['nonce_sha256']}",
                    "ASTERINAS_PHYSICAL_GRAPHICS_COMPLETE cycles=3",
                )
            )
            (output / "physical-graphics-qemu.serial.log").write_text(
                "\n".join(lines) + "\n"
            )
            return subprocess.CompletedProcess(arguments, 0, "", "")

        return run

    def test_adapter_binds_browser_web_physical_graphics_qemu_result(self) -> None:
        calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
        plan = self._browser_plan()
        identities = {identity.name: identity for identity in plan.artifacts}

        result = simulate_desktop(
            plan,
            self.output,
            run_command=self._browser_runner(calls, plan),
            artifact_validator=lambda candidate: identities,
            repository_root=self.repository,
        )

        self.assertEqual(result.reason, "desktop-pass")
        self.assertEqual(result.plan_sha256, plan.plan_sha256)
        self.assertEqual(
            result.evidence,
            (
                "native/result.json",
                "native/physical-graphics-qemu.serial.log",
                "native/physical-graphics-qemu-cycle-1.png",
                "native/physical-graphics-qemu-cycle-1.ppm",
                "native/physical-graphics-qemu-cycle-2.png",
                "native/physical-graphics-qemu-cycle-2.ppm",
                "native/physical-graphics-qemu-cycle-3.png",
                "native/physical-graphics-qemu-cycle-3.ppm",
            ),
        )
        command, options = calls[0]
        self.assertEqual(
            command[:3],
            (sys.executable, "-m", "tools.riscv.physical_graphics_qemu_gate"),
        )
        self.assertEqual(command[command.index("--command-timeout") + 1], "300")
        self.assertEqual(command[command.index("--boot-timeout") + 1], "1800")
        self.assertGreater(float(options["timeout"]), 1800)

    def test_adapter_reuses_fully_validated_native_browser_evidence(self) -> None:
        plan = self._browser_plan()
        identities = {identity.name: identity for identity in plan.artifacts}
        simulate_desktop(
            plan,
            self.output,
            run_command=self._browser_runner([], plan),
            artifact_validator=lambda candidate: identities,
            repository_root=self.repository,
        )

        def unexpected_run(*_args: object, **_kwargs: object) -> object:
            self.fail("reuse_native must not launch QEMU")

        result = simulate_desktop(
            plan,
            self.output,
            run_command=unexpected_run,
            artifact_validator=lambda candidate: identities,
            repository_root=self.repository,
            reuse_native=True,
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.reason, "desktop-pass")

    def test_browser_adapter_rejects_identity_and_evidence_drift(self) -> None:
        plan = self._browser_plan()
        identities = {identity.name: identity for identity in plan.artifacts}
        invalid_results: list[dict[str, object]] = []

        physical = self._browser_native_result(plan)
        physical["physical"] = True
        invalid_results.append(physical)

        reused_nonce = self._browser_native_result(plan)
        reused_nonce["interaction_cycles"][1]["nonce_sha256"] = reused_nonce[
            "interaction_cycles"
        ][0]["nonce_sha256"]
        invalid_results.append(reused_nonce)

        wrong_png = self._browser_native_result(plan)
        wrong_png["cycle_artifacts"][0]["guest_png_sha256"] = "0" * 64
        invalid_results.append(wrong_png)

        for native in invalid_results:
            with (
                self.subTest(native=native),
                self.assertRaises(DesktopSimulationError),
            ):
                simulate_desktop(
                    plan,
                    self.output,
                    run_command=self._browser_runner([], plan, native_result=native),
                    artifact_validator=lambda candidate: identities,
                    repository_root=self.repository,
                )

        runner = self._browser_runner([], plan)

        def fatal_transcript(
            arguments: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            result = runner(arguments, **kwargs)
            native = Path(arguments[arguments.index("--output-directory") + 1])
            with (native / "physical-graphics-qemu.serial.log").open("a") as stream:
                stream.write("Kernel panic - not syncing\n")
            return result

        with self.assertRaisesRegex(DesktopSimulationError, "fatal serial marker"):
            simulate_desktop(
                plan,
                self.output,
                run_command=fatal_transcript,
                artifact_validator=lambda candidate: identities,
                repository_root=self.repository,
            )

    def test_adapter_timeout_reserves_image_preparation_budget(self) -> None:
        with self.assertRaisesRegex(DesktopSimulationError, "setup grace"):
            simulate_desktop(
                self.plan,
                self.output,
                run_command=self._browser_runner([], self.plan),
                artifact_validator=self._identities,
                repository_root=self.repository,
                timeout=839,
            )

    def test_adapter_rejects_non_browser_plan_and_unsafe_output(self) -> None:
        with self.assertRaisesRegex(DesktopSimulationError, "profile"):
            simulate_desktop(
                replace(self.plan, profile="tcp-probe"),
                self.output,
                run_command=self._browser_runner([], self.plan),
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
        simulate.assert_called_once_with(
            self.plan, self.output, reuse_native=False
        )
        self.assertEqual(
            StageResult.from_bytes((self.output / "result.json").read_bytes()),
            expected,
        )
        unsafe = self.repository / "outside"
        with self.assertRaisesRegex(DesktopSimulationError, "outside-target"):
            simulate_desktop(
                self.plan,
                unsafe,
                run_command=self._browser_runner([], self.plan),
                artifact_validator=self._identities,
                repository_root=self.repository,
            )


if __name__ == "__main__":
    unittest.main()
