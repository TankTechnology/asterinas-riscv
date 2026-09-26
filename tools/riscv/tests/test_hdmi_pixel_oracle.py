#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Pixel-level acceptance tests for external Megrez HDMI evidence."""

from __future__ import annotations

import shutil
import struct
import unittest
import zlib


def pattern_png(*, width: int = 64, height: int = 36, wrong: bool = False) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    colors = ((7, 19, 29), (23, 215, 208), (226, 157, 36))
    if wrong:
        colors = ((0, 0, 0),) * 3
    rows = bytearray()
    for _y in range(height):
        rows.append(0)
        for x in range(width):
            rows.extend(colors[min(2, x * 3 // width)])
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable"
)
class HdmiPixelOracleTests(unittest.TestCase):
    def test_accepts_matching_external_pixels(self) -> None:
        from tools.riscv.hdmi_pixel_oracle import compare_hdmi_pixels

        image = pattern_png()
        evidence = compare_hdmi_pixels(image, image, expected_dimensions=(64, 36))
        self.assertEqual(evidence.matched_fraction, 1.0)
        self.assertEqual(evidence.mean_absolute_error, 0.0)

    def test_rejects_a_fresh_but_wrong_or_black_capture(self) -> None:
        from tools.riscv.hdmi_pixel_oracle import PixelMismatch, compare_hdmi_pixels

        with self.assertRaises(PixelMismatch):
            compare_hdmi_pixels(
                pattern_png(), pattern_png(wrong=True), expected_dimensions=(64, 36)
            )

    def test_rejects_wrong_capture_geometry(self) -> None:
        from tools.riscv.hdmi_pixel_oracle import PixelMismatch, compare_hdmi_pixels

        with self.assertRaises(PixelMismatch):
            compare_hdmi_pixels(
                pattern_png(), pattern_png(width=32), expected_dimensions=(64, 36)
            )

    def test_rejects_two_identical_black_images_as_a_false_success(self) -> None:
        from tools.riscv.hdmi_pixel_oracle import PixelMismatch, compare_hdmi_pixels

        blank = pattern_png(wrong=True)
        with self.assertRaises(PixelMismatch):
            compare_hdmi_pixels(blank, blank, expected_dimensions=(64, 36))

    def test_rejects_invalid_capture_without_hanging(self) -> None:
        from tools.riscv.hdmi_pixel_oracle import PixelMismatch, compare_hdmi_pixels

        with self.assertRaises(PixelMismatch):
            compare_hdmi_pixels(
                pattern_png(), b"invalid image", expected_dimensions=(64, 36)
            )


if __name__ == "__main__":
    unittest.main()
