#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from qemu_uboot_commands import qemu_argv  # noqa: E402
from qemu_uboot_devices import (  # noqa: E402
    DRM_FIRMWARE,
    RuntimeDevicePaths,
    device_set_by_name,
)
from qemu_uboot_profiles import (  # noqa: E402
    GENERIC_SV39_DRM_FIRMWARE_SMP4,
    profile_by_name,
)
from drm.firmware_gate import (  # noqa: E402
    DRIVER_MARKER,
    FBDEV_MARKER,
    MAX_MARKER,
    MAX_TRANSCRIPT_BYTES,
    PATTERN_MARKER,
    PRESENT_MARKER,
    READY_MARKER,
    classify_transcript,
)


def a_transcript(*, skip: bytes | None = None) -> bytes:
    """The guest sequence this gate wants, optionally with one stage removed."""

    markers = (
        DRIVER_MARKER,
        MAX_MARKER,
        PRESENT_MARKER,
        PATTERN_MARKER,
        FBDEV_MARKER,
        READY_MARKER,
    )
    return b"".join(
        b"boot noise\n" + marker + b"\n"
        for marker in markers
        if marker != skip
    )


class DrmFirmwareLaunchContractTests(unittest.TestCase):
    def test_registered_profile_is_generic_sv39_smp4(self) -> None:
        profile = profile_by_name("generic-sv39-drm-firmware-smp4")
        self.assertIs(profile, GENERIC_SV39_DRM_FIRMWARE_SMP4)
        self.assertEqual(profile.hart_count, 4)
        self.assertEqual(profile.memory, "2G")
        self.assertEqual(profile.mmu_type, "riscv,sv39")
        self.assertEqual(profile.validation.completion_line, READY_MARKER)

    def test_the_machine_has_a_display_and_deliberately_no_gpu(self) -> None:
        """The absent GPU is the experiment, so it is asserted rather than assumed."""

        self.assertIs(device_set_by_name("drm-firmware"), DRM_FIRMWARE)
        with tempfile.TemporaryDirectory() as capture_root:
            joined = " ".join(
                qemu_argv(
                    uboot=Path("/inputs/u-boot"),
                    boot_disk=Path("/inputs/boot.ext4"),
                    profile=GENERIC_SV39_DRM_FIRMWARE_SMP4,
                    device_set=DRM_FIRMWARE,
                    # A framebuffer device set is the only kind that can be
                    # screenshotted, so the contract requires a real directory
                    # to capture into and the QMP socket that takes it.
                    device_paths=RuntimeDevicePaths(
                        capture_root=Path(capture_root),
                        monitor_socket=Path(capture_root) / "qmp.sock",
                    ),
                )
            )
        self.assertIn("-smp 4", joined)
        self.assertIn("-m 2G", joined)
        self.assertIn("rv64,sv48=false", joined)
        self.assertIn("bochs-display,xres=1280,yres=1024", joined)
        # With a virtio-gpu present the driver would select it, the firmware
        # backend would go unused, and this gate would pass without testing it.
        self.assertNotIn("virtio-gpu", joined)


class DrmFirmwareClassifierTests(unittest.TestCase):
    def test_the_ordered_sequence_passes(self) -> None:
        result = classify_transcript(a_transcript())
        self.assertTrue(result.passed)
        self.assertEqual(result.stage_count, 6)

    def test_each_stage_is_required(self) -> None:
        for index, marker in enumerate(
            (
                DRIVER_MARKER,
                MAX_MARKER,
                PRESENT_MARKER,
                PATTERN_MARKER,
                FBDEV_MARKER,
                READY_MARKER,
            )
        ):
            with self.subTest(stage=marker.decode()):
                result = classify_transcript(a_transcript(skip=marker))
                self.assertFalse(result.passed)
                self.assertEqual(result.stage_count, index)

    def test_the_same_markers_in_another_order_are_not_a_pass(self) -> None:
        # Every marker present, but the pixel comparison before the present
        # that produces the pixels. Presence is not the claim; order is.
        transcript = (
            DRIVER_MARKER + MAX_MARKER + FBDEV_MARKER + PATTERN_MARKER
            + PRESENT_MARKER + READY_MARKER
        )
        result = classify_transcript(transcript)
        self.assertFalse(result.passed)
        self.assertIn("unordered", result.reason)

    def test_a_wrong_pixel_is_reported_with_its_coordinates(self) -> None:
        transcript = (
            a_transcript(skip=FBDEV_MARKER)
            + b"DRM_FIRMWARE_MISMATCH x=640 y=512 found=00000000 want=ff802080\n"
        )
        result = classify_transcript(transcript)
        self.assertFalse(result.passed)
        self.assertEqual(result.failed_stage, "pixels")
        self.assertIn("(640,512)", result.reason)
        self.assertIn("0x00000000", result.reason)
        self.assertIn("0xff802080", result.reason)

    def test_the_guest_names_the_stage_that_failed(self) -> None:
        transcript = a_transcript(skip=FBDEV_MARKER) + b"DRM_FIRMWARE_FAIL stage=setcrtc errno=-22\n"
        result = classify_transcript(transcript)
        self.assertFalse(result.passed)
        self.assertEqual(result.failed_stage, "setcrtc")
        self.assertEqual(result.reason, "guest reported setcrtc failure: errno -22")

    def test_a_panic_is_never_a_pass(self) -> None:
        transcript = a_transcript() + b"Uncaught panic\n"
        result = classify_transcript(transcript)
        self.assertFalse(result.passed)
        self.assertIn("fatal marker", result.reason)

    def test_a_transcript_over_the_cap_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            classify_transcript(b"x" * (MAX_TRANSCRIPT_BYTES + 1))


if __name__ == "__main__":
    unittest.main()
