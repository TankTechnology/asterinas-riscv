#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import inspect
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.riscv.debian.rootfs.desktop_drm_gate import (
    DESKTOP_DRM_BOOTARGS,
    DESKTOP_DRM_EXPECTED_HEIGHT,
    DESKTOP_DRM_EXPECTED_WIDTH,
    DESKTOP_DRM_GL_PREFIX,
    DESKTOP_DRM_KERNEL_MAX_BYTES,
    DESKTOP_DRM_KERNEL_MIN_BYTES,
    DESKTOP_DRM_MILESTONES,
    DESKTOP_DRM_PIXEL_MILESTONE,
    DESKTOP_DRM_PIXEL_PREFIX,
    DESKTOP_DRM_VIRGL_MILESTONE,
    DESKTOP_DRM_VIRGL_MILESTONES,
    GUEST_DEADLINE_MINIMUM_SECONDS,
    GUEST_DEADLINE_MARGIN_SECONDS,
    DesktopDRMOperations,
    capture_rendered_ppm,
    classify_desktop_drm,
    classify_desktop_drm_virgl,
    desktop_drm_milestones,
    desktop_drm_qemu_argv,
    observed_desktop_drm_pixels,
    observed_desktop_drm_renderer,
    orchestrate_desktop_drm_gate,
    reject_a_kernel_that_cannot_boot,
)
from tools.riscv.debian.rootfs.rootfs_gate import GateConfig, GateFailure
from tools.riscv.debian.rootfs.profiles import get_profile


