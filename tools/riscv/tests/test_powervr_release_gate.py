#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

import gzip
import hashlib
import io
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
from tools.riscv.drm import powervr_release_board as board


SIZES = {"code": 52_064, "data": 18_432, "coremem_code": 73_312, "coremem_data": 9_984}
BOOT_CONFIG = (REPO_ROOT / "kernel/src/device/dri/powervr_probe/meta_boot_config.bin").read_bytes()


class PowerVrReleaseGateTests(unittest.TestCase):
    def test_no_reset_recovery_refuses_before_opening_serial(self):
        argv = [
            "release-gate", "/dev/serial/by-id/test", "--artifact-dir", "/unused",
            "--kernel", "kernel.Image", "--kernel-lzma", "kernel.lzma",
            "--bundle", "/unused/bundle.toml", "--initrd", "initramfs.cpio",
            "--dtb", "board.dtb", "--log", "/unused/log",
        ]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(board, "open_serial") as serial:
            with mock.patch.object(sys, "stderr", io.StringIO()), self.assertRaises(SystemExit) as error:
                board.main()
            self.assertEqual(error.exception.code, 2)
            serial.assert_not_called()

    def test_offline_dtb_is_uploaded_instead_of_read_from_mmc(self):
        session = board.OfflineBoardSession.__new__(board.OfflineBoardSession)
        session.artifact_directory = Path("/host/artifacts")
        session.load_ymodem_artifact = mock.Mock(return_value=155097)
        self.assertEqual(session.load_artifact("dtb", "board.dtb", 0xF0000000, "40ed4c65"), 155097)
        session.load_ymodem_artifact.assert_called_once_with(
            "dtb", Path("/host/artifacts"), "board.dtb", 0xF0000000, "40ed4c65"
        )

    def test_test_kernel_is_rejected_before_serial_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle.toml"
            bundle.write_text('action = "Test"\n')
            with self.assertRaisesRegex(ValueError, "Run bundle"):
                board.validate_boot_kernel(bundle, root / "kernel.Image", root / "kernel.lzma")

    def test_run_bundle_and_lzma_must_match_the_frozen_kernel(self):
        import lzma
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "kernel.Image"
            image.write_bytes(b"production-image")
            compressed = root / "kernel.lzma"
            compressed.write_bytes(lzma.compress(image.read_bytes(), format=lzma.FORMAT_ALONE))
            bundle = root / "bundle.toml"
            bundle.write_text('action = "Run"\n\n[aster_bin]\npath = "kernel.Image"\n')
            board.validate_boot_kernel(bundle, image, compressed)
            compressed.write_bytes(lzma.compress(b"wrong-image", format=lzma.FORMAT_ALONE))
            with self.assertRaisesRegex(ValueError, "compressed kernel"):
                board.validate_boot_kernel(bundle, image, compressed)
            image.write_bytes(b"[ktest runner] All crates tested.")
            with self.assertRaisesRegex(ValueError, "ktest"):
                board.validate_boot_kernel(bundle, image, compressed)

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
