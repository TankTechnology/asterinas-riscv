#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Check the selected META boot configuration before staging GPU firmware."""

import hashlib
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from drm.rgx_meta_boot import FIRMWARE_VADDRS, prepare_meta_boot  # noqa: E402
from tools.riscv.tests.test_rgx_meta_ldr import ldr_image  # noqa: E402


class RgxMetaBootTests(unittest.TestCase):
    def test_patches_vendor_sequence_at_bootloader_offset(self):
        image = ldr_image()
        original_sha = hashlib.sha256(image).hexdigest()
        summary, buffers = prepare_meta_boot(
            image, expected_firmware_sha256=original_sha, expected_ldr_writes=1
        )
        code = buffers["code"]
        pairs = [
            struct.unpack_from("<II", code, 512 + index * 8)
            for index in range(summary["boot_config_pairs"])
        ]
        self.assertEqual(pairs[0], (0x04830030, 4))
        self.assertEqual(pairs[1], (0x04850010, 0x38880F02))
        self.assertEqual(pairs[2], (0x04850014, 18_432 - 1))
        self.assertEqual(pairs[3], (0x04850018, 0xC000E000))
        self.assertEqual(pairs[4], (0x0485001C, 0x003000E1))
        self.assertEqual(pairs[5], (0x04830030, 4))
        self.assertEqual(pairs[7:11], [
            (0x04830200, 0x80000007),
            (0x04830208, 0x80080007),
            (0x04830210, 0x80000000),
            (0x04830218, 0x80000000),
        ])
        self.assertEqual(pairs[12:16], [
            (0x04830220, 7),
            (0x04830228, 0x80007),
            (0x04830230, 0),
            (0x04830238, 0),
        ])
        self.assertEqual(pairs[-2:], [(0x04830020, 1), (0x040000C0, 0)])
        footer = struct.unpack_from("<6I", code, 512 + len(pairs) * 8)
        self.assertEqual(footer, (0, 0, 0x10014000, 73_312, 0xE1, 0xC0014000))
        self.assertEqual(summary["firmware_vaddrs"], list(FIRMWARE_VADDRS))
        self.assertEqual(summary["meta_threads"], 2)
        self.assertEqual(summary["boot_config_offset"], 512)
        self.assertEqual(summary["boot_config_bytes"], len(pairs) * 8 + 24)
        self.assertEqual(summary["segment_sha256"]["code"], hashlib.sha256(code).hexdigest())
        self.assertNotEqual(summary["segment_sha256"]["code"], original_sha)

    def test_rejects_wrong_firmware_and_unmapped_layout(self):
        image = ldr_image()
        with self.assertRaisesRegex(ValueError, "firmware SHA"):
            prepare_meta_boot(image, expected_firmware_sha256="0" * 64, expected_ldr_writes=1)
        with self.assertRaisesRegex(ValueError, "firmware VA layout"):
            prepare_meta_boot(
                image,
                expected_firmware_sha256=hashlib.sha256(image).hexdigest(),
                expected_ldr_writes=1,
                firmware_vaddrs=(FIRMWARE_VADDRS[0], FIRMWARE_VADDRS[1] + 4096, *FIRMWARE_VADDRS[2:]),
            )
        with self.assertRaisesRegex(ValueError, "LDR config count"):
            prepare_meta_boot(
                image,
                expected_firmware_sha256=hashlib.sha256(image).hexdigest(),
                expected_ldr_writes=17,
            )

    def test_rejects_ldr_writes_over_boot_configuration(self):
        image = ldr_image(load_address=0x40000200)
        with self.assertRaisesRegex(ValueError, "overlaps META boot configuration"):
            prepare_meta_boot(
                image,
                expected_firmware_sha256=hashlib.sha256(image).hexdigest(),
                expected_ldr_writes=1,
            )


if __name__ == "__main__":
    unittest.main()
