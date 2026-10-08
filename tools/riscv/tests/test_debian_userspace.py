#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Contracts for the real Debian userspace workload, including negative gates."""

import subprocess
import unittest
from pathlib import Path

from tools.riscv.debian.rootfs.profiles import get_profile
from tools.riscv.debian.rootfs.systemd_ext4_gate import classify_systemd_ext4
from tools.riscv.tests.test_debian_systemd_ext4_gate import _transcript


class UserspaceTests(unittest.TestCase):
    def test_packages_and_filesystem(self):
        profile = get_profile("systemd-ext4-m6-userspace")
        required = {"openssh-server", "cron", "anacron", "rsyslog", "python3", "perl",
                    "gcc", "g++", "rustc", "cargo", "curl", "git", "tar", "gzip",
                    "findutils", "procps", "iproute2", "nginx", "sqlite3"}
        self.assertTrue(required <= set(profile.requested_packages))
        self.assertEqual(len(profile.requested_packages), len(set(profile.requested_packages)))
        self.assertEqual(profile.filesystem_type, "ext4")
        self.assertTrue(profile.journal)
        self.assertGreaterEqual(profile.root_size_bytes, 2 * 1024**3)
        script = Path(__file__).resolve().parents[1] / "debian/rootfs/build_rootfs.sh"
        result = subprocess.run(["bash", str(script), "--profile", profile.name,
                                 "--print-packages"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), list(profile.requested_packages))

    def test_userspace_gate_requires_runtime_evidence(self):
        base = _transcript()
        def classify(transcript):
            return classify_systemd_ext4(transcript, expected_debian_release="13.6", userspace=True)
        self.assertFalse(classify(base).passed)
        markers = (
            "DEBIAN_USERSPACE_PROGRESS step=toolchains-services-done\n",
            "DEBIAN_USERSPACE_PROGRESS step=persistence-services-done\n",
            "DEBIAN_USERSPACE_PASS boot=2 services=1 toolchains=1 database=1 persistence=1\n",
        )
        valid = base.replace("DEBIAN_EXT4_READY boot=1", markers[0] + "DEBIAN_EXT4_READY boot=1")
        valid = valid.replace("DEBIAN_EXT4_READY boot=2", markers[1] + markers[2] + "DEBIAN_EXT4_READY boot=2")
        self.assertTrue(classify(valid).passed)
        for marker in markers:
            self.assertFalse(classify(valid.replace(marker, "")).passed)
            self.assertFalse(classify(valid.replace(marker, marker * 2)).passed)
        self.assertFalse(classify(valid.replace(markers[2], "") + markers[2]).passed)


if __name__ == "__main__":
    unittest.main()
