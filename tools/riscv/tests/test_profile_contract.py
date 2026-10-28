#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""One source of truth for rootfs profile identity and size."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest

from tools.riscv.debian.rootfs import profiles
from tools.riscv import megrez_debug_contract as debug_contract
from tools.riscv import megrez_debian_shell_board as shell_board
from tools.riscv import megrez_debian_shell_contract as shell_contract


REPO_ROOT = Path(__file__).resolve().parents[3]
BUILD_ROOTFS = REPO_ROOT / "tools/riscv/debian/rootfs/build_rootfs.sh"


class ProfileRegistryTests(unittest.TestCase):
    def test_registry_is_public_and_unique(self) -> None:
        names = profiles.PROFILE_NAMES
        self.assertGreaterEqual(len(names), 10)
        self.assertEqual(len(names), len(set(names)))

        selected = [profiles.get_profile(name) for name in names]
        self.assertEqual(
            len({profile.root_uuid for profile in selected}), len(selected)
        )
        self.assertEqual(
            len({(profile.root_label, profile.root_uuid) for profile in selected}),
            len(selected),
        )
        self.assertTrue(all(profile.root_size_bytes > 0 for profile in selected))

    def test_unknown_profile_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown rootfs profile"):
            profiles.get_profile("not-a-profile")

    def test_cli_output_is_the_builder_contract(self) -> None:
        environment = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        for name in profiles.PROFILE_NAMES:
            with self.subTest(profile=name):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "tools.riscv.debian.rootfs.profiles",
                        "--profile",
                        name,
                    ],
                    cwd=REPO_ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                fields = result.stdout.splitlines()
                profile = profiles.get_profile(name)
                self.assertEqual(
                    fields[:5],
                    [
                        profile.root_label,
                        profile.root_uuid,
                        str(profile.root_size_bytes),
                        profile.filesystem_type,
                        "journal" if profile.journal else "nojournal",
                    ],
                )
                self.assertEqual(fields[5:], list(profile.requested_packages))


class ProfileConsumersTests(unittest.TestCase):
    def test_builder_uses_profile_data_for_every_profile(self) -> None:
        source = BUILD_ROOTFS.read_text(encoding="utf-8")

        self.assertIn("tools.riscv.debian.rootfs.profiles --profile", source)
        self.assertNotIn('if [[ "$PROFILE" == minimal ]]; then', source)
        for duplicate in (
            'ROOT_LABEL="ASTER_DEBIANROOT"',
            'ROOT_UUID="7b7ad749-77d0-4e59-89e4-e117244a70aa"',
            'ROOT_SIZE_BYTES="1073741824"',
        ):
            self.assertNotIn(duplicate, source)

    def test_debug_contract_sizes_come_from_profiles(self) -> None:
        expected = frozenset(
            profiles.get_profile(name).root_size_bytes
            for name in profiles.PROFILE_NAMES
        )

        self.assertEqual(debug_contract.ROOT_IMAGE_SIZES, expected)
        self.assertEqual(
            debug_contract.ROOT_IMAGE_BYTES,
            profiles.get_profile("minimal").root_size_bytes,
        )
        self.assertEqual(
            debug_contract.BROWSER_ROOT_IMAGE_BYTES,
            profiles.get_profile("browser-web").root_size_bytes,
        )

    def test_shell_contracts_use_the_minimal_profile(self) -> None:
        expected = profiles.get_profile("minimal").root_size_bytes

        self.assertEqual(shell_contract._ROOT_IMAGE_SIZE_BYTES, expected)
        self.assertEqual(shell_board._ROOT_IMAGE_SIZE_BYTES, expected)
