#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Contract tests for the pinned Megrez PowerVR firmware preflight."""

import struct
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from drm.rgx_firmware_layout import inspect_firmware  # noqa: E402


BVNC = (30 << 48) | (3 << 32) | (408 << 16) | 101
META_SECTIONS = (
    (0, 1, 0x40000000, 266240, 52064, 0),
    (1, 2, 0x38880000, 90112, 18432, 0),
    (2, 3, 0x800061a0, 73312, 73312, 0),
    (3, 4, 0x82002000, 9984, 9984, 0),
)


def firmware_image(sections=META_SECTIONS, *, bvnc=BVNC, entry_size=24, count=None):
    image = bytearray(8192)
    start = len(image) - 4096
    struct.pack_into(
        "<4IQ2I2HI",
        image,
        start,
        2,
        40,
        len(sections) if count is None else count,
        entry_size,
        bvnc,
        0,
        0x80020810,
        24,
        2,
        6643903,
    )
    for index, section in enumerate(sections):
        struct.pack_into("<6I", image, start + 40 + index * 24, *section)
    return bytes(image)


class RgxFirmwareLayoutTests(unittest.TestCase):
    def test_reports_meta_segments_and_separate_allocation_sizes(self):
        result = inspect_firmware(firmware_image())
        self.assertEqual(result["bvnc"], "30.3.408.101")
        self.assertEqual(result["ddk"], "24.2.6643903")
        self.assertEqual(result["processor"], "META")
        self.assertEqual(
            result["allocation_bytes"],
            {"code": 52064, "data": 18432, "coremem_code": 73312, "coremem_data": 9984},
        )
        self.assertEqual([entry["id"] for entry in result["sections"]], [0, 1, 2, 3])

    def test_rejects_wrong_board_firmware_before_any_allocation(self):
        with self.assertRaisesRegex(ValueError, "BVNC"):
            inspect_firmware(firmware_image(bvnc=BVNC + 1))

    def test_rejects_malformed_table_bounds(self):
        for image in (
            b"short",
            firmware_image(entry_size=0),
            firmware_image(count=9),
            firmware_image(count=4, entry_size=1024),
        ):
            with self.subTest(length=len(image), header=image[-4096:-4080]):
                with self.assertRaises(ValueError):
                    inspect_firmware(image)

    def test_rejects_invalid_or_overlapping_allocation_ranges(self):
        bad_size = list(META_SECTIONS)
        bad_size[0] = (0, 1, 0x40000000, 4096, 52064, 0)
        duplicate = list(META_SECTIONS)
        duplicate[1] = (0, 2, 0x38880000, 90112, 18432, 0)
        offset = list(META_SECTIONS)
        offset[1] = (1, 2, 0x38880000, 90112, 18432, 4096)
        for sections in (bad_size, duplicate, offset):
            with self.subTest(sections=sections):
                with self.assertRaises(ValueError):
                    inspect_firmware(firmware_image(sections))


if __name__ == "__main__":
    unittest.main()
