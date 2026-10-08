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
# `megrez_board_session` imports `tools.riscv.*`, so the repository root has to
# be importable too -- the board modules are reached as a package, not as
# siblings of the QEMU ones. Inserted rather than relied on from the caller's
# cwd, so the cross-check below runs the same way from anywhere.
REPO_ROOT = TOOLS.parents[1]
sys.path.insert(0, str(REPO_ROOT))

from qemu_uboot_commands import boot_commands, qemu_argv  # noqa: E402
from qemu_uboot_devices import (  # noqa: E402
    DRM_FIRMWARE,
    MEGREZ_BASIC,
    MEGREZ_BOARD_GEOMETRY,
    DeviceKind,
    RuntimeDevicePaths,
    device_set_by_name,
)
from qemu_uboot_profiles import (  # noqa: E402
    DRM_FIRMWARE_GATE,
    GENERIC_SV39_DRM_FIRMWARE_SMP4,
    MEGREZ_SV48_SVADE_DRM_FIRMWARE,
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


class MegrezDrmFirmwareProfileTests(unittest.TestCase):
    """The board's machine contract is a different machine, not a rename.

    Every DRM gate before this one ran on `qemu-virt` with `sv48=false` and
    Sv39 paging. The board runs Sv48, with `svpbmt` and `zkr` absent. The
    kernel's VA constants are computed from the paging mode
    (`ADDRESS_WIDTH` 39 vs 48 feeds `KERNEL_BASE_VADDR`,
    `LINEAR_MAPPING_BASE_VADDR`, `VMALLOC_BASE_VADDR`), so "the firmware
    backend copies pixels into the framebuffer" is a claim that has to be
    re-made under the mode the board will actually use. These assertions pin
    the machine that re-makes it.
    """

    def test_the_megrez_profile_is_registered_and_is_sv48(self) -> None:
        profile = profile_by_name("megrez-sv48-svade-drm-firmware")
        self.assertIs(profile, MEGREZ_SV48_SVADE_DRM_FIRMWARE)
        self.assertEqual(profile.mmu_type, "riscv,sv48")
        self.assertEqual(profile.hart_count, 4)
        self.assertEqual(profile.memory, "2G")
        # `sv48` is not disabled here, which is what makes an Sv48 kernel
        # bootable: the generic DRM profile's `sv48=false` would hang it at
        # `Starting kernel ...` with no output at all.
        self.assertNotIn("sv48=false", profile.cpu)
        self.assertIn("svpbmt=false", profile.cpu)
        self.assertIn("zkr=false", profile.cpu)

    def test_the_megrez_run_grades_the_same_claim_as_the_generic_one(self) -> None:
        """One claim, one scenario: only the machine may differ.

        If the two runs graded different scenarios they could disagree about
        what "passes", and the disagreement would be invisible -- both would
        report a pass. Sharing the scenario object makes that impossible, and
        sharing the bootargs keeps the guest command line out of the
        difference: `console=ttyS0` is what puts the transcript on the serial
        line this gate reads, and the Megrez profiles' other token
        (`cpu_no_boost_1_6ghz`) appears in no Rust source in this tree, so it
        cannot change what the kernel does.
        """

        self.assertIs(
            MEGREZ_SV48_SVADE_DRM_FIRMWARE.validation, DRM_FIRMWARE_GATE
        )
        self.assertEqual(
            MEGREZ_SV48_SVADE_DRM_FIRMWARE.bootargs,
            GENERIC_SV39_DRM_FIRMWARE_SMP4.bootargs,
        )
        self.assertEqual(
            MEGREZ_SV48_SVADE_DRM_FIRMWARE.validation.completion_line,
            READY_MARKER,
        )

    def test_the_device_set_carries_a_framebuffer_and_no_gpu(self) -> None:
        device_set = device_set_by_name("megrez-basic")
        self.assertIs(device_set, MEGREZ_BASIC)
        self.assertIsNotNone(device_set.framebuffer)
        # The absent GPU is the experiment here too: with a virtio-gpu the
        # driver would select it and the firmware backend would go untested.
        self.assertNotIn(DeviceKind.VIRTIO_GPU, device_set.devices)
        self.assertNotIn(DeviceKind.VIRTIO_GPU_GL, device_set.devices)


class BoardFramebufferContractTests(unittest.TestCase):
    """The run that stands in for the board uses the board's own geometry.

    `MEGREZ_FRAMEBUFFER` lives in `megrez_board_session.py` and is what the
    physical session injects into the DTB. The QEMU device set restates the
    layout, because the two modules are used from opposite ends of the tree
    and neither should import the other's world. Restating it is only safe if
    a test holds the two together -- otherwise the stand-in silently drifts
    from the thing it stands in for, and the run keeps passing while testing a
    layout the board does not have.

    The one field that is deliberately *not* held together is the address, and
    it is pinned separately below so the divergence is visible rather than
    looking like drift.
    """

    def test_the_device_set_restates_the_board_geometry(self) -> None:
        from megrez_board_session import MEGREZ_FRAMEBUFFER  # noqa: PLC0415

        device_set = device_set_by_name("megrez-board-geometry")
        self.assertIs(device_set, MEGREZ_BOARD_GEOMETRY)
        contract = device_set.framebuffer
        self.assertIsNotNone(contract)
        # `address` and `size` are deliberately excluded: address is covered by
        # the test below, and size here is the bochs BAR's extent rather than
        # the board's scanout length.
        for field in ("width", "height", "stride", "pixel_format"):
            with self.subTest(field=field):
                self.assertEqual(
                    getattr(contract, field), getattr(MEGREZ_FRAMEBUFFER, field)
                )

    def test_the_scanout_is_the_display_so_the_capture_means_something(self) -> None:
        """Why this is not the board's 0xfd800000, pinned rather than narrated.

        Two independent reasons, and the test records both because they are
        the kind of thing that gets "simplified" back later.

        The address is not simulable at 2 GiB: U-Boot's own stack and code sit
        at `[0xfde96000, 0xffffffff]` and the board's scanout overlaps them, so
        U-Boot relocates the device tree into the scanout buffer and the kernel
        destroys the tree it booted from. Declaring the region reserved does
        not help -- U-Boot swallows the `-EEXIST` an overlapping region
        returns.

        And a scanout that is not the display cannot be screenshotted: pointing
        the node into DRAM (0x90000000, tried) passed every guest check but the
        capture held only U-Boot's two-colour console, which the PPM audit
        rejects. So this set keeps the node on the bochs BAR, where the
        screenshot is the scanout and therefore evidence.
        """

        from megrez_board_session import MEGREZ_FRAMEBUFFER  # noqa: PLC0415

        contract = MEGREZ_BOARD_GEOMETRY.framebuffer
        self.assertNotEqual(contract.address, MEGREZ_FRAMEBUFFER.address)
        # On the bochs BAR, which is where the capture comes from.
        self.assertEqual(contract.address, DRM_FIRMWARE.framebuffer.address)
        self.assertEqual(contract.size, DRM_FIRMWARE.framebuffer.size)
        # The board's geometry, which is the whole point of the set.
        self.assertEqual(contract.width, MEGREZ_FRAMEBUFFER.width)
        self.assertEqual(contract.height, MEGREZ_FRAMEBUFFER.height)
        self.assertEqual(contract.stride, MEGREZ_FRAMEBUFFER.stride)
        # And the layout the driver fits inside it: stride * height must not
        # exceed the mapping, or the last row has nowhere to go.
        self.assertLessEqual(contract.stride * contract.height, contract.size)

    def test_the_scanout_is_declared_reserved_before_booti(self) -> None:
        """U-Boot places the tree and the initrd itself, and cannot know.

        `boot_fdt_add_mem_rsv_regions()` runs immediately before
        `boot_relocate_fdt()`, so a `/reserved-memory` child is the channel
        that reaches that decision. It is not load-bearing at the address
        above -- the tree would not have landed there anyway -- but it is the
        declaration that makes the scanout's extent known, and the board's own
        DTB is the place that most needs it.
        """

        commands = boot_commands(device_set=MEGREZ_BOARD_GEOMETRY)
        names = [command.name for command in commands]
        self.assertIn("framebuffer-reserve-node", names)
        self.assertIn("framebuffer-reserve-reg", names)
        self.assertLess(
            names.index("framebuffer-reserve-reg"), names.index("booti")
        )
        reserve_reg = commands[names.index("framebuffer-reserve-reg")]
        self.assertIn("0x40000000", reserve_reg.text)
        self.assertIn("0x1000000", reserve_reg.text)

    def test_the_display_device_is_built_at_the_same_mode(self) -> None:
        with tempfile.TemporaryDirectory() as capture_root:
            joined = " ".join(
                qemu_argv(
                    uboot=Path("/inputs/u-boot"),
                    boot_disk=Path("/inputs/boot.ext4"),
                    profile=MEGREZ_SV48_SVADE_DRM_FIRMWARE,
                    device_set=MEGREZ_BOARD_GEOMETRY,
                    device_paths=RuntimeDevicePaths(
                        capture_root=Path(capture_root),
                        monitor_socket=Path(capture_root) / "qmp.sock",
                    ),
                )
            )
        self.assertIn("bochs-display,xres=1920,yres=1080", joined)
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

    def test_the_configured_machine_and_device_set_reach_the_runner(self) -> None:
        """The profile is an input, not a constant with a Megrez-shaped comment.

        A gate that hardcoded the generic profile while accepting a `--profile`
        flag would run the generic machine and report a pass, and the pass
        would be read as evidence about the board. Nothing in the transcript
        would say otherwise -- the two runs emit the same six markers. So what
        is asserted here is that the configured objects arrive at the runner,
        which is the only place the machine is decided.
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
                    profile=MEGREZ_SV48_SVADE_DRM_FIRMWARE,
                    device_set=MEGREZ_BASIC,
                ),
                runner=fake_runner,
            )

        self.assertTrue(result.passed, result.reason)
        self.assertIs(captured["profile"], MEGREZ_SV48_SVADE_DRM_FIRMWARE)
        self.assertIs(captured["device_set"], MEGREZ_BASIC)
        # The timeouts travel with the profile rather than with the gate, so a
        # Megrez run cannot inherit the generic machine's by accident.
        self.assertEqual(
            captured["boot_timeout"],
            MEGREZ_SV48_SVADE_DRM_FIRMWARE.validation.boot_timeout,
        )

    def test_the_dtb_audit_reaches_the_runner(self) -> None:
        """A contract approximation is refused without it, before QEMU starts.

        The runner demands a generated-DTB audit for every profile whose
        fidelity is not VIRTUAL_PLATFORM: an approximation has to show its
        device tree really carries the properties it claims. The generic Sv39
        profile is a virtual platform, so this gate ran without one for its
        whole life, and the first Megrez run died with `a generated DTB audit
        is required for this machine contract` -- an error about the launch,
        not about the board.
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
                    profile=MEGREZ_SV48_SVADE_DRM_FIRMWARE,
                    device_set=MEGREZ_BASIC,
                    dtb_audit=Path("/inputs/qemu-dtb-audit.json"),
                ),
                runner=fake_runner,
            )

        self.assertTrue(result.passed, result.reason)
        self.assertEqual(captured["dtb_audit"], Path("/inputs/qemu-dtb-audit.json"))

    def test_a_virtual_platform_profile_still_needs_no_audit(self) -> None:
        """The default must stay `None`, or every existing gate breaks."""

        captured: dict[str, object] = {}

        def fake_runner(**arguments: object) -> object:
            captured.update(arguments)
            Path(arguments["serial_log"]).write_bytes(a_transcript())
            return type("BaseResult", (), {"passed": True})()

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "evidence"
            output.mkdir()
            run_firmware_gate(
                FirmwareGateConfig(
                    uboot=Path("/inputs/u-boot"),
                    boot_disk=Path("/inputs/boot.ext4"),
                    manifest=Path("/inputs/artifacts.json"),
                    output_directory=output,
                ),
                runner=fake_runner,
            )

        self.assertIsNone(captured["dtb_audit"])

    def test_an_unknown_profile_is_a_usage_error_not_a_gate_failure(self) -> None:
        """A typo must not read as "the board failed".

        `_parse_args` is the only caller of the resolvers, and both raise
        `ValueError`, which argparse does not catch. Uncaught it exits 1 with a
        traceback -- the same status a real gate failure returns, so a caller
        checking the exit code would record a failing board rather than a
        mistyped name.
        """

        from drm.firmware_gate import _parse_args  # noqa: PLC0415

        for flag, value in (
            ("--profile", "megrez-sv48-svade-drm-firmwarer"),
            ("--device-set", "megrez-bais"),
        ):
            with self.subTest(flag=flag):
                with self.assertRaises(SystemExit) as caught:
                    _parse_args(
                        [
                            "--uboot", "/inputs/u-boot",
                            "--boot-disk", "/inputs/boot.ext4",
                            "--manifest", "/inputs/artifacts.json",
                            "--output-directory", "/outputs",
                            flag, value,
                        ]
                    )
                self.assertIn("unknown", str(caught.exception))

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
