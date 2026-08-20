#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ltp_package import publish_package_identity, validate_package_identity


class LtpPackageIdentityTests(unittest.TestCase):
    def test_identity_binds_suite_manifest_unavailable_and_initramfs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            initramfs = directory / "initramfs.cpio.gz"
            manifest = directory / "manifest.txt"
            unavailable = directory / "unavailable-tests.json"
            identity = directory / "package.json"
            initramfs.write_bytes(b"archive")
            manifest.write_text("read01 read01\n")
            unavailable.write_text("[]\n")

            publish_package_identity(
                suite="arch-riscv64",
                initramfs=initramfs,
                manifest=manifest,
                unavailable=unavailable,
                output=identity,
            )

            payload = json.loads(identity.read_text())
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["suite"], "arch-riscv64")
            self.assertEqual(
                payload["initramfs_sha256"],
                hashlib.sha256(b"archive").hexdigest(),
            )
            validate_package_identity(
                suite="arch-riscv64",
                initramfs=initramfs,
                manifest=manifest,
                unavailable=unavailable,
                identity=identity,
            )

            initramfs.write_bytes(b"stale archive")
            with self.assertRaisesRegex(ValueError, "initramfs_sha256"):
                validate_package_identity(
                    suite="arch-riscv64",
                    initramfs=initramfs,
                    manifest=manifest,
                    unavailable=unavailable,
                    identity=identity,
                )


if __name__ == "__main__":
    unittest.main()
