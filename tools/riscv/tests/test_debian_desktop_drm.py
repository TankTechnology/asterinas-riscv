#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.riscv.debian.rootfs.desktop_drm_gate import (
    DESKTOP_DRM_EXPECTED_HEIGHT,
    DESKTOP_DRM_EXPECTED_WIDTH,
    DESKTOP_DRM_MILESTONES,
    classify_desktop_drm,
    desktop_drm_qemu_argv,
)
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
        # The kernel's virtio-gpu DRM driver synthesizes a single 1280x800
        # mode (kernel/src/device/drm/kms.rs), so the gate must accept that
        # screendump geometry instead of the bochs-era 1280x1024 default.
        self.assertEqual((DESKTOP_DRM_EXPECTED_WIDTH, DESKTOP_DRM_EXPECTED_HEIGHT), (1280, 800))

    def test_classifier_requires_all_ordered_drm_markers(self) -> None:
        transcript = ("boot\n" + "\n".join(DESKTOP_DRM_MILESTONES) + "\n").encode()
        self.assertTrue(classify_desktop_drm(transcript, expected_debian_release="13.6").passed)
        failed = classify_desktop_drm(
            transcript + b"DEBIAN_DESKTOP_DRM_FAIL reason=xorg\n",
            expected_debian_release="13.6",
        )
        self.assertFalse(failed.passed)


if __name__ == "__main__":
    unittest.main()
