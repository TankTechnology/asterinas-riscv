#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Contract tests for the ext4 xfstests profile and RISC-V device wiring."""

from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
XFSTESTS = REPO_ROOT / "test/initramfs/src/conformance/xfstests"
EXT4 = XFSTESTS / "ext4"


def _list_ids(name: str) -> list[str]:
    return [
        line.strip()
        for line in (EXT4 / "run_list" / name).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


class Ext4ConfigTests(unittest.TestCase):
    def test_build_config_uses_the_compatibility_feature_set(self) -> None:
        text = (EXT4 / "config/build_config.mk").read_text(encoding="utf-8")

        self.assertIn("XFSTESTS_NEEDS_BLOCK_DEVICES := true", text)
        self.assertIn("XFSTESTS_MKFS :=", text)
        for feature in (
            "^metadata_csum",
            "^64bit",
            "^flex_bg",
            "^orphan_file",
            "^dir_nlink",
            "^extra_isize",
        ):
            self.assertIn(feature, text)

    def test_runtime_config_mounts_ext4_without_acl(self) -> None:
        text = (EXT4 / "config/xfstests.config").read_text(encoding="utf-8")

        self.assertIn("export FSTYP=ext4", text)
        self.assertIn("XFSTESTS_TEST_DEV:-/dev/vdd", text)
        self.assertIn("XFSTESTS_SCRATCH_DEV:-/dev/vde", text)
        self.assertIn('MOUNT_OPTIONS="-o noacl"', text)
        self.assertIn("^metadata_csum", text)

    def test_run_lists_are_well_formed_and_short_is_a_subset(self) -> None:
        pattern = re.compile(r"^(?:generic|ext4)/[0-9]{3}$")
        short = _list_ids("short.list")
        full = _list_ids("full.list")
        blocked = _list_ids("block.list")

        self.assertTrue(short)
        self.assertTrue(blocked)
        for test_id in short + full + blocked:
            self.assertRegex(test_id, pattern)
        self.assertLessEqual(set(short), set(full))
        self.assertEqual(len(short), len(set(short)))
        self.assertEqual(len(full), len(set(full)))

    def test_prepare_script_has_valid_bash_syntax(self) -> None:
        subprocess.run(
            ["bash", "-n", str(EXT4 / "prepare.sh")],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )


class CrossBuildTests(unittest.TestCase):
    def test_cross_build_replaces_host_bash_shebangs(self) -> None:
        text = (REPO_ROOT / "test/initramfs/nix/conformance/xfstests.nix").read_text(
            encoding="utf-8"
        )

        self.assertIn("src/dmerror", text)
        self.assertIn("ltp/rwtest.sh", text)
        self.assertIn("pkgs.bash", text)
        self.assertIn("readlink -f", text)


class RiscvQemuTests(unittest.TestCase):
    def _devices(self, enabled: bool) -> list[str]:
        env = {
            **os.environ,
            "INITRAMFS": "on",
            "CONSOLE": "ttyS0",
            "XFSTESTS_NEEDS_BLOCK_DEVICES": "true" if enabled else "false",
        }
        result = subprocess.run(
            ["bash", "tools/qemu_args.sh", "riscv"],
            cwd=REPO_ROOT,
            env=env,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return re.findall(r"-device (virtio-blk-device,drive=x[0-9])", result.stdout)

    def test_xfstests_devices_precede_x2_for_reverse_discovery(self) -> None:
        self.assertEqual(
            self._devices(True),
            [
                "virtio-blk-device,drive=x4",
                "virtio-blk-device,drive=x3",
                "virtio-blk-device,drive=x2",
                "virtio-blk-device,drive=x1",
                "virtio-blk-device,drive=x0",
            ],
        )

    def test_xfstests_devices_are_not_attached_by_default(self) -> None:
        self.assertEqual(
            self._devices(False),
            [
                "virtio-blk-device,drive=x2",
                "virtio-blk-device,drive=x1",
                "virtio-blk-device,drive=x0",
            ],
        )


if __name__ == "__main__":
    unittest.main()
