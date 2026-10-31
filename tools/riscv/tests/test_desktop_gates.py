#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the picked Debian desktop application gates."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.riscv.debian.rootfs import desktop_m3_gate
from tools.riscv.debian.rootfs import desktop_m4_gate


def transcript(milestones: tuple[str, ...], *, framebuffer: bool = False) -> bytes:
    lines = ["boot noise", *milestones]
    if framebuffer:
        lines.extend(
            [
                "DEBIAN_DESKTOP_M4_FB_IOCTL width=1280 height=1024 "
                "stride=5120 bpp=32 format=BgrReserved",
                "DEBIAN_DESKTOP_M4_FB_SAMPLE bytes=4096 sha256=" + "a" * 64,
                "DEBIAN_DESKTOP_M4_FB_COLORS distinct=4 non_background=12",
            ]
        )
    return ("\n".join(lines) + "\n").encode()


class DesktopQemuTests(unittest.TestCase):
    def test_graphical_gate_is_four_harts_and_has_input_devices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {}
            for name in ("u-boot", "boot.ext4", "root.ext2"):
                path = root / name
                path.write_bytes(b"fixture")
                files[name] = path

            argv = desktop_m4_gate.desktop_m4_qemu_argv(
                uboot=files["u-boot"],
                boot_disk=files["boot.ext4"],
                root_disk=files["root.ext2"],
                monitor_socket=root / "monitor.sock",
            )

        joined = " ".join(argv)
        self.assertIn("-smp 4", joined)
        self.assertIn("bochs-display,xres=1280,yres=1024", joined)
        self.assertIn("virtio-keyboard-device", joined)
        self.assertIn("virtio-tablet-device", joined)


class DesktopClassificationTests(unittest.TestCase):
    def test_m4_requires_ordered_milestones_and_framebuffer_evidence(self) -> None:
        result = desktop_m4_gate.classify_desktop_m4(
            transcript(desktop_m4_gate.DESKTOP_M4_MILESTONES, framebuffer=True),
            expected_debian_release="13.0",
        )

        self.assertTrue(result.passed, result.reason)

    def test_m4_rejects_missing_milestone_and_framebuffer(self) -> None:
        missing = desktop_m4_gate.classify_desktop_m4(
            transcript(desktop_m4_gate.DESKTOP_M4_MILESTONES[:-1]),
            expected_debian_release="13.0",
        )
        no_framebuffer = desktop_m4_gate.classify_desktop_m4(
            transcript(desktop_m4_gate.DESKTOP_M4_MILESTONES),
            expected_debian_release="13.0",
        )

        self.assertFalse(missing.passed)
        self.assertIn("missing desktop milestone", missing.reason)
        self.assertFalse(no_framebuffer.passed)
        self.assertIn("framebuffer", no_framebuffer.reason)

    def test_m4_rejects_out_of_order_and_fatal_transcripts(self) -> None:
        milestones = desktop_m4_gate.DESKTOP_M4_MILESTONES
        out_of_order = transcript((milestones[1], milestones[0], *milestones[2:]), framebuffer=True)
        fatal = transcript(milestones, framebuffer=True) + b"kernel panic - not syncing\n"

        self.assertFalse(
            desktop_m4_gate.classify_desktop_m4(
                out_of_order, expected_debian_release="13.0"
            ).passed
        )
        self.assertFalse(
            desktop_m4_gate.classify_desktop_m4(
                fatal, expected_debian_release="13.0"
            ).passed
        )

    def test_m3_and_empty_release_validation(self) -> None:
        m3 = desktop_m3_gate.classify_desktop_m3(
            transcript(desktop_m3_gate.DESKTOP_M3_MILESTONES),
            expected_debian_release="13.0",
        )
        empty = desktop_m3_gate.classify_desktop_m3(
            transcript(desktop_m3_gate.DESKTOP_M3_MILESTONES),
            expected_debian_release="",
        )

        self.assertTrue(m3.passed, m3.reason)
        self.assertFalse(empty.passed)
        self.assertIn("release", empty.reason)


if __name__ == "__main__":
    unittest.main()
