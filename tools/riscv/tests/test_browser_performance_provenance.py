# SPDX-License-Identifier: MPL-2.0

"""Tests for immutable Firefox performance runtime provenance."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

from tools.riscv.debian.rootfs import browser_performance_provenance as provenance


def framebuffer_records(
    *, width: int = 1920, height: int = 1080, stride: int = 7680
) -> tuple[bytes, bytes]:
    variable = bytearray(160)
    fixed = bytearray(80)
    struct.pack_into("=8I", variable, 0, width, height, width, height, 0, 0, 32, 0)
    struct.pack_into("=3I", variable, 32, 16, 8, 0)
    struct.pack_into("=3I", variable, 44, 8, 8, 0)
    struct.pack_into("=3I", variable, 56, 0, 8, 0)
    struct.pack_into("=3I", variable, 68, 24, 8, 0)
    struct.pack_into("=I", fixed, 24, stride * height)
    struct.pack_into("=I", fixed, 36, 2)
    struct.pack_into("=I", fixed, 48, stride)
    return bytes(variable), bytes(fixed)


class BrowserPerformanceProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "var/lib/dpkg").mkdir(parents=True)
        (self.root / "usr/bin").mkdir(parents=True)
        (self.root / "usr/share/asterinas").mkdir(parents=True)
        (self.root / "var/lib/dpkg/status").write_text(
            "Package: xserver-xorg-core\n"
            "Status: install ok installed\n"
            "Version: 2:21.1.16-1\n\n"
            "Package: xserver-xorg-video-fbdev\n"
            "Status: install ok installed\n"
            "Version: 1:0.5.0-2\n\n",
            encoding="utf-8",
        )
        (self.root / "usr/bin/firefox-esr").write_bytes(b"firefox-esr")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_builds_exact_fbdev_esr_runtime_provenance(self) -> None:
        variable, fixed = framebuffer_records()

        result = provenance.build_runtime_provenance(
            root=self.root,
            display_provider="fbdev",
            framebuffer_variable=variable,
            framebuffer_fixed=fixed,
        )

        self.assertEqual(
            result,
            {
                "schema_version": 1,
                "display_provider": "fbdev",
                "framebuffer": {
                    "width": 1920,
                    "height": 1080,
                    "stride_bytes": 7680,
                    "bits_per_pixel": 32,
                },
                "firefox": {
                    "executable": "/usr/bin/firefox-esr",
                    "jit_overlay": False,
                },
                "packages": {
                    "xserver-xorg-core": "2:21.1.16-1",
                    "xserver-xorg-video-fbdev": "1:0.5.0-2",
                },
            },
        )

    def test_selects_only_a_marker_bound_jit_firefox(self) -> None:
        variable, fixed = framebuffer_records()
        (self.root / "usr/bin/firefox").write_bytes(b"firefox-jit")
        marker = self.root / "usr/share/asterinas/firefox-riscv-jit-overlay.json"
        marker.write_text('{"schema_version":1}\n', encoding="utf-8")

        result = provenance.build_runtime_provenance(
            root=self.root,
            display_provider="fbdev",
            framebuffer_variable=variable,
            framebuffer_fixed=fixed,
        )

        self.assertEqual(result["firefox"]["executable"], "/usr/bin/firefox")
        self.assertIs(result["firefox"]["jit_overlay"], True)

    def test_rejects_invalid_runtime_inputs(self) -> None:
        variable, fixed = framebuffer_records()
        invalid_variable, _ = framebuffer_records(width=0)
        cases = (
            {"display_provider": "unknown"},
            {"framebuffer_variable": invalid_variable},
            {"framebuffer_variable": variable[:-1]},
            {"framebuffer_fixed": fixed[:-1]},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                arguments = {
                    "root": self.root,
                    "display_provider": "fbdev",
                    "framebuffer_variable": variable,
                    "framebuffer_fixed": fixed,
                    **changes,
                }
                with self.assertRaises(provenance.ProvenanceError):
                    provenance.build_runtime_provenance(**arguments)

    def test_rejects_missing_packages_and_inconsistent_jit_files(self) -> None:
        variable, fixed = framebuffer_records()
        status = self.root / "var/lib/dpkg/status"
        status.write_text("Package: xserver-xorg-core\nVersion: 1\n\n")
        with self.assertRaises(provenance.ProvenanceError):
            provenance.build_runtime_provenance(
                root=self.root,
                display_provider="fbdev",
                framebuffer_variable=variable,
                framebuffer_fixed=fixed,
            )

        status.write_text(
            "Package: xserver-xorg-core\nStatus: install ok installed\nVersion: 1\n\n"
            "Package: xserver-xorg-video-fbdev\nStatus: install ok installed\nVersion: 2\n\n"
        )
        (self.root / "usr/share/asterinas/firefox-riscv-jit-overlay.json").write_text(
            "{}\n"
        )
        with self.assertRaises(provenance.ProvenanceError):
            provenance.build_runtime_provenance(
                root=self.root,
                display_provider="fbdev",
                framebuffer_variable=variable,
                framebuffer_fixed=fixed,
            )

    def test_binds_validated_runtime_to_the_final_manifest(self) -> None:
        variable, fixed = framebuffer_records()
        runtime = provenance.build_runtime_provenance(
            root=self.root,
            display_provider="fbdev",
            framebuffer_variable=variable,
            framebuffer_fixed=fixed,
        )
        manifest = self.root / "rootfs-manifest.json"
        manifest.write_bytes(b'{"schema_version":7}\n')

        result = provenance.bind_runtime_provenance(
            (json.dumps(runtime) + "\n").encode(), manifest
        )

        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(
            result["rootfs_manifest_sha256"],
            hashlib.sha256(manifest.read_bytes()).hexdigest(),
        )
        self.assertEqual(result["display_provider"], "fbdev")

    def test_binding_rejects_extra_fields_and_symlinked_manifest(self) -> None:
        variable, fixed = framebuffer_records()
        runtime = provenance.build_runtime_provenance(
            root=self.root,
            display_provider="fbdev",
            framebuffer_variable=variable,
            framebuffer_fixed=fixed,
        )
        manifest = self.root / "rootfs-manifest.json"
        target = self.root / "manifest-target.json"
        target.write_text("{}\n")
        manifest.symlink_to(target)

        with self.assertRaises(provenance.ProvenanceError):
            provenance.bind_runtime_provenance(
                (json.dumps(runtime) + "\n").encode(), manifest
            )
        with self.assertRaises(provenance.ProvenanceError):
            provenance.validate_runtime_provenance({**runtime, "extra": True})


if __name__ == "__main__":
    unittest.main()
