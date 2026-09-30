#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools" / "riscv"))
from tools.riscv.drm.powervr_release_gate import build_initramfs


SIZES = {"code": 52_064, "data": 18_432, "coremem_code": 73_312, "coremem_data": 9_984}
BOOT_CONFIG = (REPO_ROOT / "kernel/src/device/dri/powervr_probe/meta_boot_config.bin").read_bytes()


class PowerVrReleaseGateTests(unittest.TestCase):
    def test_builder_requires_manifest_and_embeds_all_validated_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = root / "segments"
            segments.mkdir()
            digests = {}
            for name, size in SIZES.items():
                payload = bytearray(size)
                if name == "code":
                    payload[512:808] = BOOT_CONFIG
                (segments / f"{name}.bin").write_bytes(payload)
                digests[name] = hashlib.sha256(payload).hexdigest()
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "processor": "META",
                "firmware_sha256": "25e9e7ff4645292ceb991617dba028e350a5c17088a105230dbaaaf995f4413b",
                "segment_sha256": digests,
                "firmware_vaddrs": [0xE1C0000000, 0xE1C000E000, 0xE1C0014000, 0xE1C0027000],
                "boot_config_offset": 512,
                "boot_config_bytes": 296,
                "boot_config_pairs": 34,
                "boot_config_ldr_writes": 17,
                "boot_config_sha256": "2c70442a16a343dbae1312d3d1f54fedf773a26d544ff4702c58cb8fcd88d4d5",
                "meta_threads": 2,
                "meta_dma": True,
                "slc_vivt": True,
            }))
            output = root / "release.cpio.gz"
            fake_init = b"\x7fELF\x02fake-init"
            with mock.patch(
                "tools.riscv.drm.powervr_release_gate._compile_init",
                side_effect=lambda path: path.write_bytes(fake_init),
            ):
                build_initramfs(output, segments, manifest)
            archive = gzip.decompress(output.read_bytes())
            self.assertIn(b"pvr/code.bin", archive)
            self.assertIn(b"pvr/data.bin", archive)
            self.assertIn(b"pvr/coremem_code.bin", archive)
            self.assertIn(b"pvr/coremem_data.bin", archive)


if __name__ == "__main__":
    unittest.main()
