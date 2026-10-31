#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the compact direct-boot Debian desktop gate."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from tools.riscv.debian import desktop_gate


class QemuArgumentTests(unittest.TestCase):
    def test_direct_boot_uses_a_private_monitor_and_a_ram_framebuffer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            argv = desktop_gate.build_qemu_argv(
                qemu="qemu-system-riscv64",
                kernel=root / "kernel.Image",
                rootfs=root / "root.ext2",
                stage1=root / "stage1.cpio",
                dtb=root / "fb.dtb",
                log_dir=root,
                memory="2G",
                smp=4,
            )

        joined = " ".join(argv)
        self.assertIn("-initrd", argv)
        self.assertIn("-dtb", argv)
        self.assertIn("-monitor", argv)
        self.assertIn(
            f"unix:{root / 'qemu-monitor.sock'},server,nowait",
            argv,
        )
        self.assertNotIn("bochs-display", joined)
        self.assertNotIn("-vga", argv)
        append = argv[argv.index("-append") + 1]
        self.assertIn("init=/init", append)
        self.assertIn("--root-init=systemd", append)
        self.assertIn("ASTERINAS_BROWSER_WEB_BASIC_ONLY=1", append)
        self.assertIn("ASTERINAS_WEB_NETWORK_MODE=direct", append)
        self.assertIn("snapshot=on", argv[argv.index("-drive") + 1])

    def test_markers_are_ordered_and_unique(self) -> None:
        markers = desktop_gate.DESKTOP_MARKERS

        self.assertEqual(len(markers), len(set(markers)))
        self.assertTrue(markers[0].endswith("display-provider-ready"))
        self.assertTrue(markers[-1].endswith("panel-start"))

    def test_guest_evidence_patterns_are_specific(self) -> None:
        self.assertIsNotNone(
            desktop_gate.DESKTOP_FRAME.search(
                "BROWSER_WEB_DESKTOP_FRAME_STAGE=desktop"
            )
        )
        self.assertIsNotNone(
            desktop_gate.FIREFOX_WINDOW.search(
                "BROWSER_WEB_DESKTOP_FIREFOX_WINDOW=ready "
                "id=0x123 title=about:blank_Mozilla_Firefox\r\n"
            )
        )
        self.assertIsNone(
            desktop_gate.FIREFOX_WINDOW.search(
                "BROWSER_WEB_DESKTOP_FIREFOX_WINDOW=ready id=0x123 title="
            )
        )
        self.assertIsNotNone(
            desktop_gate.FRAME_READY.search(
                "BROWSER_WEB_DESKTOP_FRAME_READY source=host-pmemsave"
            )
        )


class FramebufferTests(unittest.TestCase):
    def _raw(self, directory: Path, pixels: list[bytes], *, stride: int) -> Path:
        path = directory / "framebuffer.raw"
        path.write_bytes(b"".join(pixels))
        return path

    def test_visible_pixels_are_counted_and_encoded_as_rgb(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = self._raw(
                root,
                [
                    b"\x00\x00\xff\x00",  # red
                    b"\x00\xff\x00\x00",  # green
                    b"\xff\x00\x00\x00",  # blue
                    b"\xff\xff\xff\x00",  # white
                ],
                stride=8,
            )
            result = desktop_gate._read_framebuffer(
                raw,
                width=2,
                height=2,
                stride=8,
                min_nonzero=0.5,
                min_colors=3,
            )
            ppm = root / "framebuffer.ppm"
            desktop_gate._write_ppm(raw, ppm, width=2, height=2, stride=8)

            self.assertEqual(result["nonzero_pixels"], 4)
            self.assertEqual(result["distinct_colors"], 4)
            self.assertEqual(
                ppm.read_bytes(),
                b"P6\n2 2\n255\n"
                b"\xff\x00\x00\x00\xff\x00"
                b"\x00\x00\xff\xff\xff\xff",
            )

    def test_padding_is_not_counted_as_visible_pixels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = self._raw(
                root,
                [
                    b"\x00" * 8 + b"\xff\xff\xff\xff",
                    b"\x00" * 8 + b"\xff\xff\xff\xff",
                ],
                stride=12,
            )
            ppm = root / "framebuffer.ppm"
            desktop_gate._write_ppm(raw, ppm, width=2, height=2, stride=12)

            with self.assertRaisesRegex(desktop_gate.DesktopGateError, "blank"):
                desktop_gate._read_framebuffer(
                    raw,
                    width=2,
                    height=2,
                    stride=12,
                    min_nonzero=0.01,
                    min_colors=1,
                )
            self.assertEqual(
                ppm.read_bytes(),
                b"P6\n2 2\n255\n" + b"\x00" * 12,
            )

    def test_short_dump_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = self._raw(root, [b"\x00" * 8], stride=8)
            with self.assertRaisesRegex(desktop_gate.DesktopGateError, "expected 16"):
                desktop_gate._read_framebuffer(
                    raw,
                    width=2,
                    height=2,
                    stride=8,
                    min_nonzero=0.01,
                    min_colors=1,
                )


class LogDirectoryTests(unittest.TestCase):
    def test_private_directory_is_accepted_and_public_one_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "private"
            root.mkdir(mode=0o700)
            desktop_gate._prepare_log_dir(root)

            root.chmod(0o755)
            with self.assertRaisesRegex(desktop_gate.DesktopGateError, "0700"):
                desktop_gate._prepare_log_dir(root)


class ArgumentTests(unittest.TestCase):
    def test_defaults_are_bounded_to_the_contract(self) -> None:
        args = desktop_gate.parse_args(
            [
                "--kernel",
                "k",
                "--rootfs",
                "r",
                "--stage1",
                "s",
                "--dtb",
                "d",
            ]
        )

        self.assertEqual(args.memory, "2G")
        self.assertEqual(args.smp, 4)
        self.assertEqual(args.timeout, 900.0)
        self.assertEqual(args.firefox_timeout, 600.0)
        self.assertEqual(args.fb_address, 0xF0000000)
        self.assertEqual((args.fb_width, args.fb_height, args.fb_stride), (1280, 1024, 5120))

    def test_required_inputs_and_four_harts_are_enforced(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            desktop_gate.parse_args(["--kernel", "k"])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            desktop_gate.parse_args(
                [
                    "--kernel",
                    "k",
                    "--rootfs",
                    "r",
                    "--stage1",
                    "s",
                    "--dtb",
                    "d",
                    "--smp",
                    "2",
                ]
            )


if __name__ == "__main__":
    unittest.main()
