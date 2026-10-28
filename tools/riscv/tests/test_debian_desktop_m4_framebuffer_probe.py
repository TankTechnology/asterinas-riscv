# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import struct
import unittest

from tools.riscv.debian.rootfs.desktop_m4_framebuffer_probe import (
    ProbeError,
    _layout,
    _parse_output,
    _pixel_diversity,
    _png,
)


def _metadata(width: int = 1920, height: int = 1080) -> tuple[bytes, bytes]:
    variable = bytearray(160)
    fixed = bytearray(80)
    struct.pack_into("=8I", variable, 0, width, height, width, height, 0, 0, 32, 0)
    for offset, values in (
        (32, (16, 8, 0)),
        (44, (8, 8, 0)),
        (56, (0, 8, 0)),
        (68, (24, 8, 0)),
    ):
        struct.pack_into("=3I", variable, offset, *values)
    stride = width * 4
    struct.pack_into("=I", fixed, 24, stride * height)
    struct.pack_into("=I", fixed, 36, 2)
    struct.pack_into("=I", fixed, 48, stride)
    return bytes(variable), bytes(fixed)


class DesktopM4FramebufferProbeTests(unittest.TestCase):
    def test_output_parser_does_not_require_locale_or_gettext(self) -> None:
        self.assertEqual(_parse_output([]).name, "asterinas-desktop-m4-framebuffer.png")
        self.assertEqual(
            _parse_output(["--output", "/run/custom.png"]),
            __import__("pathlib").Path("/run/custom.png"),
        )
        with self.assertRaisesRegex(ProbeError, "usage"):
            _parse_output(["--unexpected"])

    def test_accepts_the_megrez_firmware_scanout_layout(self) -> None:
        self.assertEqual(_layout(*_metadata()), (1920, 1080, 7680, 8294400))

    def test_rejects_non_32_bit_scanout(self) -> None:
        variable, fixed = _metadata()
        mutated = bytearray(variable)
        struct.pack_into("=I", mutated, 24, 16)
        with self.assertRaisesRegex(ProbeError, "unsupported"):
            _layout(bytes(mutated), fixed)

    def test_png_is_bounded_and_has_png_signature(self) -> None:
        width, height, stride = 16, 8, 64
        raw = bytearray(stride * height)
        for y in range(height):
            for x in range(width):
                offset = y * stride + x * 4
                raw[offset : offset + 4] = bytes((x, y, 127, 0))
        payload = _png(bytes(raw), width, height, stride)
        self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertLessEqual(len(payload), 2 * 1024 * 1024)

    def test_pixel_diversity_rejects_blank_scanout(self) -> None:
        with self.assertRaisesRegex(ProbeError, "single color"):
            _pixel_diversity(bytes(16 * 8 * 4), 16 * 4, 16, 8)

    def test_pixel_diversity_accepts_rendered_scanout(self) -> None:
        raw = bytearray(16 * 8 * 4)
        raw[0:4] = b"\x01\x02\x03\x00"
        raw[(4 * 16 + 4) * 4 : (4 * 16 + 4) * 4 + 4] = b"\x04\x05\x06\x00"
        colors, non_background = _pixel_diversity(bytes(raw), 16 * 4, 16, 8)
        self.assertGreaterEqual(colors, 2)
        self.assertGreater(non_background, 0)


if __name__ == "__main__":
    unittest.main()
