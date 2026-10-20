#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import hashlib
import struct
import unittest

from tools.riscv.debian.rootfs.desktop_drm_x11_screenshot import (
    ScreenshotError,
    parse_xwd,
)


def xwd_fixture(width: int = 2, height: int = 2) -> tuple[bytes, bytes]:
    pixels = bytes(range(width * height * 4))
    values = [
        100 + 12,
        7,
        2,
        32,
        width,
        height,
        0,
        0,
        32,
        32,
        32,
        32,
        width * 4,
        4,
        0x00FF0000,
        0x0000FF00,
        0x000000FF,
        8,
        0,
        0,
        width,
        height,
        0,
        0,
        0,
    ]
    header = struct.pack(">25I", *values)
    return header + b"name\0" + b"\0" * 7 + pixels, pixels


class DesktopDrmX11ScreenshotTests(unittest.TestCase):
    def test_hashes_only_pixel_payload(self) -> None:
        payload, pixels = xwd_fixture()
        image = parse_xwd(payload)
        self.assertEqual((image.width, image.height), (2, 2))
        self.assertEqual(image.bits_per_pixel, 32)
        self.assertEqual(image.bytes_per_line, 8)
        self.assertEqual(image.pixel_sha256, hashlib.sha256(pixels).hexdigest())

    def test_rejects_truncated_pixels(self) -> None:
        payload, _ = xwd_fixture()
        with self.assertRaisesRegex(ScreenshotError, "truncated"):
            parse_xwd(payload[:-1])

    def test_rejects_bad_stride(self) -> None:
        payload, _ = xwd_fixture()
        values = list(struct.unpack_from(">25I", payload))
        values[12] = 1
        bad = struct.pack(">25I", *values) + payload[100:]
        with self.assertRaisesRegex(ScreenshotError, "stride"):
            parse_xwd(bad)


if __name__ == "__main__":
    unittest.main()
