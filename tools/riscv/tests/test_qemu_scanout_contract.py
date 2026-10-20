# SPDX-License-Identifier: MPL-2.0

import hashlib
import unittest

from tools.riscv.qemu_scanout_contract import (
    ScanoutContractError,
    ScanoutMode,
    parse_ppm,
    validate_fault,
    validate_mode,
    verify_region,
)


EXPECTED = ScanoutMode(
    bar_address=0x40000000,
    bar_size=0x01000000,
    width=1280,
    height=1024,
    stride=5120,
    pixel_format="x8r8g8b8",
)


class QemuScanoutContractTests(unittest.TestCase):
    def test_mode_contract_accepts_bochs_layout(self):
        validate_mode(EXPECTED, expected=EXPECTED)

    def test_mode_contract_rejects_missing_bar(self):
        mode = ScanoutMode(0, 0, 1280, 1024, 5120, "x8r8g8b8")
        self.assertRegex(validate_fault("missing-bar", mode), "BAR")

    def test_mode_contract_rejects_bad_stride(self):
        mode = ScanoutMode(0x40000000, 0x1000000, 1280, 1024, 4096, "x8r8g8b8")
        self.assertRegex(validate_fault("bad-stride", mode), "stride")

    def test_mode_contract_rejects_truncated_mapping(self):
        mode = ScanoutMode(0x40000000, 0x1000000, 1280, 1024, 5120,
                           "x8r8g8b8", mapped_size=5120 * 1023)
        self.assertRegex(validate_fault("truncated-mapping", mode), "mapping")

    def test_mode_contract_rejects_frame_larger_than_bar(self):
        mode = ScanoutMode(0x40000000, 4 * 1024 * 1024 + 4096, 1280, 1024, 5120, "x8r8g8b8")
        self.assertRegex(validate_fault("short-bar", mode), "truncated")

    def test_qmp_ppm_hash_and_rgb_regions(self):
        width, height = 8, 4
        red, green, blue = b"\xff\x00\x00", b"\x00\xff\x00", b"\x00\x00\xff"
        pixels = bytearray(b"\x00\x00\x00" * (width * height))
        for y in range(2):
            pixels[(y * width) * 3 : (y * width + 2) * 3] = red * 2
            pixels[(y * width + 2) * 3 : (y * width + 4) * 3] = green * 2
            pixels[(y * width + 4) * 3 : (y * width + 6) * 3] = blue * 2
        image = parse_ppm(f"P6\n{width} {height}\n255\n".encode() + pixels,
                          expected_width=width, expected_height=height)
        self.assertEqual(image.pixel_sha256, hashlib.sha256(pixels).hexdigest())
        verify_region(image, x=0, y=0, width=2, height=2, color=red)
        verify_region(image, x=2, y=0, width=2, height=2, color=green)
        verify_region(image, x=4, y=0, width=2, height=2, color=blue)
        self.assertEqual(len(image.colors), 4)

    def test_ppm_readback_mismatch_is_rejected(self):
        payload = b"P6\n1 1\n255\n" + b"\xff\x00\x00"
        image = parse_ppm(payload, expected_width=1, expected_height=1)
        with self.assertRaisesRegex(ScanoutContractError, "differs"):
            verify_region(image, x=0, y=0, width=1, height=1, color=b"\x00\xff\x00")

    def test_ppm_truncation_is_rejected(self):
        with self.assertRaisesRegex(ScanoutContractError, "truncated"):
            parse_ppm(b"P6\n2 1\n255\n\x00\x00\x00", expected_width=2, expected_height=1)


if __name__ == "__main__":
    unittest.main()
