#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from tools.riscv.drm.xorg_modesetting_gate import (
    DESKTOP_MARKERS,
    DRI3_CARD_MARKER,
    EXPECTED_HEIGHT,
    EXPECTED_WIDTH,
    validate_xorg_modesetting,
)


def _serial() -> bytes:
    return b"\n".join(
        (
            *DESKTOP_MARKERS,
            DRI3_CARD_MARKER,
            b"DEBIAN_DESKTOP_DRM_GL_XORG accel=virgl",
        )
    )


def _ppm() -> bytes:
    pixels = bytearray()
    for y in range(EXPECTED_HEIGHT):
        for x in range(EXPECTED_WIDTH):
            pixels.extend(
                (0x18, 0x20, 0x2C)
                if x < EXPECTED_WIDTH // 2
                else (0x80 + x % 32, 0x80 + y % 32, 0xA0)
            )
            if x == y % EXPECTED_WIDTH and y < 32:
                pixels[-3:] = bytes((0xE8, 0x58, 0x46))
    return f"P6\n{EXPECTED_WIDTH} {EXPECTED_HEIGHT}\n255\n".encode() + pixels


class DrmXorgModesettingGateTests(unittest.TestCase):
    def test_accepts_ordered_modesetting_and_pixel_hash(self) -> None:
        screenshot = _ppm()
        result = validate_xorg_modesetting(_serial(), screenshot)
        self.assertTrue(result.passed, result.reason)
        self.assertEqual((result.width, result.height), (EXPECTED_WIDTH, EXPECTED_HEIGHT))
        self.assertEqual(result.ppm_sha256, hashlib.sha256(screenshot).hexdigest())
        self.assertEqual(result.renderer, "virgl")
        self.assertGreaterEqual(result.distinct_colors, 3)

    def test_rejects_software_renderer(self) -> None:
        result = validate_xorg_modesetting(
            _serial().replace(b"accel=virgl", b"accel=llvmpipe"), _ppm()
        )
        self.assertFalse(result.passed)
        self.assertIn("virgl", result.reason)

    def test_rejects_fbdev_or_missing_xorg_marker(self) -> None:
        self.assertFalse(
            validate_xorg_modesetting(
                _serial().replace(DESKTOP_MARKERS[4], b"DEBIAN_DESKTOP_DRM_XORG framebuffer=fbdev"),
                _ppm(),
            ).passed
        )
        self.assertFalse(
            validate_xorg_modesetting(_serial().replace(DESKTOP_MARKERS[5], b""), _ppm()).passed
        )

    def test_rejects_blank_or_wrong_geometry_capture(self) -> None:
        blank = b"P6\n1280 800\n255\n" + bytes((0x18, 0x20, 0x2C)) * (EXPECTED_WIDTH * EXPECTED_HEIGHT)
        blank_result = validate_xorg_modesetting(_serial(), blank)
        self.assertFalse(blank_result.passed)
        wrong = b"P6\n1280 1024\n255\n" + bytes(1280 * 1024 * 3)
        wrong_result = validate_xorg_modesetting(_serial(), wrong)
        self.assertFalse(wrong_result.passed)

    def test_output_is_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            serial = root / "serial.log"
            screenshot = root / "screen.ppm"
            output = root / "result.json"
            serial.write_bytes(_serial())
            screenshot.write_bytes(_ppm())
            from tools.riscv.drm.xorg_modesetting_gate import run_validation

            result = run_validation(serial, screenshot, output)
            self.assertTrue(result.passed)
            self.assertIn(result.pixel_sha256, output.read_text())


if __name__ == "__main__":
    unittest.main()
