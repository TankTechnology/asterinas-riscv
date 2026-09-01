#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.riscv.debian.rootfs.browser_m5_qemu_gate import (
    BROWSER_M5_MILESTONES,
    BROWSER_M5_QEMU_BOOTARGS,
    BROWSER_M5_QEMU_MILESTONES,
    BrowserM5QemuOperations,
    browser_m5_qemu_argv,
    classify_browser_m5_qemu,
)
from tools.riscv.debian.rootfs.desktop_m5_network_gate import (
    DESKTOP_M5_QEMU_MILESTONES,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MAKEFILE = REPOSITORY_ROOT / "Makefile"


class DebianBrowserM5QemuGateTests(unittest.TestCase):
    def test_classifier_requires_network_private_namespace_and_live_content(self) -> None:
        transcript = ("\n".join(BROWSER_M5_QEMU_MILESTONES) + "\n").encode()

        result = classify_browser_m5_qemu(
            transcript,
            expected_debian_release="13.6",
        )

        self.assertTrue(result.passed, result.reason)
        self.assertEqual(
            BROWSER_M5_QEMU_MILESTONES[: len(DESKTOP_M5_QEMU_MILESTONES)],
            DESKTOP_M5_QEMU_MILESTONES,
        )
        self.assertLess(
            BROWSER_M5_QEMU_MILESTONES.index(
                "DEBIAN_BROWSER_M5_NETNS firefox=private initial=distinct"
            ),
            BROWSER_M5_QEMU_MILESTONES.index(
                "DEBIAN_BROWSER_M5_CONTENT js=pass media=vp8-webm canplay=pass "
                "ended=pass network_mode=private-loopback source=file "
                "direct_nonloopback_ip=unavailable"
            ),
        )

    def test_classifier_fails_closed_for_every_guest_failure_domain(self) -> None:
        passing = ("\n".join(BROWSER_M5_QEMU_MILESTONES) + "\n").encode()
        cases = (
            (b"DEBIAN_NETWORK_M5_FAIL reason=qemu-https", "network guest failure"),
            (
                b"DEBIAN_BROWSER_M5_NETNS_FAIL reason=firefox-in-initial-netns",
                "browser namespace failure",
            ),
            (b"DEBIAN_BROWSER_M5_FAIL reason=browser-content", "desktop guest failure"),
        )
        for marker, reason in cases:
            with self.subTest(marker=marker):
                result = classify_browser_m5_qemu(
                    passing + marker + b"\n",
                    expected_debian_release="13.6",
                )
                self.assertFalse(result.passed)
                self.assertEqual(result.reason, reason)

    def test_classifier_rejects_missing_or_reordered_content(self) -> None:
        missing = ("\n".join(BROWSER_M5_QEMU_MILESTONES[:-1]) + "\n").encode()
        reordered = ("\n".join(reversed(BROWSER_M5_QEMU_MILESTONES)) + "\n").encode()

        self.assertIn(
            "missing desktop milestone",
            classify_browser_m5_qemu(
                missing,
                expected_debian_release="13.6",
            ).reason,
        )
        self.assertEqual(
            classify_browser_m5_qemu(
                reordered,
                expected_debian_release="13.6",
            ).reason,
            "desktop milestones out of order",
        )

    def test_adapter_binds_schema_six_to_one_slirp_nic_without_debug(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("u-boot", "boot.ext4", "root.ext2"):
                (root / name).write_bytes(name.encode())
            arguments = browser_m5_qemu_argv(
                uboot=root / "u-boot",
                boot_disk=root / "boot.ext4",
                root_disk=root / "root.ext2",
                monitor_socket=root / "monitor.sock",
            )

        self.assertEqual(BrowserM5QemuOperations.SCHEMA_VERSION, 6)
        self.assertEqual(BrowserM5QemuOperations.PROFILE_NAME, "browser-m5")
        self.assertEqual(BrowserM5QemuOperations.MILESTONES, BROWSER_M5_QEMU_MILESTONES)
        self.assertEqual(
            BrowserM5QemuOperations.ADDITIONAL_FAILURE_MARKERS,
            (
                b"DEBIAN_NETWORK_M5_FAIL reason=",
                b"DEBIAN_BROWSER_M5_NETNS_FAIL reason=",
            ),
        )
        self.assertNotIn("-nic", arguments)
        self.assertEqual(arguments.count("user,id=net0"), 1)
        self.assertEqual(arguments.count("virtio-net-device,netdev=net0"), 1)
        self.assertIn("asterinas.debian_network=qemu-slirp", BROWSER_M5_QEMU_BOOTARGS)
        self.assertNotIn("systemd.log_level", BROWSER_M5_QEMU_BOOTARGS)
        self.assertIn(
            "systemd.setenv=ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS=120",
            BROWSER_M5_QEMU_BOOTARGS,
        )

    def test_make_target_is_explicit_and_only_extends_host_budget(self) -> None:
        target = (
            MAKEFILE.read_text(encoding="utf-8")
            .split(".PHONY: test_riscv_debian_browser_m5_qemu_gate", 1)[1]
            .split(".PHONY:", 1)[0]
        )

        self.assertIn(
            "python3 -m tools.riscv.debian.rootfs.browser_m5_qemu_gate",
            target,
        )
        self.assertIn("DEBIAN_BROWSER_M5_QEMU_GATE_OUTPUT", target)
        self.assertIn("--boot-timeout 7200", target)
        self.assertNotIn("ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS", target)
        desktop_target = (
            MAKEFILE.read_text(encoding="utf-8")
            .split(".PHONY: test_riscv_debian_desktop_m5_qemu_gate", 1)[1]
            .split(".PHONY:", 1)[0]
        )
        self.assertIn("DEBIAN_DESKTOP_BOOT_TIMEOUT ?= 420", MAKEFILE.read_text())
        self.assertIn(
            '--boot-timeout "$(DEBIAN_DESKTOP_BOOT_TIMEOUT)"', desktop_target
        )
        self.assertNotIn("--boot-timeout 7200", desktop_target)
        variables = (
            "DEBIAN_KERNEL",
            "DEBIAN_UBOOT",
            "DEBIAN_DTB",
            "DEBIAN_STAGE1_INITRAMFS",
            "DEBIAN_ROOT_IMAGE",
            "DEBIAN_ROOT_MANIFEST",
            "DEBIAN_PACKAGES_LOCK",
            "DEBIAN_PACKAGE_CHECKSUMS",
            "DEBIAN_BROWSER_M5_QEMU_GATE_OUTPUT",
        )
        dry_run = subprocess.run(
            [
                "make",
                "--no-print-directory",
                "-n",
                "test_riscv_debian_browser_m5_qemu_gate",
                *(f"{name}=/tmp/{name.lower()}" for name in variables),
            ],
            cwd=REPOSITORY_ROOT,
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        self.assertIn(
            "python3 -m tools.riscv.debian.rootfs.browser_m5_qemu_gate",
            dry_run,
        )
        for name in variables:
            self.assertIn(f"/tmp/{name.lower()}", dry_run)

    def test_browser_milestones_end_in_formal_ready(self) -> None:
        self.assertEqual(
            BROWSER_M5_MILESTONES[-1],
            "DEBIAN_BROWSER_M5_READY user=asterinas display=:0",
        )


if __name__ == "__main__":
    unittest.main()
