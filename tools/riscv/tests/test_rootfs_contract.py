#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the frozen rootfs manifest and signed-source contracts."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from tools.riscv.debian.rootfs import contract, profiles, signed_sources


def minimal_manifest() -> dict[str, object]:
    profile = profiles.get_profile("minimal")
    return {
        "schema_version": 1,
        "suite": "trixie",
        "debian_release": "13.0",
        "mirror_url": "https://mirror.example/debian",
        "architecture": "riscv64",
        "signed_metadata": {
            "url": "https://mirror.example/debian/dists/trixie/InRelease",
            "sha256": "a" * 64,
        },
        "packages_lock_sha256": "b" * 64,
        "downloaded_packages": [
            {
                "name": "bash",
                "architecture": "riscv64",
                "version": "5.2",
                "sha256": "c" * 64,
            }
        ],
        "filesystem": {
            "type": "ext2",
            "label": profile.root_label,
            "uuid": profile.root_uuid,
            "size_bytes": profile.root_size_bytes,
            "block_size_bytes": 4096,
        },
        "tool_versions": {"debootstrap": "1.0"},
        "build_timestamp": "2024-01-01T00:00:00Z",
        "root_image_sha256": "d" * 64,
        "gate_packages": {
            package: "1.0" for package in profile.identity_packages
        },
    }


class ManifestTests(unittest.TestCase):
    def write(self, document: object) -> Path:
        temporary = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        self.addCleanup(Path(temporary.name).unlink)
        temporary.write(json.dumps(document))
        temporary.close()
        return Path(temporary.name)

    def test_structurally_valid_manifest_loads(self) -> None:
        manifest = contract.load_manifest(self.write(minimal_manifest()))

        self.assertEqual(manifest.schema_version, 1)
        self.assertEqual(manifest.profile, "minimal")
        self.assertEqual(manifest.filesystem.label, "ASTER_DEBIANROOT")
        self.assertEqual(len(manifest.downloaded_packages), 1)

    def test_missing_schema_version_is_rejected(self) -> None:
        document = minimal_manifest()
        del document["schema_version"]

        with self.assertRaisesRegex(contract.ContractError, "schema_version"):
            contract.load_manifest(self.write(document))

    def test_invalid_json_and_duplicate_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            invalid = root / "invalid.json"
            invalid.write_text("{")
            duplicate = root / "duplicate.json"
            duplicate.write_text('{"schema_version": 1, "schema_version": 2}')

            with self.assertRaisesRegex(contract.ContractError, "invalid manifest JSON"):
                contract.load_manifest(invalid)
            with self.assertRaisesRegex(contract.ContractError, "duplicate JSON key"):
                contract.load_manifest(duplicate)

    def test_unsupported_schema_is_rejected(self) -> None:
        document = minimal_manifest()
        document["schema_version"] = 99

        with self.assertRaisesRegex(contract.ContractError, "unsupported manifest"):
            contract.load_manifest(self.write(document))

    def test_empty_downloads_and_partial_gate_packages_are_rejected(self) -> None:
        empty = minimal_manifest()
        empty["downloaded_packages"] = []
        partial = minimal_manifest()
        partial["gate_packages"] = {}

        with self.assertRaisesRegex(contract.ContractError, "must not be empty"):
            contract.load_manifest(self.write(empty))
        with self.assertRaisesRegex(contract.ContractError, "missing gate_packages"):
            contract.load_manifest(self.write(partial))

    def test_sha256_file_uses_the_whole_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "payload"
            path.write_bytes(b"asterinas")

            self.assertEqual(
                contract.sha256_file(path),
                hashlib.sha256(b"asterinas").hexdigest(),
            )

    def test_packages_lock_requires_three_tab_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "packages.lock"
            path.write_text("bash\triscv64\t5.2\n")
            self.assertEqual(
                contract.parse_packages_lock(path),
                (("bash", "riscv64", "5.2"),),
            )

            path.write_text("bash riscv64 5.2\n")
            with self.assertRaisesRegex(contract.ContractError, "tab-separated"):
                contract.parse_packages_lock(path)

    def test_verify_cli_requires_all_inputs(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            contract.main(["verify"])


class SignedSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = signed_sources.BASE_SOURCE
        self.security = signed_sources.SECURITY_SOURCE

    def test_sources_have_unique_roles_and_https_urls(self) -> None:
        roles = [source.role for source in signed_sources.SIGNED_SOURCES]
        self.assertEqual(roles, ["base", "security"])
        for source in signed_sources.SIGNED_SOURCES:
            with self.subTest(source=source.role):
                self.assertTrue(source.mirror_url.startswith("https://"))
                self.assertTrue(source.inrelease_url.startswith("https://"))
                self.assertTrue(source.inrelease_url.endswith("/InRelease"))

    def test_apt_filenames_map_to_one_source(self) -> None:
        base = (
            "mirror_debian_dists_trixie_main_binary-riscv64_Packages"
        )
        security = (
            "security_debian-security_dists_trixie-security_main_"
            "binary-riscv64_Packages"
        )

        self.assertIs(
            signed_sources.source_for_apt_list(base),
            signed_sources.BASE_SOURCE,
        )
        self.assertIs(
            signed_sources.source_for_apt_list(security),
            signed_sources.SECURITY_SOURCE,
        )
        with self.assertRaisesRegex(ValueError, "source owners"):
            signed_sources.source_for_apt_list("unknown_Packages")

    def test_packages_index_must_match_the_inrelease_row(self) -> None:
        index = b"Package: bash\nVersion: 5.2\n"
        digest = hashlib.sha256(index).hexdigest()
        release = (
            "Suite: stable\nSHA256:\n"
            f" {digest} {len(index)} main/binary-riscv64/Packages\n"
        ).encode()

        signed_sources.authenticate_packages(
            index, "main/binary-riscv64/Packages", release
        )
        with self.assertRaisesRegex(ValueError, "not uniquely authenticated"):
            signed_sources.authenticate_packages(
                index + b"x", "main/binary-riscv64/Packages", release
            )
        duplicate = release + release.split(b"Suite: stable\n", 1)[1]
        with self.assertRaisesRegex(ValueError, "not uniquely authenticated"):
            signed_sources.authenticate_packages(
                index, "main/binary-riscv64/Packages", duplicate
            )

    def test_manifest_fragment_hashes_each_signed_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {}
            for source in signed_sources.SIGNED_SOURCES:
                path = root / f"{source.role}.InRelease"
                path.write_text(source.role)
                files[source.role] = path

            rows = signed_sources.signed_sources_manifest(
                signed_sources.SIGNED_SOURCES, files
            )

            self.assertEqual([row["role"] for row in rows], ["base", "security"])
            self.assertEqual(
                rows[0]["inrelease_sha256"],
                hashlib.sha256(b"base").hexdigest(),
            )
            with self.assertRaisesRegex(ValueError, "files do not match"):
                signed_sources.signed_sources_manifest(
                    signed_sources.SIGNED_SOURCES, {"base": files["base"]}
                )


if __name__ == "__main__":
    unittest.main()
