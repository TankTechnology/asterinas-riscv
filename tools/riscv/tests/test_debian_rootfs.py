#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from tools.riscv.debian.rootfs.contract import (
    GATE_IDENTITY_PACKAGES,
    INSTALL_PACKAGES,
    ROOT_LABEL,
    load_manifest,
    parse_packages_lock,
    validate_frozen_root,
)


ROOT_IMAGE_SIZE_BYTES = 1024 * 1024 * 1024
ZERO_FILLED_ROOT_SHA256 = (
    "49bc20df15e412a64472421e13fe86ff1c5165e18b2afccf160d4dc19fe68a14"
)

PACKAGE_ROWS = (
    ("base-files", "riscv64", "13.8+deb13u1"),
    ("bash", "riscv64", "5.2.37-2+b5"),
    ("ca-certificates", "all", "20250419"),
    ("coreutils", "riscv64", "9.7-3"),
    ("libc6", "riscv64", "2.41-12"),
    ("procps", "riscv64", "2:4.0.4-9"),
    ("util-linux", "riscv64", "2.41-5"),
)


def _lock_text(rows: tuple[tuple[str, str, str], ...] = PACKAGE_ROWS) -> str:
    return "".join("\t".join(row) + "\n" for row in rows)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _manifest_payload(packages_lock_sha256: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "suite": "trixie",
        "debian_release": "13",
        "mirror_url": "https://deb.debian.org/debian",
        "architecture": "riscv64",
        "signed_metadata": {
            "url": "https://deb.debian.org/debian/dists/trixie/InRelease",
            "sha256": hashlib.sha256(b"InRelease").hexdigest(),
        },
        "packages_lock_sha256": packages_lock_sha256,
        "downloaded_packages": [
            {
                "name": name,
                "architecture": "riscv64",
                "version": f"1.{index}",
                "sha256": hashlib.sha256(name.encode()).hexdigest(),
            }
            for index, name in enumerate(INSTALL_PACKAGES)
        ],
        "filesystem": {
            "type": "ext2",
            "label": ROOT_LABEL,
            "uuid": "7b7ad749-77d0-4e59-89e4-e117244a70aa",
            "size_bytes": ROOT_IMAGE_SIZE_BYTES,
            "block_size_bytes": 4096,
        },
        "tool_versions": {
            "debootstrap": "1.0.141",
            "mke2fs": "1.47.2",
            "qemu-riscv64-static": "10.0.2",
        },
        "build_timestamp": "2026-08-24T00:00:00Z",
        "root_image_sha256": ZERO_FILLED_ROOT_SHA256,
        "gate_packages": {
            name: version
            for name, architecture, version in PACKAGE_ROWS
            if name in GATE_IDENTITY_PACKAGES and architecture == "riscv64"
        },
    }


class DebianRootfsContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary_directory.cleanup)
        cls.directory = Path(cls.temporary_directory.name)
        cls.image = cls.directory / "debian-root.ext2"
        with cls.image.open("wb") as image_file:
            image_file.truncate(ROOT_IMAGE_SIZE_BYTES)

    def setUp(self) -> None:
        self.packages_lock = self.directory / "packages.lock"
        self.manifest_path = self.directory / "rootfs-manifest.json"
        self.packages_lock.write_text(_lock_text(), encoding="utf-8")
        self.payload = _manifest_payload(_sha256_text(_lock_text()))

    def write_manifest(self, payload: dict[str, object] | None = None) -> None:
        self.manifest_path.write_text(
            json.dumps(self.payload if payload is None else payload),
            encoding="utf-8",
        )

    def load_and_validate(self):
        self.write_manifest()
        manifest = load_manifest(self.manifest_path)
        rows = parse_packages_lock(self.packages_lock)
        return validate_frozen_root(self.image, manifest, self.packages_lock), rows

    def test_accepts_frozen_manifest_and_complete_package_lock(self) -> None:
        validated, rows = self.load_and_validate()

        self.assertEqual(
            INSTALL_PACKAGES,
            (
                "bash",
                "ca-certificates",
                "coreutils",
                "procps",
                "util-linux",
            ),
        )
        self.assertEqual(
            GATE_IDENTITY_PACKAGES,
            (
                "base-files",
                "libc6",
                "bash",
                "coreutils",
                "util-linux",
            ),
        )
        self.assertEqual(ROOT_LABEL, "ASTER_DEBIAN_ROOT")
        self.assertEqual(rows, PACKAGE_ROWS)
        self.assertEqual(validated.filesystem.size_bytes, ROOT_IMAGE_SIZE_BYTES)
        with self.assertRaises(FrozenInstanceError):
            validated.suite = "forky"
        with self.assertRaises(FrozenInstanceError):
            validated.filesystem.label = "mutable"

    def test_rejects_missing_and_unknown_json_keys(self) -> None:
        cases: list[tuple[str, dict[str, object]]] = []

        missing_top_level = copy.deepcopy(self.payload)
        del missing_top_level["architecture"]
        cases.append(("missing manifest fields", missing_top_level))

        unknown_top_level = copy.deepcopy(self.payload)
        unknown_top_level["extra"] = "not allowed"
        cases.append(("unknown manifest fields", unknown_top_level))

        missing_filesystem = copy.deepcopy(self.payload)
        del missing_filesystem["filesystem"]["uuid"]
        cases.append(("missing filesystem fields", missing_filesystem))

        unknown_signed_metadata = copy.deepcopy(self.payload)
        unknown_signed_metadata["signed_metadata"]["signature"] = "detached"
        cases.append(("unknown signed_metadata fields", unknown_signed_metadata))

        for expected_error, payload in cases:
            with self.subTest(expected_error=expected_error):
                self.write_manifest(payload)
                with self.assertRaisesRegex(ValueError, expected_error):
                    load_manifest(self.manifest_path)

    def test_rejects_booleans_where_integers_are_required(self) -> None:
        cases = (
            (("schema_version",), True),
            (("filesystem", "size_bytes"), True),
            (("filesystem", "block_size_bytes"), False),
        )

        for path, value in cases:
            with self.subTest(path=".".join(path)):
                payload = copy.deepcopy(self.payload)
                target = payload
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                self.write_manifest(payload)
                with self.assertRaisesRegex(ValueError, "must be an integer"):
                    load_manifest(self.manifest_path)

        self.write_manifest()
        manifest = load_manifest(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "must be an integer"):
            validate_frozen_root(
                self.image,
                replace(manifest, schema_version=True),
                self.packages_lock,
            )

    def test_rejects_non_https_provenance_urls(self) -> None:
        cases = (
            (("mirror_url",), "http://deb.debian.org/debian"),
            (
                ("signed_metadata", "url"),
                "file:///var/cache/debian/dists/trixie/InRelease",
            ),
        )

        for path, value in cases:
            with self.subTest(path=".".join(path)):
                payload = copy.deepcopy(self.payload)
                target = payload
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                self.write_manifest(payload)
                manifest = load_manifest(self.manifest_path)
                with self.assertRaisesRegex(ValueError, "HTTPS URL"):
                    validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_wrong_debian_and_filesystem_identity(self) -> None:
        cases = (
            (("suite",), "bookworm"),
            (("debian_release",), "12"),
            (("architecture",), "amd64"),
            (("filesystem", "type"), "ext4"),
            (("filesystem", "label"), "DEBIAN_ROOT"),
            (("filesystem", "size_bytes"), ROOT_IMAGE_SIZE_BYTES // 2),
            (("filesystem", "block_size_bytes"), 1024),
        )

        for path, value in cases:
            with self.subTest(path=".".join(path)):
                payload = copy.deepcopy(self.payload)
                target = payload
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                self.write_manifest(payload)
                manifest = load_manifest(self.manifest_path)
                with self.assertRaisesRegex(ValueError, "unexpected"):
                    validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_malformed_sha256_values(self) -> None:
        paths = (
            ("signed_metadata", "sha256"),
            ("packages_lock_sha256",),
            ("downloaded_packages", 0, "sha256"),
            ("root_image_sha256",),
        )

        for path in paths:
            with self.subTest(path=".".join(str(part) for part in path)):
                payload = copy.deepcopy(self.payload)
                target = payload
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = "A" * 64
                self.write_manifest(payload)
                with self.assertRaisesRegex(ValueError, "lowercase SHA-256"):
                    load_manifest(self.manifest_path)

    def test_rejects_duplicate_and_unsorted_package_entries(self) -> None:
        cases = (
            PACKAGE_ROWS + (PACKAGE_ROWS[-1],),
            tuple(reversed(PACKAGE_ROWS)),
        )

        for rows in cases:
            with self.subTest(rows=rows):
                lock_text = _lock_text(rows)
                self.packages_lock.write_text(lock_text, encoding="utf-8")
                self.payload["packages_lock_sha256"] = _sha256_text(lock_text)
                self.write_manifest()
                manifest = load_manifest(self.manifest_path)
                with self.assertRaisesRegex(ValueError, "sorted and unique"):
                    validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_manifest_package_lock_version_mismatch(self) -> None:
        self.payload["gate_packages"]["bash"] = "0.invalid"
        self.write_manifest()

        manifest = load_manifest(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "gate package bash version"):
            validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_base_image_size_and_hash_mismatch(self) -> None:
        self.write_manifest()
        manifest = load_manifest(self.manifest_path)
        short_image = self.directory / "short.ext2"
        short_image.write_bytes(b"not one GiB")

        with self.assertRaisesRegex(ValueError, "image size"):
            validate_frozen_root(short_image, manifest, self.packages_lock)

        payload = copy.deepcopy(self.payload)
        payload["root_image_sha256"] = "0" * 64
        self.write_manifest(payload)
        manifest = load_manifest(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "image SHA-256"):
            validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_package_lock_hash_mismatch(self) -> None:
        self.payload["packages_lock_sha256"] = "0" * 64
        self.write_manifest()

        manifest = load_manifest(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "package-lock SHA-256"):
            validate_frozen_root(self.image, manifest, self.packages_lock)


if __name__ == "__main__":
    unittest.main()
