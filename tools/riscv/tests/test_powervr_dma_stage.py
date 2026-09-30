#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "drm"))
from powervr_dma_stage import EXPECTED_FIRMWARE_SHA256, checked_frames, decode_status, require_stage_opt_in


SIZES = {"code": 52_064, "data": 18_432, "coremem_code": 73_312, "coremem_data": 9_984}


class CheckedFramesTests(unittest.TestCase):
    def test_requires_prepared_config_and_matching_addresses_before_staging(self):
        template = (Path(__file__).resolve().parents[3] /
                    "kernel/src/device/dri/powervr_probe/meta_boot_config.bin").read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            digests = {}
            for name, size in SIZES.items():
                payload = bytearray(size)
                if name == "code":
                    payload[512:808] = template
                (root / f"{name}.bin").write_bytes(payload)
                digests[name] = hashlib.sha256(payload).hexdigest()
            record = {
                "processor": "META", "firmware_sha256": EXPECTED_FIRMWARE_SHA256,
                "segment_sha256": digests,
                "firmware_vaddrs": [0xE1C0000000, 0xE1C000E000, 0xE1C0014000, 0xE1C0027000],
                "boot_config_offset": 512, "boot_config_bytes": 296,
                "boot_config_pairs": 34, "boot_config_ldr_writes": 17,
                "boot_config_sha256": hashlib.sha256(template).hexdigest(),
                "meta_threads": 2, "meta_dma": True, "slc_vivt": True,
            }
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps(record))
            self.assertEqual(len(checked_frames(root, manifest, require_boot_config=True)), 4)
            record["firmware_vaddrs"][1] += 4096
            manifest.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "boot configuration"):
                checked_frames(root, manifest, require_boot_config=True)
            record["firmware_vaddrs"][1] -= 4096
            payload = bytearray((root / "code.bin").read_bytes())
            payload[540] ^= 1
            (root / "code.bin").write_bytes(payload)
            record["segment_sha256"]["code"] = hashlib.sha256(payload).hexdigest()
            manifest.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "boot configuration"):
                checked_frames(root, manifest, require_boot_config=True)

    def test_status_preserves_faults_and_rejects_incompatible_frames(self):
        payload = b"PVS1" + struct.pack("<11I", 1, 1, 123, 2, 0x40, 3, 1, 0x190001, 42, 0x1234, 1)
        observed = decode_status(payload)
        self.assertEqual(observed["meta_release_attempted"], 1)
        self.assertEqual(observed["firmware_started"], 1)
        self.assertEqual(observed["firmware_faults"], 2)
        self.assertEqual(observed["hwr_state"], 0x40)
        self.assertEqual(observed["hwr_count"], 3)
        self.assertEqual(observed["ddk_build"], 42)
        with self.assertRaisesRegex(ValueError, "status frame"):
            decode_status(payload[:-1])
        with self.assertRaisesRegex(ValueError, "status frame"):
            decode_status(b"PVS2" + payload[4:])

    def test_requires_both_kernel_opt_ins_before_staging(self):
        require_stage_opt_in("console=tty0 asterinas.powervr=1 asterinas.powervr_dma_stage=1")
        with self.assertRaisesRegex(ValueError, "asterinas.powervr=1"):
            require_stage_opt_in("console=tty0 asterinas.powervr_dma_stage=1")
        with self.assertRaisesRegex(ValueError, "asterinas.powervr_dma_stage=1"):
            require_stage_opt_in("console=tty0 asterinas.powervr=1")
        with self.assertRaisesRegex(ValueError, "asterinas.powervr_boot_config_preflight=1"):
            require_stage_opt_in("asterinas.powervr=1 asterinas.powervr_dma_stage=1", boot_config_check=True)
        require_stage_opt_in(
            "asterinas.powervr=1 asterinas.powervr_dma_stage=1 asterinas.powervr_boot_config_preflight=1",
            boot_config_check=True,
        )

    def test_client_starts_from_shallow_guest_path(self):
        with tempfile.TemporaryDirectory() as directory:
            relocated = Path(directory) / "powervr_dma_stage.py"
            shutil.copyfile(Path(__file__).resolve().parents[1] / "drm/powervr_dma_stage.py", relocated)
            result = subprocess.run(
                [sys.executable, str(relocated), "--help"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--manifest", result.stdout)

    def test_validates_all_four_segments_before_emitting_exact_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            digests = {}
            for name, size in SIZES.items():
                data = bytes([len(name)]) * size
                (root / f"{name}.bin").write_bytes(data)
                digests[name] = hashlib.sha256(data).hexdigest()
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "processor": "META",
                "firmware_sha256": EXPECTED_FIRMWARE_SHA256,
                "segment_sha256": digests,
            }))

            frames = checked_frames(root, manifest)
            self.assertEqual(len(frames), 4)
            for number, (name, size) in enumerate(SIZES.items()):
                frame = frames[number]
                self.assertEqual(frame[:4], b"PVR1")
                self.assertEqual(int.from_bytes(frame[4:8], "little"), number)
                self.assertEqual(int.from_bytes(frame[8:12], "little"), size)
                self.assertEqual(hashlib.sha256(frame[12:]).hexdigest(), digests[name])

            (root / "coremem_data.bin").write_bytes(b"x" * SIZES["coremem_data"])
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                checked_frames(root, manifest)


if __name__ == "__main__":
    unittest.main()
