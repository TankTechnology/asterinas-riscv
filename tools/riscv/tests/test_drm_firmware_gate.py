#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
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
    DIRTYFB_MARKER,
    DRIVER_MARKER,
    MAX_MARKER,
    MAX_TRANSCRIPT_BYTES,
    PAGEFLIP_MARKER,
    READY_MARKER,
    SETCRTC_MARKER,
    FirmwareGateConfig,
    classify_transcript,
    run_firmware_gate,
)

#: Every marker the guest emits, in the order it emits them.
STAGE_MARKERS = (
    DRIVER_MARKER,
    MAX_MARKER,
    SETCRTC_MARKER,
    PAGEFLIP_MARKER,
    DIRTYFB_MARKER,
    READY_MARKER,
)


def a_transcript(*, skip: bytes | None = None) -> bytes:
    """The guest sequence this gate wants, optionally with one stage removed."""

    return b"".join(
        b"boot noise\n" + marker + b"\n"
        for marker in STAGE_MARKERS
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
        for index, marker in enumerate(STAGE_MARKERS):
            with self.subTest(stage=marker.decode()):
                result = classify_transcript(a_transcript(skip=marker))
                self.assertFalse(result.passed)
                self.assertEqual(result.stage_count, index)

    def test_the_same_markers_in_another_order_are_not_a_pass(self) -> None:
        # Every marker present, but the page flip before the setcrtc that puts
        # the first frame up. Presence is not the claim; order is.
        transcript = (
            DRIVER_MARKER + MAX_MARKER + PAGEFLIP_MARKER + SETCRTC_MARKER
            + DIRTYFB_MARKER + READY_MARKER
        )
        result = classify_transcript(transcript)
        self.assertFalse(result.passed)
        self.assertIn("unordered", result.reason)

    def test_all_three_present_paths_are_required(self) -> None:
        # Each is a different ioctl reaching the same place by a different
        # route, so a gate that covered one would leave the other two to be
        # found broken by a client. Named individually because a single
        # "presented a frame" marker would not say which route was tested.
        for marker, stage in (
            (SETCRTC_MARKER, "setcrtc"),
            (PAGEFLIP_MARKER, "page-flip"),
            (DIRTYFB_MARKER, "dirtyfb"),
        ):
            with self.subTest(stage=stage):
                result = classify_transcript(a_transcript(skip=marker))
                self.assertFalse(result.passed)

    def test_a_wrong_pixel_is_reported_with_its_coordinates_and_side(self) -> None:
        # The side matters: a mismatch *outside* the damage means the whole
        # frame was re-presented, which no marker count would show.
        transcript = (
            a_transcript(skip=DIRTYFB_MARKER)
            + b"DRM_FIRMWARE_MISMATCH x=640 y=512 found=00000000 want=ff802080 "
            + b"stage=dirtyfb where=outside\n"
        )
        result = classify_transcript(transcript)
        self.assertFalse(result.passed)
        self.assertEqual(result.failed_stage, "dirtyfb")
        self.assertIn("(640,512)", result.reason)
        self.assertIn("outside the damage", result.reason)
        self.assertIn("0x00000000", result.reason)
        self.assertIn("0xff802080", result.reason)

    def test_the_guest_names_the_stage_that_failed(self) -> None:
        for line, expected in (
            (b"DRM_FIRMWARE_FAIL stage=setcrtc errno=-22\n",
             "guest reported setcrtc failure: errno -22"),
            # The probe also decides things the kernel cannot report, and a
            # detail is not an errno; both have to reach the reason.
            (b"DRM_FIRMWARE_FAIL stage=dirtyfb detail=rect-not-copied\n",
             "guest reported dirtyfb failure: rect-not-copied"),
        ):
            with self.subTest(line=line.decode().strip()):
                result = classify_transcript(
                    a_transcript(skip=DIRTYFB_MARKER) + line
                )
                self.assertFalse(result.passed)
                self.assertEqual(result.reason, expected)

    def test_a_panic_is_never_a_pass(self) -> None:
        transcript = a_transcript() + b"Uncaught panic\n"
        result = classify_transcript(transcript)
        self.assertFalse(result.passed)
        self.assertIn("fatal marker", result.reason)

    def test_a_transcript_over_the_cap_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            classify_transcript(b"x" * (MAX_TRANSCRIPT_BYTES + 1))


class DrmFirmwareRunContractTests(unittest.TestCase):
    def test_the_run_asks_the_runner_for_the_picture_it_is_grading(self) -> None:
        """A framebuffer device set is refused unless its display is captured.

        This is not a detail of the runner: the gate did not pass a screenshot
        or a display audit, and the run died before QEMU started with
        `framebuffer device set requires positive display outputs`. Nothing
        about the kernel could have shown that -- the gate simply could not
        launch -- so the requirement is pinned here, where it fails in a
        second instead of after a boot.
        """

        captured: dict[str, object] = {}

        def fake_runner(**arguments: object) -> object:
            captured.update(arguments)
            Path(arguments["serial_log"]).write_bytes(a_transcript())
            return type("BaseResult", (), {"passed": True})()

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "evidence"
            output.mkdir()
            result = run_firmware_gate(
                FirmwareGateConfig(
                    uboot=Path("/inputs/u-boot"),
                    boot_disk=Path("/inputs/boot.ext4"),
                    manifest=Path("/inputs/artifacts.json"),
                    output_directory=output,
                ),
                runner=fake_runner,
            )

        self.assertTrue(result.passed, result.reason)
        self.assertIs(captured["device_set"], DRM_FIRMWARE)
        self.assertIs(captured["profile"], GENERIC_SV39_DRM_FIRMWARE_SMP4)
        self.assertIsNotNone(captured["screenshot"])
        self.assertIsNotNone(captured["display_audit"])

    def test_failure_never_leaves_a_stale_pass_behind(self) -> None:
        def failing_runner(**_arguments: object) -> object:
            raise RuntimeError("launch failed")

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "evidence"
            output.mkdir()
            (output / "result.json").write_text('{"passed": true}\n')

            result = run_firmware_gate(
                FirmwareGateConfig(
                    uboot=Path("/inputs/u-boot"),
                    boot_disk=Path("/inputs/boot.ext4"),
                    manifest=Path("/inputs/artifacts.json"),
                    output_directory=output,
                ),
                runner=failing_runner,
            )

            self.assertFalse(result.passed)
            self.assertEqual(
                json.loads((output / "result.json").read_text())["passed"], False
            )


if __name__ == "__main__":
    unittest.main()
