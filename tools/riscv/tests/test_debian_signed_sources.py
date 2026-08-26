# SPDX-License-Identifier: MPL-2.0

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.riscv.debian.rootfs.profiles import get_profile
from tools.riscv.debian.rootfs.signed_sources import (
    BASE_SOURCE,
    M5_SOURCES,
    SECURITY_SOURCE,
    authenticate_packages,
    require_unchanged,
    signed_sources_manifest,
    source_for_apt_list,
    verify_inrelease,
)


class DebianSignedSourcesTests(unittest.TestCase):
    BASE_HEADER = """Suite: stable
Version: 13.6
Codename: trixie
Architectures: all amd64 arm64 riscv64
Components: main contrib non-free-firmware non-free
"""
    SECURITY_HEADER = """Suite: stable-security
Version: 13
Codename: trixie-security
Architectures: amd64 arm64 riscv64
Components: updates/main updates/contrib updates/non-free-firmware updates/non-free
"""

    def test_browser_m5_profile_replaces_netsurf_with_firefox(self) -> None:
        profile = get_profile("browser-m5")
        self.assertEqual(profile.schema_version, 5)
        self.assertLessEqual(len(profile.root_label.encode("ascii")), 16)
        self.assertIn("firefox-esr", profile.requested_packages)
        self.assertNotIn("netsurf-gtk", profile.requested_packages)

    @mock.patch("subprocess.run")
    def test_each_release_is_independently_gpg_verified(self, run: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            keyring = root / "archive.gpg"
            keyring.touch()
            base = root / "base.InRelease"
            base.write_text(self.BASE_HEADER)
            security = root / "security.InRelease"
            security.write_text(self.SECURITY_HEADER)
            self.assertEqual(verify_inrelease(BASE_SOURCE, base, keyring), "13.6")
            self.assertEqual(verify_inrelease(SECURITY_SOURCE, security, keyring), "13")
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][:2], ["gpgv", "--keyring"])
            self.assertTrue(call.kwargs["check"])

    @mock.patch("subprocess.run")
    def test_rejects_wrong_duplicate_and_security_metadata(self, _: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "InRelease"
            keyring = Path(directory) / "keyring"
            for document, reason in (
                (self.SECURITY_HEADER + "Codename: trixie-security\n", "duplicate"),
                (self.SECURITY_HEADER.replace("trixie-security", "bookworm-security"), "Codename"),
                (self.SECURITY_HEADER.replace("Version: 13\n", ""), "Version"),
                (self.SECURITY_HEADER.replace("Version: 13\n", "Version: 13.0\n"), "Version"),
                (self.SECURITY_HEADER.replace("Suite: stable-security", "Suite: testing-security"), "Suite"),
                (self.SECURITY_HEADER.replace(" arm64 riscv64", " arm64"), "Architectures"),
                (self.SECURITY_HEADER.replace("updates/main", "main"), "Components"),
            ):
                path.write_text(document)
                with self.assertRaisesRegex(ValueError, reason):
                    verify_inrelease(SECURITY_SOURCE, path, keyring)

    def test_drift_and_unknown_or_ambiguous_list_ownership_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            retained = Path(directory) / "retained"
            current = Path(directory) / "current"
            retained.write_bytes(b"one")
            current.write_bytes(b"two")
            with self.assertRaisesRegex(ValueError, "changed"):
                require_unchanged(retained, current, "security")
        self.assertEqual(
            source_for_apt_list("security_dists_trixie-security_main_binary-riscv64_Packages.xz"),
            SECURITY_SOURCE,
        )
        with self.assertRaisesRegex(ValueError, "0 source owners"):
            source_for_apt_list("unknown_Packages")
        with self.assertRaisesRegex(ValueError, "2 source owners"):
            source_for_apt_list(
                "x_dists_trixie_main_binary-riscv64_Packages_"
                "dists_trixie-security_main_binary-riscv64_Packages"
            )

    def test_packages_must_match_exactly_one_hash_size_path_row(self) -> None:
        index = b"Package: firefox-esr\nArchitecture: riscv64\n"
        digest = hashlib.sha256(index).hexdigest()
        row = f" {digest} {len(index)} main/binary-riscv64/Packages\n"
        authenticate_packages(index, "main/binary-riscv64/Packages", ("SHA256:\n" + row).encode())
        for release in (b"SHA256:\n", ("SHA256:\n" + row + row).encode()):
            with self.assertRaisesRegex(ValueError, "uniquely authenticated"):
                authenticate_packages(index, "main/binary-riscv64/Packages", release)

    def test_manifest_fragment_is_sorted_complete_and_content_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {"base": root / "base", "security": root / "security"}
            files["base"].write_bytes(b"base signed")
            files["security"].write_bytes(b"security signed")
            rows = signed_sources_manifest(tuple(reversed(M5_SOURCES)), files)
        self.assertEqual([row["role"] for row in rows], ["base", "security"])
        self.assertEqual(rows[1]["suite"], "trixie-security")
        self.assertEqual(rows[1]["inrelease_sha256"], hashlib.sha256(b"security signed").hexdigest())


if __name__ == "__main__":
    unittest.main()
