#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

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
    DESKTOP_DRM_MILESTONES,
    DESKTOP_DRM_VIRGL_MILESTONE,
    GUEST_DEADLINE_MINIMUM_SECONDS,
    GUEST_DEADLINE_MARGIN_SECONDS,
    DesktopDRMOperations,
    classify_desktop_drm,
    classify_desktop_drm_virgl,
    desktop_drm_qemu_argv,
    observed_desktop_drm_renderer,
    orchestrate_desktop_drm_gate,
)
from tools.riscv.debian.rootfs.rootfs_gate import GateConfig
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

    @staticmethod
    def _transcript(renderer: str | None) -> bytes:
        lines = ["boot", *DESKTOP_DRM_MILESTONES]
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
        # failures; only the second one has a renderer to name.
        result = classify_desktop_drm_virgl(
            self._transcript(None), expected_debian_release="13.6"
        )
        self.assertFalse(result.passed)
        self.assertNotIn("llvmpipe", result.reason)
        self.assertIn(DESKTOP_DRM_VIRGL_MILESTONE, result.reason)

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
