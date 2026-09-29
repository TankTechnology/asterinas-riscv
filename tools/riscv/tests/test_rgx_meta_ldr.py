#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Check META LDR bounds before firmware bytes can reach GPU-owned memory."""

import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from drm.rgx_meta_ldr import scan_ldr  # noqa: E402
from tools.riscv.tests.test_rgx_firmware_layout import firmware_image  # noqa: E402


def ldr_image(
    *, load_address=0x40000000, load_size=4,
    zero_address=0x38880000, zero_size=8, final_next=0xFFFFFFFF
):
    image = bytearray(firmware_image())
    struct.pack_into("<IIIHH", image, 0, 0x01AA5500, 0x10, 0x100, 0, 0)
    struct.pack_into("<HHI2I", image, 0x100, 5, 16, 0x120, 0x200, 0)
    struct.pack_into("<HHI2I", image, 0x120, 0, 16, 0x140, load_address, 0x220)
    struct.pack_into("<HHI2I", image, 0x140, 4, 16, 0x160, zero_address, zero_size)
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

    def test_materializes_loads_and_later_zeros_in_command_order(self):
        result = scan_ldr(ldr_image(zero_address=0x40000001, zero_size=2))
        expected = bytearray(52064)
        expected[:4] = bytes(range(4))
        expected[1:3] = b"\0\0"
        self.assertEqual(
            result["segment_sha256"]["code"], hashlib.sha256(expected).hexdigest()
        )
        self.assertEqual(
            result["segment_sha256"]["data"], hashlib.sha256(bytes(18432)).hexdigest()
        )

    def test_global_address_alias_writes_the_same_code_allocation(self):
        local = scan_ldr(ldr_image())
        global_alias = scan_ldr(ldr_image(load_address=0xC0000000))
        self.assertEqual(global_alias["segment_sha256"], local["segment_sha256"])

    def test_zero_command_does_not_treat_coremem_code_as_direct_write(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            scan_ldr(ldr_image(zero_address=0x800061A0))

    def test_cli_stages_private_segments_only_after_full_validation(self):
        script = Path(__file__).resolve().parents[1] / "drm/rgx_meta_ldr.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fw.bin"
            source.write_bytes(ldr_image())
            output = root / "staged"
            result = subprocess.run(
                [sys.executable, str(script), str(source), "--output-dir", str(output)],
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o700)
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {"code.bin", "data.bin", "coremem_code.bin", "coremem_data.bin"},
            )
            for kind, digest in summary["segment_sha256"].items():
                path = output / f"{kind}.bin"
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

            bad = bytearray(ldr_image())
            struct.pack_into("<Q", bad, len(bad) - 4096 + 16, 0)
            source.write_bytes(bad)
            rejected = root / "rejected"
            result = subprocess.run(
                [sys.executable, str(script), str(source), "--output-dir", str(rejected)],
                capture_output=True, text=True, timeout=5,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(rejected.exists())

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
