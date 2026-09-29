#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "drm"))
from powervr_dma_stage import EXPECTED_FIRMWARE_SHA256, checked_frames


SIZES = {"code": 52_064, "data": 18_432, "coremem_code": 73_312, "coremem_data": 9_984}


class CheckedFramesTests(unittest.TestCase):
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
