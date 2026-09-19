# SPDX-License-Identifier: MPL-2.0

import hashlib
import json
import tempfile
import unittest
import zlib
from pathlib import Path

from tools.riscv import megrez_desktop_boot as boot


class DesktopBootManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        artifacts = {}
        for name, payload in (
            ("kernel", b"kernel-image"),
            ("initramfs", b"stage1-cpio"),
            ("megrez_dtb", b"prepared-dtb"),
            ("root_image", b"root-identity-only"),
        ):
            path = self.root / name
            path.write_bytes(payload)
            artifacts[name] = {
                "name": name,
                "path": str(path),
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "crc32": f"{zlib.crc32(payload):08x}",
                "load_address": {
                    "kernel": 0x80200000,
                    "initramfs": 0x83000000,
                    "megrez_dtb": 0xF0000000,
                    "root_image": 0,
                }[name],
            }
        self.plan = self.root / "plan.json"
        self.plan.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "profile": "debian-browser",
                    "smp": 4,
                    "sv39": True,
                    "reboot_after": 600,
                    "bootargs": "console=ttyS0 init=/init -- --root-init=systemd",
                    "markers": [],
                    "artifacts": list(artifacts.values()),
                }
            )
        )

    def test_manifest_is_canonical_and_content_addressed(self) -> None:
        manifest = boot.DesktopBootManifest.from_plan(self.plan)
        document = json.loads(manifest.canonical_bytes())

        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["stage1_protocol_version"], 1)
        self.assertEqual(document["generation_sha256"], manifest.generation_sha256)
        self.assertEqual(len(manifest.generation_sha256), 64)
        self.assertTrue(manifest.generation_directory.endswith(manifest.generation_sha256[:16]))
        self.assertEqual(manifest.canonical_bytes(), manifest.canonical_bytes())

    def test_generation_uses_safe_sha_prefixed_p3_paths(self) -> None:
        manifest = boot.DesktopBootManifest.from_plan(self.plan)
        commands = boot.uboot_load_commands(manifest)

        self.assertEqual(len(commands), 3)
        self.assertTrue(all(command.startswith("ext4load mmc 1:3 ") for command in commands))
        self.assertTrue(all(".." not in command for command in commands))
        for name in ("kernel", "initramfs", "megrez_dtb"):
            artifact = manifest.artifacts[name]
            self.assertIn(artifact.sha256[:16], artifact.basename)
            self.assertIn(manifest.generation_directory, artifact.mmc_path)

    def test_rejects_tampered_and_unsafe_plan_artifacts(self) -> None:
        document = json.loads(self.plan.read_text())
        document["artifacts"][0]["sha256"] = "0" * 64
        self.plan.write_text(json.dumps(document))
        with self.assertRaisesRegex(boot.DesktopBootError, "identity mismatch"):
            boot.DesktopBootManifest.from_plan(self.plan)


if __name__ == "__main__":
    unittest.main()
