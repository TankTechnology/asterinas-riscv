# SPDX-License-Identifier: MPL-2.0

import hashlib
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tools.riscv.debian.rootfs.dev_overlay import (
    OverlayError,
    build_derived_documents,
    load_overlay_spec,
    materialize_image,
    materialize_rootfs,
)


REPOSITORY_ROOT = Path(__file__).parents[3]
ROOTFS_DIRECTORY = REPOSITORY_ROOT / "tools/riscv/debian/rootfs"
BROWSER_WEB_SPEC = ROOTFS_DIRECTORY / "browser_web_dev_overlay.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_spec(directory: Path, files: list[dict[str, str]]) -> Path:
    path = directory / "overlay.json"
    path.write_text(
        json.dumps({"schema_version": 1, "profile": "browser-web", "files": files}),
        encoding="utf-8",
    )
    return path


def _debugfs(image: Path, command: str) -> str:
    return subprocess.run(
        ["debugfs", "-R", command, str(image)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@unittest.skipUnless(
    shutil.which("debugfs") and shutil.which("mke2fs"),
    "debugfs and mke2fs are required",
)
class DevelopmentOverlayTests(unittest.TestCase):
    def test_loads_exact_safe_overlay_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "runtime.sh"
            source.write_text("#!/bin/sh\necho new\n", encoding="utf-8")
            spec = _write_spec(
                root,
                [
                    {
                        "source": "runtime.sh",
                        "destination": "/usr/lib/asterinas/runtime",
                        "mode": "0755",
                    }
                ],
            )

            loaded = load_overlay_spec(spec, expected_profile="browser-web")

            self.assertEqual(loaded.profile, "browser-web")
            self.assertEqual(loaded.files[0].source, source)
            self.assertEqual(loaded.files[0].destination, "/usr/lib/asterinas/runtime")
            self.assertEqual(loaded.files[0].mode, 0o755)
            self.assertEqual(loaded.files[0].sha256, _sha256(source))

    def test_rejects_unsafe_or_ambiguous_overlay_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime.sh").write_text("new", encoding="utf-8")
            outside = root.parent / f"{root.name}-outside"
            outside.write_text("outside", encoding="utf-8")
            self.addCleanup(outside.unlink)
            cases = (
                (
                    {"schema_version": 1, "profile": "browser-web", "files": []},
                    "at least one file",
                ),
                (
                    {
                        "schema_version": 1,
                        "profile": "browser-web",
                        "files": [],
                        "unknown": True,
                    },
                    "unexpected overlay fields",
                ),
                (
                    {
                        "schema_version": 1,
                        "profile": "browser-web",
                        "files": [
                            {
                                "source": f"../{outside.name}",
                                "destination": "/safe",
                                "mode": "0644",
                            }
                        ],
                    },
                    "source must remain beneath",
                ),
                (
                    {
                        "schema_version": 1,
                        "profile": "browser-web",
                        "files": [
                            {
                                "source": "runtime.sh",
                                "destination": "/usr/../etc/passwd",
                                "mode": "0644",
                            }
                        ],
                    },
                    "canonical absolute path",
                ),
                (
                    {
                        "schema_version": 1,
                        "profile": "browser-web",
                        "files": [
                            {
                                "source": "runtime.sh",
                                "destination": "/safe",
                                "mode": "4755",
                            }
                        ],
                    },
                    "mode must be 0",
                ),
                (
                    {
                        "schema_version": 1,
                        "profile": "browser-web",
                        "files": [
                            {
                                "source": "runtime.sh",
                                "destination": "/safe;rm",
                                "mode": "0644",
                            }
                        ],
                    },
                    "safe debugfs path characters",
                ),
            )
            for payload, message in cases:
                with self.subTest(message=message):
                    spec = root / "case.json"
                    spec.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(OverlayError, message):
                        load_overlay_spec(spec, expected_profile="browser-web")

            duplicate = _write_spec(
                root,
                [
                    {
                        "source": "runtime.sh",
                        "destination": "/same",
                        "mode": "0644",
                    },
                    {
                        "source": "runtime.sh",
                        "destination": "/same",
                        "mode": "0755",
                    },
                ],
            )
            with self.assertRaisesRegex(OverlayError, "duplicate destination"):
                load_overlay_spec(duplicate, expected_profile="browser-web")

            with self.assertRaisesRegex(OverlayError, "profile does not match"):
                load_overlay_spec(duplicate, expected_profile="minimal-m1")

    def test_materializes_deterministic_image_without_changing_base(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "runtime.sh"
            source.write_text("#!/bin/sh\necho new\n", encoding="utf-8")
            source.chmod(0o755)
            spec = load_overlay_spec(
                _write_spec(
                    root,
                    [
                        {
                            "source": "runtime.sh",
                            "destination": "/usr/lib/asterinas/runtime",
                            "mode": "0755",
                        }
                    ],
                )
            )
            staged = root / "staged/usr/lib/asterinas"
            staged.mkdir(parents=True)
            (staged / "runtime").write_text("old\n", encoding="utf-8")
            base = root / "base.ext2"
            subprocess.run(
                [
                    "mke2fs",
                    "-q",
                    "-t",
                    "ext2",
                    "-d",
                    str(root / "staged"),
                    "-F",
                    str(base),
                    "16M",
                ],
                check=True,
            )
            base_hash = _sha256(base)
            first = root / "first.ext2"
            second = root / "second.ext2"

            first_hash = materialize_image(base, first, spec)
            self.assertIn(
                "Type: regular",
                _debugfs(first, "stat /usr/lib/asterinas/runtime"),
            )
            time.sleep(1.1)
            second_hash = materialize_image(base, second, spec)

            self.assertEqual(_sha256(base), base_hash)
            self.assertEqual(first_hash, _sha256(first))
            self.assertEqual(first_hash, second_hash)
            with tempfile.TemporaryDirectory() as dump_directory:
                base_dump = Path(dump_directory) / "base"
                output_dump = Path(dump_directory) / "output"
                _debugfs(base, f"dump /usr/lib/asterinas/runtime {base_dump}")
                _debugfs(first, f"dump /usr/lib/asterinas/runtime {output_dump}")
                self.assertEqual(base_dump.read_bytes(), b"old\n")
                self.assertEqual(output_dump.read_bytes(), source.read_bytes())
            self.assertRegex(
                _debugfs(first, "stat /usr/lib/asterinas/runtime"),
                r"Mode:\s+0755\b",
            )

    def test_failure_preserves_published_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "runtime.sh"
            source.write_text("new", encoding="utf-8")
            spec = load_overlay_spec(
                _write_spec(
                    root,
                    [
                        {
                            "source": "runtime.sh",
                            "destination": "/missing",
                            "mode": "0644",
                        }
                    ],
                )
            )
            base = root / "base.ext2"
            subprocess.run(
                ["mke2fs", "-q", "-t", "ext2", "-F", str(base), "16M"], check=True
            )
            output = root / "published.ext2"
            output.write_bytes(b"published")

            with self.assertRaisesRegex(OverlayError, "destination does not exist"):
                materialize_image(base, output, spec)

            self.assertEqual(output.read_bytes(), b"published")

    def test_rejects_symlinked_base_image_before_debugfs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "runtime.sh"
            source.write_text("new", encoding="utf-8")
            spec = load_overlay_spec(
                _write_spec(
                    root,
                    [
                        {
                            "source": "runtime.sh",
                            "destination": "/runtime",
                            "mode": "0644",
                        }
                    ],
                )
            )
            base = root / "base.ext2"
            base.write_bytes(b"not-an-ext-image")
            symlink = root / "base-link.ext2"
            symlink.symlink_to(base)

            with self.assertRaisesRegex(OverlayError, "non-symlink regular file"):
                materialize_image(symlink, root / "output.ext2", spec)

    def test_rejects_base_image_as_output_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "runtime.sh"
            source.write_text("new", encoding="utf-8")
            spec = load_overlay_spec(
                _write_spec(
                    root,
                    [
                        {
                            "source": "runtime.sh",
                            "destination": "/runtime",
                            "mode": "0644",
                        }
                    ],
                )
            )
            staged = root / "staged"
            staged.mkdir()
            (staged / "runtime").write_text("old", encoding="utf-8")
            base = root / "base.ext2"
            subprocess.run(
                [
                    "mke2fs",
                    "-q",
                    "-t",
                    "ext2",
                    "-d",
                    str(staged),
                    "-F",
                    str(base),
                    "16M",
                ],
                check=True,
            )
            base_hash = _sha256(base)

            with self.assertRaisesRegex(OverlayError, "must differ from the base"):
                materialize_image(base, base, spec)

            self.assertEqual(_sha256(base), base_hash)

    def test_derived_documents_preserve_package_identity_and_record_base(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "runtime.sh"
            source.write_text("new", encoding="utf-8")
            spec = load_overlay_spec(
                _write_spec(
                    root,
                    [
                        {
                            "source": "runtime.sh",
                            "destination": "/runtime",
                            "mode": "0755",
                        }
                    ],
                )
            )
            base_document = {
                "schema_version": 7,
                "profile": "browser-web",
                "packages_lock_sha256": "1" * 64,
                "downloaded_packages": [{"name": "firefox-esr"}],
                "tool_versions": {
                    "browser-web-runtime": "2" * 64,
                    "debootstrap": "test",
                },
                "root_image_sha256": "3" * 64,
            }

            derived, companion = build_derived_documents(
                base_document,
                base_manifest_sha256="4" * 64,
                derived_image_sha256="5" * 64,
                spec=spec,
            )

            self.assertEqual(derived["root_image_sha256"], "5" * 64)
            self.assertEqual(
                derived["downloaded_packages"], base_document["downloaded_packages"]
            )
            self.assertRegex(
                derived["tool_versions"]["asterinas-dev-overlay"], r"\A[0-9a-f]{64}\Z"
            )
            self.assertEqual(companion["base_root_image_sha256"], "3" * 64)
            self.assertEqual(companion["base_manifest_sha256"], "4" * 64)
            self.assertEqual(companion["derived_root_image_sha256"], "5" * 64)
            self.assertEqual(companion["files"][0]["sha256"], _sha256(source))

    def test_browser_web_spec_maps_only_guest_runtime_files(self) -> None:
        spec = load_overlay_spec(BROWSER_WEB_SPEC, expected_profile="browser-web")
        destinations = {entry.destination for entry in spec.files}
        self.assertEqual(
            destinations,
            {
                "/usr/lib/asterinas/desktop-m5-network-evidence",
                "/usr/lib/asterinas/megrez-safe-reboot",
                "/usr/lib/asterinas/browser-web-marionette-gate",
                "/usr/lib/asterinas/browser_m5_marionette_gate.py",
                "/usr/lib/asterinas/browser-web-firefox",
                "/usr/lib/asterinas/browser-web-evidence",
                "/usr/lib/asterinas/browser-web-timeline",
                "/usr/share/asterinas/browser-web-trust-check.py",
                "/usr/share/asterinas/browser-web-online-rootfs-check.py",
                "/usr/lib/asterinas/desktop-m5-session",
                "/usr/lib/asterinas/desktop-m5-device-access",
                "/usr/lib/asterinas/desktop-m5-evidence",
                "/etc/systemd/system/asterinas-browser-web.service",
                "/etc/systemd/system/asterinas-browser-web-evidence.service",
                "/etc/systemd/system/asterinas-browser-web-timeline-begin.service",
                "/etc/systemd/system/asterinas-browser-web-timeline-basic.service",
            },
        )
        self.assertNotIn(
            "desktop_m5_network_gate.py", {entry.source.name for entry in spec.files}
        )

    def test_make_default_publishes_beneath_user_writable_target(self) -> None:
        makefile = (REPOSITORY_ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn(
            "DEBIAN_BROWSER_WEB_DEV_ROOTFS ?= "
            "$(CURDIR)/target/dev-overlays/browser-web/rootfs",
            makefile,
        )

    def test_rejects_overlapping_base_and_output_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime.sh").write_text("new", encoding="utf-8")
            spec = _write_spec(
                root,
                [
                    {
                        "source": "runtime.sh",
                        "destination": "/runtime",
                        "mode": "0644",
                    }
                ],
            )

            with self.assertRaisesRegex(OverlayError, "must not overlap"):
                materialize_rootfs(root, spec, root / "derived")


if __name__ == "__main__":
    unittest.main()
