#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(TOOLS))

from drm.scanout_gate import (  # noqa: E402
    SCANOUT_ADDFB_MARKER,
    SCANOUT_CAP_MARKER,
    SCANOUT_CLEANUP_MARKER,
    SCANOUT_CONNECTOR_MARKER,
    SCANOUT_DEVICE_MARKER,
    SCANOUT_DUMB_MARKER,
    SCANOUT_GETCRTC_MARKER,
    SCANOUT_READBACK_MARKER,
    SCANOUT_READY_MARKER,
    SCANOUT_RESOURCES_MARKER,
    SCANOUT_SETCRTC_MARKER,
    SCANOUT_VERSION_MARKER,
    ScanoutGateConfig,
    classify_transcript,
    run_scanout_gate,
)
from qemu_uboot_commands import qemu_argv  # noqa: E402
from qemu_uboot_devices import (  # noqa: E402
    DRM_SCANOUT,
    DRM_SCANOUT_GTK,
    RuntimeDevicePaths,
    device_set_by_name,
)
from qemu_uboot_profiles import (  # noqa: E402
    GENERIC_SV39_DRM_SCANOUT_SMP4,
    profile_by_name,
)


class DrmScanoutContractTests(unittest.TestCase):
    def test_driver_name_uses_mesa_abi_spelling(self) -> None:
        """The DRM driver name is virtio_gpu, even though the device is virtio-gpu."""

        self.assertEqual(SCANOUT_VERSION_MARKER, b"DRM_SCANOUT_VERSION name=virtio_gpu")

    @staticmethod
    def passing_transcript() -> bytes:
        return b"\n".join(
            (
                b"virtio_gpu_cmd_res_create_2d res 0x1, fmt 0x2, w 1024, h 768",
                b"virtio_gpu_cmd_res_back_attach res 0x1",
                b"virtio_gpu_cmd_set_scanout id 0, res 0x1, w 1024, h 768, x 0, y 0",
                b"virtio_gpu_cmd_res_xfer_toh_2d res 0x1",
                b"virtio_gpu_cmd_res_flush res 0x1, w 1024, h 768, x 0, y 0",
                SCANOUT_DEVICE_MARKER,
                SCANOUT_VERSION_MARKER,
                SCANOUT_CAP_MARKER,
                SCANOUT_RESOURCES_MARKER,
                SCANOUT_CONNECTOR_MARKER,
                SCANOUT_DUMB_MARKER,
                SCANOUT_READBACK_MARKER,
                SCANOUT_ADDFB_MARKER,
                SCANOUT_SETCRTC_MARKER,
                SCANOUT_GETCRTC_MARKER,
                SCANOUT_CLEANUP_MARKER,
                b"virtio_gpu_cmd_res_create_2d res 0x2, fmt 0x2, w 1024, h 768",
                b"virtio_gpu_cmd_res_back_attach res 0x2",
                b"virtio_gpu_cmd_set_scanout id 0, res 0x2, w 1024, h 768, x 0, y 0",
                b"virtio_gpu_cmd_res_xfer_toh_2d res 0x2",
                b"virtio_gpu_cmd_res_flush res 0x2, w 1024, h 768, x 0, y 0",
                SCANOUT_READY_MARKER,
            )
        )

    def test_profile_and_device_are_independent_from_cursor_gate(self) -> None:
        self.assertIs(profile_by_name("generic-sv39-drm-scanout-smp4"), GENERIC_SV39_DRM_SCANOUT_SMP4)
        self.assertIs(device_set_by_name("drm-scanout"), DRM_SCANOUT)
        argv = qemu_argv(
            uboot=Path("/inputs/u-boot"),
            boot_disk=Path("/inputs/boot.ext4"),
            profile=GENERIC_SV39_DRM_SCANOUT_SMP4,
            device_set=DRM_SCANOUT,
        )
        joined = " ".join(argv)
        self.assertIn("virtio-gpu-device", joined)
        self.assertIn("virtio_gpu_cmd_res_create_2d", joined)
        self.assertNotIn("virtio_gpu_update_cursor", joined)
        self.assertNotIn("virtio-net", joined)

    def test_gtk_scanout_backend_is_explicit_and_keeps_the_same_contract(self) -> None:
        self.assertIs(device_set_by_name("drm-scanout-gtk"), DRM_SCANOUT_GTK)
        self.assertNotEqual(DRM_SCANOUT_GTK.capture_contract, DRM_SCANOUT.capture_contract)
        assert DRM_SCANOUT_GTK.capture_contract is not None
        self.assertEqual((DRM_SCANOUT_GTK.capture_contract.width, DRM_SCANOUT_GTK.capture_contract.height), (640, 480))
        self.assertEqual(DRM_SCANOUT_GTK.display, "gtk")
        argv = qemu_argv(
            uboot=Path("/inputs/u-boot"),
            boot_disk=Path("/inputs/boot.ext4"),
            profile=GENERIC_SV39_DRM_SCANOUT_SMP4,
            device_set=DRM_SCANOUT_GTK,
        )
        self.assertEqual(argv[argv.index("-display") + 1], "gtk")
        self.assertIn("virtio-gpu-device", argv)

    def test_capture_contract_adds_qmp_only_for_a_private_capture_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            argv = qemu_argv(
                uboot=Path("/inputs/u-boot"),
                boot_disk=Path("/inputs/boot.ext4"),
                profile=GENERIC_SV39_DRM_SCANOUT_SMP4,
                device_set=DRM_SCANOUT,
                device_paths=RuntimeDevicePaths(
                    capture_root=root,
                    monitor_socket=root / "qmp.sock",
                ),
            )
            joined = " ".join(argv)
            self.assertIn("-qmp unix:", joined)
            self.assertIn("virtio-gpu-device", joined)

    def test_requires_guest_markers_and_ordered_scanout_trace(self) -> None:
        result = classify_transcript(self.passing_transcript())
        self.assertTrue(result.passed, result.reason)
        self.assertEqual(result.trace_count, 5)
        self.assertFalse(classify_transcript(self.passing_transcript().replace(SCANOUT_SETCRTC_MARKER, SCANOUT_GETCRTC_MARKER, 1)).passed)
        self.assertFalse(classify_transcript(self.passing_transcript() + b"\nDEBIAN_DESKTOP_M4_READY").passed)
        transcript = self.passing_transcript()
        marker_end = transcript.index(SCANOUT_DEVICE_MARKER) + len(SCANOUT_DEVICE_MARKER)
        tail = transcript[marker_end:].replace(
            b"virtio_gpu_cmd_res_flush", b"virtio_gpu_cmd_res_flush_bad", 1
        )
        self.assertFalse(classify_transcript(transcript[:marker_end] + tail).passed)

    def test_setcrtc_transfer_and_flush_faults_fail_closed(self) -> None:
        transcript = self.passing_transcript()
        marker_end = transcript.index(SCANOUT_DEVICE_MARKER) + len(SCANOUT_DEVICE_MARKER)
        tail = transcript[marker_end:]
        faults = {
            "setcrtc": SCANOUT_SETCRTC_MARKER,
            "transfer": b"virtio_gpu_cmd_res_xfer_toh_2d",
            "flush": b"virtio_gpu_cmd_res_flush",
        }
        for name, marker in faults.items():
            with self.subTest(name=name):
                if name == "setcrtc":
                    mutated = tail.replace(marker, b"DRM_SCANOUT_SETCRTC_FAULT", 1)
                else:
                    mutated = tail.replace(marker, marker + b"_fault", 1)
                result = classify_transcript(transcript[:marker_end] + mutated)
                self.assertFalse(result.passed)
                self.assertIn(name, result.reason)

    def test_duplicate_scanout_command_fails_closed(self) -> None:
        """A repeated transfer/flush cannot masquerade as one clean frame."""

        transcript = self.passing_transcript()
        marker_end = transcript.index(SCANOUT_DEVICE_MARKER) + len(SCANOUT_DEVICE_MARKER)
        tail = transcript[marker_end:]
        transfer = b"virtio_gpu_cmd_res_xfer_toh_2d res 0x2"
        duplicate = transfer + b"\n" + transfer
        mutated = tail.replace(transfer, duplicate, 1)
        result = classify_transcript(transcript[:marker_end] + mutated)
        self.assertFalse(result.passed)
        self.assertIn("trace count", result.reason)

    def test_runtime_invalidates_stale_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            output.mkdir()
            (output / "result.json").write_text('{"passed": true}\n')

            def fake_runner(**arguments: object) -> object:
                Path(arguments["serial_log"]).write_bytes(self.passing_transcript())
                return type("BaseResult", (), {"passed": True})()

            result = run_scanout_gate(
                ScanoutGateConfig(
                    uboot=Path("/inputs/u-boot"),
                    boot_disk=Path("/inputs/boot.ext4"),
                    manifest=Path("/inputs/artifacts.json"),
                    output_directory=output,
                ),
                runner=fake_runner,
            )
            self.assertTrue(result.passed, result.reason)
            self.assertEqual(result.device_set, DRM_SCANOUT.name)
            self.assertEqual(result.display_backend, "none")
            self.assertTrue(json.loads((output / "result.json").read_text())["passed"])

    def test_failed_attempt_can_recover_without_stale_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            output.mkdir()
            calls = 0

            def runner(**arguments: object) -> object:
                nonlocal calls
                calls += 1
                payload = self.passing_transcript()
                if calls == 1:
                    marker_end = payload.index(SCANOUT_DEVICE_MARKER) + len(SCANOUT_DEVICE_MARKER)
                    payload = payload[:marker_end] + payload[marker_end:].replace(
                        b"virtio_gpu_cmd_res_flush", b"virtio_gpu_cmd_res_flush_fault", 1
                    )
                Path(arguments["serial_log"]).write_bytes(payload)
                return type("BaseResult", (), {"passed": True})()

            config = ScanoutGateConfig(
                uboot=Path("/inputs/u-boot"),
                boot_disk=Path("/inputs/boot.ext4"),
                manifest=Path("/inputs/artifacts.json"),
                output_directory=output,
            )
            self.assertFalse(run_scanout_gate(config, runner=runner).passed)
            self.assertTrue(run_scanout_gate(config, runner=runner).passed)
            self.assertEqual(json.loads((output / "result.json").read_text())["passed"], True)


class DrmScanoutGuestProbeTests(unittest.TestCase):
    def test_native_layout_self_test_compiles(self) -> None:
        source = TOOLS / "drm/scanout_gate_init.c"
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "scanout-self-test"
            result = subprocess.run(
                ["cc", "-std=c11", "-O2", "-static", "-Wall", "-Wextra", "-Werror", "-DDRM_SCANOUT_GATE_SELF_TEST", os.fspath(source), "-o", os.fspath(binary)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            run = subprocess.run([binary], capture_output=True, text=True, check=False)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout, "DRM_SCANOUT_SELF_TEST PASS layout\n")


if __name__ == "__main__":
    unittest.main()
