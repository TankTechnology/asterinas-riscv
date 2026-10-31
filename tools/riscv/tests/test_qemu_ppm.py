#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for bounded PPM screendump validation."""

from __future__ import annotations

import unittest

from tools.riscv.qemu_ppm import audit_ppm


def ppm(width: int, height: int, pixels: list[bytes]) -> bytes:
    return f"P6\n{width} {height}\n255\n".encode() + b"".join(pixels)


class PpmAuditTests(unittest.TestCase):
    def test_black_image_is_not_a_passing_capture(self) -> None:
        audit = audit_ppm(
            ppm(8, 8, [b"\0\0\0"] * 64),
            expected_width=8,
            expected_height=8,
        )

        self.assertFalse(audit.passed)
        self.assertEqual(audit.non_black_pixels, 0)
        self.assertIsNone(audit.bounding_box)
        self.assertEqual(audit.distinct_colors_lower_bound, 1)

    def test_colored_region_reports_its_bounding_box(self) -> None:
        pixels = []
        for index in range(100):
            if index < 64:
                pixels.append((b"\xff\x00\x00", b"\x00\xff\x00", b"\x00\x00\xff")[index % 3])
            else:
                pixels.append(b"\0\0\0")

        audit = audit_ppm(
            ppm(10, 10, pixels),
            expected_width=10,
            expected_height=10,
        )

        self.assertTrue(audit.passed)
        self.assertEqual(audit.non_black_pixels, 64)
        self.assertEqual(audit.distinct_colors_lower_bound, 3)
        self.assertEqual(audit.bounding_box, (0, 0, 9, 6))

    def test_header_and_dimensions_are_strict(self) -> None:
        valid = ppm(2, 2, [b"\0\0\0"] * 4)

        with self.assertRaisesRegex(ValueError, "not strict P6"):
            audit_ppm(b"P3\n2 2\n255\n" + b"0 0 0 " * 4, expected_width=2, expected_height=2)
        with self.assertRaisesRegex(ValueError, "dimensions differ"):
            audit_ppm(valid, expected_width=3, expected_height=2)
        with self.assertRaisesRegex(ValueError, "invalid length"):
            audit_ppm(valid[:-1], expected_width=2, expected_height=2)
        with self.assertRaisesRegex(ValueError, "must be positive"):
            audit_ppm(valid, expected_width=0, expected_height=2)

    def test_nonblack_pixels_below_threshold_do_not_pass(self) -> None:
        pixels = [b"\xff\x00\x00"] * 63 + [b"\0\0\0"]
        audit = audit_ppm(
            ppm(8, 8, pixels),
            expected_width=8,
            expected_height=8,
        )

        self.assertFalse(audit.passed)
        self.assertEqual(audit.non_black_pixels, 63)


if __name__ == "__main__":
    unittest.main()
