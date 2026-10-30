#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the frozen Debian rootfs profile registry."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from tools.riscv.debian.rootfs import profiles


class ProfileRegistryTests(unittest.TestCase):
    def test_only_the_supported_profiles_exist(self) -> None:
        self.assertEqual(profiles.PROFILE_NAMES, ("minimal", "browser-web"))

    def test_identities_are_unique_and_complete(self) -> None:
        labels = {profile.root_label for profile in profiles._PROFILES.values()}
        uuids = {profile.root_uuid for profile in profiles._PROFILES.values()}

        self.assertEqual(len(labels), len(profiles.PROFILE_NAMES))
        self.assertEqual(len(uuids), len(profiles.PROFILE_NAMES))
        for profile in profiles._PROFILES.values():
            with self.subTest(profile=profile.name):
                self.assertTrue(profile.requested_packages)
                self.assertIn("base-files", profile.identity_packages)
                self.assertEqual(profile.filesystem_type, "ext2")
                self.assertFalse(profile.journal)
                self.assertGreaterEqual(profile.root_size_bytes, 1024**3)
                self.assertEqual(
                    len(profile.requested_packages),
                    len(set(profile.requested_packages)),
                )

    def test_minimal_identity_is_stable(self) -> None:
        profile = profiles.get_profile("minimal")

        self.assertEqual(profile.root_label, "ASTER_DEBIANROOT")
        self.assertEqual(profile.root_uuid, "7b7ad749-77d0-4e59-89e4-e117244a70aa")
        self.assertEqual(profile.root_size_bytes, 1024**3)
        self.assertEqual(profile.schema_version, 1)
        self.assertEqual(
            profile.requested_packages,
            ("bash", "ca-certificates", "coreutils", "procps", "util-linux"),
        )

    def test_browser_profile_keeps_the_approved_package_set(self) -> None:
        profile = profiles.get_profile("browser-web")

        self.assertEqual(profile.root_label, "ASTER_BROWSERWEB")
        self.assertEqual(profile.root_uuid, "c2ce5134-afcc-4d7c-b71e-7e6d4a8f2b10")
        self.assertEqual(profile.root_size_bytes, 3 * 1024**3)
        self.assertEqual(profile.schema_version, 7)
        for package in (
            "firefox-esr",
            "xserver-xorg-core",
            "xserver-xorg-video-fbdev",
            "libavcodec-extra61",
            "build-essential",
        ):
            with self.subTest(package=package):
                self.assertIn(package, profile.requested_packages)
        for package in profiles.BROWSER_WEB_DESKTOP_PACKAGES:
            with self.subTest(desktop_package=package):
                self.assertIn(package, profile.requested_packages)
                self.assertIn(package, profile.identity_packages)

    def test_unknown_profile_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown rootfs profile"):
            profiles.get_profile("desktop-drm")


class ProfileCliTests(unittest.TestCase):
    def test_cli_prints_identity_then_packages(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(profiles.main(["--profile", "minimal"]), 0)

        lines = output.getvalue().splitlines()
        profile = profiles.get_profile("minimal")
        self.assertEqual(
            lines[:5],
            [
                profile.root_label,
                profile.root_uuid,
                str(profile.root_size_bytes),
                profile.filesystem_type,
                "journal" if profile.journal else "nojournal",
            ],
        )
        self.assertEqual(tuple(lines[5:]), profile.requested_packages)

    def test_cli_rejects_unknown_profiles(self) -> None:
        with (
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            profiles.main(["--profile", "missing"])

        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