class DebianDesktopDRMTests(unittest.TestCase):
    def test_profile_uses_drm_runtime_and_no_fbdev_driver(self) -> None:
        profile = get_profile("desktop-drm")
        self.assertEqual((profile.schema_version, profile.root_label), (8, "ASTER_DEBIANDRM"))
        self.assertIn("libgl1-mesa-dri", profile.requested_packages)
        self.assertIn("libdrm2", profile.identity_packages)
        self.assertNotIn("xserver-xorg-video-fbdev", profile.requested_packages)

    def test_qemu_contract_selects_virtio_gpu(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            paths = {}
            for name in ("u-boot", "boot.ext4", "root.ext2"):
                path = directory / name
                path.write_bytes(name.encode())
                paths[name] = path
            argv = desktop_drm_qemu_argv(
                uboot=paths["u-boot"],
                boot_disk=paths["boot.ext4"],
                root_disk=paths["root.ext2"],
                monitor_socket=directory / "monitor.sock",
            )
        self.assertIn("virtio-gpu-device", argv)
        self.assertNotIn("bochs-display", argv)
        self.assertIn("virtio-keyboard-device", argv)
        self.assertIn("virtio-tablet-device", argv)

    def test_expected_geometry_matches_kernel_mode(self) -> None:
        # The virtio-gpu DRM driver synthesizes a single 1280x800 mode, so the
        # gate must accept that screendump geometry instead of the bochs-era
        # 1280x1024 default.
        self.assertEqual((DESKTOP_DRM_EXPECTED_WIDTH, DESKTOP_DRM_EXPECTED_HEIGHT), (1280, 800))

    def test_kernel_reports_the_driver_name_mesa_matches_on(self) -> None:
        # Mesa chooses the DRI driver for a device that is not on the PCI bus
        # from the name the kernel returns for DRM_IOCTL_VERSION, and from
        # nothing else; it compares that with strcmp against its own
        # `virtio_gpu` descriptor. A miss does not raise: the static pipe
        # loader falls through to the kmsro descriptor, that fails to create a
        # screen, and Mesa quietly renders with llvmpipe instead. Because the
        # only symptom is a renderer line tens of minutes into a QEMU run, the
        # spelling is pinned here where a rename is caught in milliseconds.
        source = (
            Path(__file__).resolve().parents[3] / "kernel/src/device/dri.rs"
        ).read_text(encoding="utf-8")
        # The visibility prefix is optional in the pattern because the
        # declaration carries one. Anchoring on a bare `^const` is what made
        # this test fail for as long as the constant has been `pub(super)`:
        # it matched nothing, so it reported "DRIVER_NAME not found" rather
        # than a spelling change, and a test that can only fail with the wrong
        # message is a test nobody reads.
        match = re.search(
            r'^\s*(?:pub(?:\([^)]*\))?\s+)?const DRIVER_NAME: &str = "([^"]+)";',
            source,
            re.MULTILINE,
        )
        self.assertIsNotNone(match, "DRIVER_NAME not found in kernel/src/device/dri.rs")
        self.assertEqual(match.group(1), "virtio_gpu")

    def test_a_kernel_that_would_hang_is_refused_before_launch(self) -> None:
        """The two artifacts that stall silently, at the sizes they arrive at.

        `cargo osdk test` rewrites the gate's kernel in place with a dev-profile
        Sv48 build, and a build made without `riscv_sv39_mode` is an Sv48
        release build. Both stop at "Starting kernel ..." and never print
        again, so the gate has to refuse them *before* it launches anything:
        once running, a stall is indistinguishable from work.
        """
        with tempfile.TemporaryDirectory() as directory:
            kernel = Path(directory) / "kernel"
            for size in (
                14_316_712,  # a dev-profile build, as `cargo osdk test` leaves it
                4_056_528,  # a release build made without `riscv_sv39_mode`
            ):
                with kernel.open("wb") as handle:
                    handle.truncate(size)
                with self.assertRaises(GateFailure) as raised:
                    reject_a_kernel_that_cannot_boot(kernel)
                self.assertIn(str(size), str(raised.exception))
                self.assertIn("riscv_sv39_mode", str(raised.exception))

    def test_a_kernel_of_a_bootable_size_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = Path(directory) / "kernel"
            with kernel.open("wb") as handle:
                handle.truncate(6_138_136)  # a real release build with Sv39
            reject_a_kernel_that_cannot_boot(kernel)

    def test_classifier_requires_all_ordered_drm_markers(self) -> None:
        transcript = ("boot\n" + "\n".join(DESKTOP_DRM_MILESTONES) + "\n").encode()
        self.assertTrue(classify_desktop_drm(transcript, expected_debian_release="13.6").passed)
        failed = classify_desktop_drm(
            transcript + b"DEBIAN_DESKTOP_DRM_FAIL reason=xorg\n",
            expected_debian_release="13.6",
        )
        self.assertFalse(failed.passed)

    def test_guest_deadline_expires_before_the_gate_window(self) -> None:
        # The guest has to report its own diagnosis before the gate's window
        # closes, otherwise a stuck desktop is only ever a bare gate timeout.
        # The value is an absolute guest uptime, so it shares the gate's origin.
        deadline = DesktopDRMOperations._guest_deadline_seconds(420)
        self.assertEqual(deadline, 420 - GUEST_DEADLINE_MARGIN_SECONDS)
        self.assertLess(deadline, 420)

    def test_guest_deadline_keeps_a_floor_for_short_windows(self) -> None:
        self.assertEqual(
            DesktopDRMOperations._guest_deadline_seconds(30),
            GUEST_DEADLINE_MINIMUM_SECONDS,
        )

    def test_bootargs_carry_the_guest_deadline_before_the_separator(self) -> None:
        bootargs = DesktopDRMOperations._bootargs(600)
        self.assertIn("asterinas.desktop_drm_deadline=600", bootargs)
        # Only the kernel's own arguments belong before `--`; the stage-1
        # selector must stay after it.
        self.assertLess(
            bootargs.index("asterinas.desktop_drm_deadline=600"),
            bootargs.index(" -- "),
        )
        self.assertTrue(bootargs.endswith("--root-init=systemd"))

    def test_bootargs_are_unchanged_without_a_deadline(self) -> None:
        self.assertEqual(DesktopDRMOperations._bootargs(), DESKTOP_DRM_BOOTARGS)

    def test_bootargs_environment_override_still_receives_the_deadline(self) -> None:
        override = "console=ttyS0 loglevel=7 -- --root-init=systemd"
        with mock.patch.dict("os.environ", {"ASTERINAS_DESKTOP_DRM_BOOTARGS": override}):
            bootargs = DesktopDRMOperations._bootargs(600)
        self.assertIn("loglevel=7", bootargs)
        self.assertIn("asterinas.desktop_drm_deadline=600", bootargs)
        self.assertTrue(bootargs.endswith("--root-init=systemd"))


class DesktopDRMScreenshotContractTests(unittest.TestCase):
    def test_the_capture_accepts_what_this_gate_passes_it(self) -> None:
        """The screenshot call and the screenshot function have to agree.

        This gate passed `min_distinct_colors` and
        `min_non_background_ratio` for as long as those constants have existed,
        and `capture_rendered_ppm` never took them, so every run that captured
        a screenshot died with `TypeError: ... got an unexpected keyword
        argument`. The orchestrator reported the phase name, `protocol`, and
        threw the exception away -- so the failure named nothing.

        It went unnoticed because the virgl run, which is the one anybody runs,
        skips the capture entirely (`_capture_screenshot` is false for a GL
        device). The call only ever ran on the non-3D path. The signature is
        the contract between the two, so this pins the names the gate passes.
        """

        parameters = inspect.signature(capture_rendered_ppm).parameters
        for name in ("min_distinct_colors", "min_non_background_ratio"):
            with self.subTest(parameter=name):
                self.assertIn(name, parameters)

    def test_the_gate_still_passes_its_own_thresholds(self) -> None:
        # The parameters existing is only half of it: the call has to name the
        # gate's constants, or the stricter "the desktop has painted" check
        # would silently become the looser "the screen is not blank" one.
        source = inspect.getsource(DesktopDRMOperations.run_protocol)
        self.assertIn("min_distinct_colors=DESKTOP_DRM_MIN_DISTINCT_COLORS", source)
        self.assertIn(
            "min_non_background_ratio=DESKTOP_DRM_MIN_NON_BACKGROUND_RATIO", source
        )


class DesktopDRMXorgMilestoneTests(unittest.TestCase):
    """The Xorg milestone has to be a check, not a constant on both sides.

    It was `device=virtio-gpu` in this gate's expected list and the same literal
    in the guest's `emit`, so the two agreed by construction and the milestone
    passed on every machine -- including one with no virtio-gpu at all, where it
    was false. Nothing could have caught that from a transcript, because the
    transcript was the thing being asserted.

    These pin both halves: the expectation varies with the display device, and
    the guest derives its half instead of restating it. Either one alone is
    useless -- a device-aware expectation matched against a constant emission
    fails every non-virtio run, and a constant expectation matched against an
    observation never fails at all.
    """

    EVIDENCE = (
        Path(__file__).resolve().parents[1] / "debian/rootfs/desktop_drm_evidence.sh"
    )

    def test_the_expected_driver_follows_the_display_device(self) -> None:
        self.assertIn(
            "DEBIAN_DESKTOP_DRM_XORG driver=modesetting device=virtio_gpu "
            "drm=active display=:0",
            desktop_drm_milestones("virtio-gpu-device"),
        )
        # A bochs display has no GPU, so what presents is the firmware
        # backend's `simpledrm`.
        self.assertIn(
            "DEBIAN_DESKTOP_DRM_XORG driver=modesetting device=simpledrm "
            "drm=active display=:0",
            desktop_drm_milestones("bochs-display"),
        )

    def test_an_unknown_display_device_is_refused(self) -> None:
        # Better to fail here than to expect a driver nothing will report.
        with self.assertRaises(ValueError):
            desktop_drm_milestones("some-other-display")

    def test_the_guest_observes_the_driver_rather_than_restating_it(self) -> None:
        script = self.EVIDENCE.read_text()
        emits = [
            line
            for line in script.splitlines()
            if "DEBIAN_DESKTOP_DRM_XORG" in line and line.lstrip().startswith("emit")
        ]
        self.assertEqual(len(emits), 1, "expected exactly one Xorg emit line")
        # A literal `device=` is the defect: it prints the same string on a
        # machine that does not have that device.
        self.assertNotIn("device=virtio-gpu", emits[0])
        self.assertIn("$(drm_driver_name)", emits[0])

    def test_the_guest_reports_a_missing_device_as_itself(self) -> None:
        # `absent` and `unnamed` are not driver names, and are not meant to be:
        # substituting a plausible one would turn "there is no card node" into
        # "the wrong driver is bound", which is a different investigation.
        script = self.EVIDENCE.read_text()
        self.assertIn("printf 'absent'", script)
        self.assertIn("printf 'unnamed'", script)


class DesktopDRMRendererTests(unittest.TestCase):
    """The renderer line is the evidence; these pin how it is waited on and graded."""

    def setUp(self) -> None:
        # Building the operations opens every input path, so the config has to
        # point at files that exist rather than at plausible-looking names.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.inputs = Path(temporary.name) / "inputs"
        self.inputs.mkdir()
        for name in (
            "kernel",
            "u-boot",
            "dtb",
            "initramfs.cpio",
            "root.ext2",
            "manifest.json",
            "packages.lock",
            "checksums",
        ):
            (self.inputs / name).write_bytes(b"input")
        # The kernel has to be sized like one that can boot: the gate refuses
        # an image it would hang on before it launches anything, and a
        # five-byte placeholder is exactly such an image. Sparse, so this costs
        # no disk and no measurable time.
        with (self.inputs / "kernel").open("r+b") as handle:
            handle.truncate(DESKTOP_DRM_KERNEL_MIN_BYTES + 1)
        self.output = Path(temporary.name) / "evidence"
        self.output.mkdir()

    def _config(self, graphics_device: str) -> GateConfig:
        return GateConfig(
            kernel=self.inputs / "kernel",
            u_boot=self.inputs / "u-boot",
            dtb=self.inputs / "dtb",
            stage1_initramfs=self.inputs / "initramfs.cpio",
            root_image=self.inputs / "root.ext2",
            manifest=self.inputs / "manifest.json",
            packages_lock=self.inputs / "packages.lock",
            package_checksums=self.inputs / "checksums",
            output_directory=self.output,
            graphics_device=graphics_device,
        )

    #: A verdict line in the shape the guest's probe emits on success.
    PASSING_PIXELS = "ok=yes left=ffffff right=000000 expect=ffffff/000000"

    @classmethod
    def _transcript(
        cls, renderer: str | None, pixels: str | None = PASSING_PIXELS
    ) -> bytes:
        """A transcript in the guest's own emission order.

        The pixel verdict comes *before* the renderer line, as it does on the
        guest, because `classify_desktop` rejects milestones that are out of
        order -- so a transcript assembled the other way round would test an
        ordering that never occurs.

        `pixels=None` with a renderer given is the case of a guest that
        reported a renderer and no pixel verdict at all, which is its own
        failure and not the same as a verdict of `ok=no`.
        """
        lines = ["boot", *DESKTOP_DRM_MILESTONES]
        if renderer is not None and pixels is not None:
            lines.append(f"{DESKTOP_DRM_PIXEL_PREFIX}{pixels}")
        if renderer is not None:
            lines.append(f"{DESKTOP_DRM_GL_PREFIX}{renderer}")
        return ("\n".join(lines) + "\n").encode()

    def test_a_3d_run_waits_on_the_prefix_not_the_virgl_literal(self) -> None:
        # Waiting for the literal meant an llvmpipe run was never recognised at
        # all: the gate sat out its entire window and reported a bare protocol
        # timeout, destroying the one datum the run existed to produce.
        operations = DesktopDRMOperations(self._config("virtio-gpu-gl-device"))
        self.assertEqual(operations.TERMINAL_MARKER, DESKTOP_DRM_GL_PREFIX.encode())
        self.assertTrue(
            operations.TERMINAL_MARKER.startswith(DESKTOP_DRM_GL_PREFIX.encode())
        )
        # It must still match the good answer, or the gate would never stop on it.
        self.assertTrue(DESKTOP_DRM_VIRGL_MILESTONE.encode().startswith(operations.TERMINAL_MARKER))

    def test_a_2d_run_still_waits_on_its_last_milestone(self) -> None:
        # A plain virtio-gpu run emits no renderer line, so waiting on the
        # prefix there would hang until the timeout on every single run.
        operations = DesktopDRMOperations(self._config("virtio-gpu-device"))
        self.assertEqual(operations.TERMINAL_MARKER, DESKTOP_DRM_MILESTONES[-1].encode())

    def test_the_device_decides_whether_virgl_is_required(self) -> None:
        self.assertTrue(
            DesktopDRMOperations(self._config("virtio-gpu-gl-device")).REQUIRES_VIRGL
        )
        self.assertFalse(
            DesktopDRMOperations(self._config("virtio-gpu-device")).REQUIRES_VIRGL
        )

    def test_a_3d_run_that_fell_back_to_llvmpipe_fails_and_names_it(self) -> None:
        result = classify_desktop_drm_virgl(
            self._transcript("llvmpipe"), expected_debian_release="13.6"
        )
        self.assertFalse(result.passed)
        self.assertIn("llvmpipe", result.reason)

    def test_a_3d_run_on_virgl_passes(self) -> None:
        self.assertTrue(
            classify_desktop_drm_virgl(
                self._transcript("virgl"), expected_debian_release="13.6"
            ).passed
        )

    def test_a_3d_run_with_no_renderer_line_is_not_reported_as_a_renderer(self) -> None:
        # "never got there" and "got there on the wrong driver" are different
        # failures; only the second one has a renderer to name. Both the pixel
        # verdict and the renderer line are absent here, so the classifier
        # reports whichever it looks for first -- the pixel verdict, because
        # that is the order the guest emits them in -- and what matters is that
        # it reads as a missing milestone rather than as a named renderer.
        result = classify_desktop_drm_virgl(
            self._transcript(None), expected_debian_release="13.6"
        )
        self.assertFalse(result.passed)
        self.assertNotIn("llvmpipe", result.reason)
        self.assertNotIn("GL renderer was", result.reason)
        self.assertIn("missing desktop milestone:", result.reason)

    def test_the_2d_classifier_ignores_the_renderer_entirely(self) -> None:
        # Same transcript, no 3D device: nothing about the renderer may leak
        # into a 2D verdict, or every plain run would start failing.
        self.assertTrue(
            classify_desktop_drm(
                self._transcript("llvmpipe"), expected_debian_release="13.6"
            ).passed
        )

    def test_the_observed_renderer_is_read_back_from_the_line(self) -> None:
        self.assertEqual(observed_desktop_drm_renderer(self._transcript("zink")), "zink")
        self.assertIsNone(observed_desktop_drm_renderer(self._transcript(None)))

    def test_a_renderer_that_drew_nothing_is_not_a_pass(self) -> None:
        """`renderer=virgl` says a driver was chosen; it does not say it drew.

        This is the whole reason the pixel verdict exists. A winsys that
        initialises and then produces nothing, or a command stream that never
        lands, reports virgl exactly as happily as a working one -- so the
        renderer line on its own cannot tell those apart.
        """
        result = classify_desktop_drm_virgl(
            self._transcript("virgl", "ok=no left=000000 right=000000"),
            expected_debian_release="13.6",
        )
        self.assertFalse(result.passed)
        self.assertIn("nothing was drawn", result.reason)
        self.assertIn("000000", result.reason)

    def test_a_missing_pixel_verdict_is_not_a_pass(self) -> None:
        """A guest that never answered the pixel question has not passed it."""
        result = classify_desktop_drm_virgl(
            self._transcript("virgl", None), expected_debian_release="13.6"
        )
        self.assertFalse(result.passed)
        self.assertIn(DESKTOP_DRM_PIXEL_MILESTONE, result.reason)

    def test_a_software_renderer_is_reported_before_its_pixels(self) -> None:
        """When both questions are answered badly, name the more basic one.

        A software renderer with wrong pixels and a GPU renderer with wrong
        pixels need different work; leading with the pixels would send the
        reader after the second when the cause is the first.
        """
        result = classify_desktop_drm_virgl(
            self._transcript("llvmpipe", "ok=no left=000000 right=000000"),
            expected_debian_release="13.6",
        )
        self.assertFalse(result.passed)
        self.assertIn("llvmpipe", result.reason)
        self.assertNotIn("nothing was drawn", result.reason)

    def test_the_pixel_verdict_is_read_from_the_line(self) -> None:
        self.assertEqual(
            observed_desktop_drm_pixels(self._transcript("virgl")),
            self.PASSING_PIXELS.encode(),
        )
        self.assertEqual(
            observed_desktop_drm_pixels(
                self._transcript("virgl", "ok=no left=000000 right=000000")
            ),
            b"ok=no left=000000 right=000000",
        )
        self.assertIsNone(observed_desktop_drm_pixels(self._transcript(None)))

    def test_the_pixel_verdict_is_expected_before_the_renderer_line(self) -> None:
        """The guest emits the verdict first, and the gate has to agree.

        `classify_desktop` fails a transcript whose milestones are out of
        order, so this is a contract between the two halves of the evidence
        rather than a coincidence of how the list happens to be written.
        """
        self.assertLess(
            DESKTOP_DRM_VIRGL_MILESTONES.index(DESKTOP_DRM_PIXEL_MILESTONE),
            DESKTOP_DRM_VIRGL_MILESTONES.index(DESKTOP_DRM_VIRGL_MILESTONE),
        )

    def test_the_gate_wires_the_3d_classifier_to_a_3d_device(self) -> None:
        # Requiring virgl is only real if the run that needs it actually gets
        # graded by the classifier that checks it. A correct classifier that
        # nothing calls would look identical from the outside.
        for device, expected in (
            ("virtio-gpu-gl-device", classify_desktop_drm_virgl),
            ("virtio-gpu-device", classify_desktop_drm),
        ):
            with self.subTest(device=device):
                operations = DesktopDRMOperations(self._config(device))
                with mock.patch(
                    "tools.riscv.debian.rootfs.desktop_drm_gate."
                    "orchestrate_systemd_m2_gate",
                    return_value={"passed": True},
                ) as orchestrate:
                    orchestrate_desktop_drm_gate(
                        self._config(device), operations
                    )
                self.assertIs(
                    orchestrate.call_args.kwargs["classifier"], expected
                )


if __name__ == "__main__":
    unittest.main()
