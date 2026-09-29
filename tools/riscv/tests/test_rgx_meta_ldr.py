#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Check META LDR bounds before firmware bytes can reach GPU-owned memory."""

import struct
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from drm.rgx_meta_ldr import scan_ldr  # noqa: E402
from tools.riscv.tests.test_rgx_firmware_layout import firmware_image  # noqa: E402


def ldr_image(
    *, load_address=0x40000000, load_size=4,
    zero_address=0x38880000, final_next=0xFFFFFFFF
):
    image = bytearray(firmware_image())
    struct.pack_into("<IIIHH", image, 0, 0x01AA5500, 0x10, 0x100, 0, 0)
    struct.pack_into("<HHI2I", image, 0x100, 5, 16, 0x120, 0x200, 0)
    struct.pack_into("<HHI2I", image, 0x120, 0, 16, 0x140, load_address, 0x220)
    struct.pack_into("<HHI2I", image, 0x140, 4, 16, 0x160, zero_address, 8)
    struct.pack_into("<HHI", image, 0x160, 3, 8, final_next)
    struct.pack_into("<HH3IH", image, 0x200, 0, 18, 2, 0x04830030, 4, 0)
    struct.pack_into("<HH", image, 0x220, 0, load_size + 6)
    image[0x224 : 0x224 + load_size] = bytes(range(load_size))
    return bytes(image)


class RgxMetaLdrTests(unittest.TestCase):
    def test_scans_supported_commands_and_segment_writes(self):
        result = scan_ldr(ldr_image())
        self.assertEqual(result["command_counts"], {
            "config": 1, "loadmem": 1, "zeromem": 1, "start_threads": 1
        })
        self.assertEqual(result["write_bytes"], {"code": 4, "data": 8})
        self.assertEqual(result["boot_config_writes"], 1)
        self.assertEqual(result["blocks"], 4)

    def test_rejects_cycle_and_out_of_bounds_next_pointer(self):
        with self.assertRaisesRegex(ValueError, "cycle"):
            scan_ldr(ldr_image(final_next=0x100))
        with self.assertRaisesRegex(ValueError, "L1"):
            scan_ldr(ldr_image(final_next=8190))

    def test_rejects_section_overrun_even_if_start_address_is_valid(self):
        with self.assertRaisesRegex(ValueError, "L1=0x120.*allocation.*address=0x4000cb5e"):
            scan_ldr(ldr_image(load_address=0x40000000 + 52062))

    def test_skips_only_bounded_coremem_data_zero_commands(self):
        result = scan_ldr(ldr_image(zero_address=0x82000000))
        self.assertEqual(result["skipped_coremem_data_zero_bytes"], 8)
        self.assertEqual(result["write_bytes"], {"code": 4})
        with self.assertRaisesRegex(ValueError, "outside"):
            scan_ldr(ldr_image(zero_address=0x82020000))

    def test_rejects_truncated_load_payload_and_unsupported_command(self):
        bad_payload = bytearray(ldr_image())
        struct.pack_into("<I", bad_payload, 0x12C, 0xFFE)
        with self.assertRaisesRegex(ValueError, "L2"):
            scan_ldr(bytes(bad_payload))
        unsupported = bytearray(ldr_image())
        struct.pack_into("<H", unsupported, 0x100, 1)
        with self.assertRaisesRegex(ValueError, "unsupported"):
            scan_ldr(bytes(unsupported))


if __name__ == "__main__":
    unittest.main()
