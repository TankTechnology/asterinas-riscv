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
        self.assertNotIn(str(self.root), manifest.canonical_bytes().decode())

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

    def test_rejects_symlink_source(self) -> None:
        document = json.loads(self.plan.read_text())
        source = Path(document["artifacts"][0]["path"])
        target = source.with_suffix(".real")
        source.rename(target)
        source.symlink_to(target)

        with self.assertRaisesRegex(boot.DesktopBootError, "symbolic link"):
            boot.DesktopBootManifest.from_plan(self.plan)

    def test_rejects_duplicate_artifact_names(self) -> None:
        document = json.loads(self.plan.read_text())
        document["artifacts"].append(document["artifacts"][0])
        self.plan.write_text(json.dumps(document))

        with self.assertRaisesRegex(boot.DesktopBootError, "duplicate"):
            boot.DesktopBootManifest.from_plan(self.plan)

    def test_publication_script_is_atomic_idempotent_and_partition3_only(self) -> None:
        manifest = boot.DesktopBootManifest.from_plan(self.plan)
        script = boot.publication_script(
            manifest, "http://10.100.19.216:18080", "0123456789abcdef"
        )

        self.assertIn("mktemp -d", script)
        self.assertIn("trap", script)
        self.assertIn("mv -T", script)
        self.assertIn("sha256sum -c", script)
        self.assertIn("ASTERINAS_DESKTOP_GENERATION_READY", script)
        self.assertNotIn("mmcblk1p1", script)
        self.assertNotIn("mmcblk1p2", script)
        self.assertNotIn(str(self.root), script)
        self.assertNotIn("debian", script.split("GENERATION_ROOT=", 1)[0])
        self.assertIn(
            f'"{manifest.artifacts["kernel"].sha256}  $WORK/', script
        )

    def test_publication_script_rejects_unsafe_transport_inputs(self) -> None:
        manifest = boot.DesktopBootManifest.from_plan(self.plan)
        for base_url, nonce in (
            ("http://host/a b", "0123456789abcdef"),
            ("http://host", "../escape"),
        ):
            with self.subTest(base_url=base_url, nonce=nonce):
                with self.assertRaises(boot.DesktopBootError):
                    boot.publication_script(manifest, base_url, nonce)

    def test_prepare_uses_rockos_and_always_returns_to_uboot(self) -> None:
        manifest = boot.DesktopBootManifest.from_plan(self.plan)

        class Operations:
            def __init__(self) -> None:
                self.calls = []
                self.publication_transcript = (
                    "ASTERINAS_DESKTOP_GENERATION_READY "
                    f"generation={manifest.generation_sha256}"
                ).encode()

            def open(self, timeout):
                self.calls.append(("open", timeout))

            def boot_rockos(self, timeout):
                self.calls.append(("boot", timeout))

            def login(self, username, password, timeout):
                self.calls.append(("login", username, password, timeout))

            def publish(self, commands, password, timeout):
                self.calls.append(("publish", commands, password, timeout))

            def reboot_and_recover(self, password, timeout):
                self.calls.append(("recover", password, timeout))

            def close(self):
                self.calls.append(("close",))

        operations = Operations()
        transcript = boot.prepare_generation(
            manifest,
            operations,
            username="debian",
            password="secret",
            base_url="http://10.100.19.216:18080",
            nonce="0123456789abcdef",
        )

        self.assertEqual(transcript, operations.publication_transcript)
        self.assertEqual(
            [call[0] for call in operations.calls],
            ["open", "boot", "login", "publish", "recover", "close"],
        )
        launcher = operations.calls[3][1][0]
        self.assertIn(manifest.generation_sha256[:16], launcher)
        self.assertIn("sha256sum -c", launcher)
        self.assertNotIn("secret", launcher)

    def test_prepare_recovers_after_publication_failure(self) -> None:
        manifest = boot.DesktopBootManifest.from_plan(self.plan)

        class Operations:
            publication_transcript = b""

            def __init__(self) -> None:
                self.calls = []

            def open(self, timeout):
                self.calls.append("open")

            def boot_rockos(self, timeout):
                self.calls.append("boot")

            def login(self, username, password, timeout):
                self.calls.append("login")

            def publish(self, commands, password, timeout):
                self.calls.append("publish")
                raise RuntimeError("interrupted")

            def reboot_and_recover(self, password, timeout):
                self.calls.append("recover")

            def close(self):
                self.calls.append("close")

        operations = Operations()
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            boot.prepare_generation(
                manifest,
                operations,
                username="debian",
                password="secret",
                base_url="http://10.100.19.216:18080",
                nonce="0123456789abcdef",
            )
        self.assertEqual(operations.calls[-2:], ["recover", "close"])


if __name__ == "__main__":
    unittest.main()
