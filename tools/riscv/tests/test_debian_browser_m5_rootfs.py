#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.riscv.debian.rootfs import browser_m5_rootfs_check


class DebianBrowserM5RootfsCheckerTests(unittest.TestCase):
    def _make_root(self, *, netsurf: bool = False) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        paths = (
            "usr/lib/firefox-esr/firefox-esr",
            "usr/share/asterinas/browser-m5/index.html",
            "usr/share/asterinas/browser-m5/browser-m5.webm",
            "usr/lib/asterinas/browser-m5-marionette-gate",
            "usr/lib/asterinas/browser-m5-firefox",
            "usr/lib/asterinas/browser-m5-window-observer",
            "usr/lib/asterinas/browser-m5-network-observer",
            "usr/lib/asterinas/browser-m5-startup-evidence",
            "etc/systemd/system/asterinas-browser-m5.service",
            "etc/systemd/system/asterinas-browser-m5-startup.service",
        )
        for relative in paths:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("placeholder\n")
            path.chmod(0o755)
        launcher = root / "usr/bin/firefox-esr"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.symlink_to("../lib/firefox-esr/firefox-esr")
        status = [
            "Package: firefox-esr",
            "Status: install ok installed",
            "Architecture: riscv64",
        ]
        if netsurf:
            status.extend(("", "Package: netsurf-gtk", "Status: install ok installed"))
        (root / "var/lib/dpkg").mkdir(parents=True)
        (root / "var/lib/dpkg/status").write_text("\n".join(status) + "\n")
        (root / "usr/share/asterinas/browser-m5/index.html").write_text(
            "<html>local</html>\n"
        )
        (root / "usr/share/asterinas/browser-m5/browser-m5.webm").write_bytes(
            browser_m5_rootfs_check.WEBM_EBML_HEADER + b"fixture"
        )
        (root / "usr/lib/asterinas/browser-m5-firefox").write_text(
            "#!/bin/sh\nexec firefox-esr --marionette --profile /tmp/profile\n"
        )
        (root / "etc/systemd/system/asterinas-browser-m5.service").write_text(
            "[Service]\n"
            "User=asterinas\n"
            "PrivateNetwork=yes\n"
            "NoNewPrivileges=yes\n"
            "ExecStart=/usr/lib/asterinas/browser-m5-firefox\n"
        )
        (root / "etc/systemd/system/asterinas-browser-m5-startup.service").write_text(
            "[Service]\nExecStart=/usr/lib/asterinas/browser-m5-startup-evidence\n"
        )
        return root

    def test_accepts_complete_firefox_stage_root(self) -> None:
        root = self._make_root()
        with mock.patch.object(browser_m5_rootfs_check, "riscv_elf"):
            self.assertEqual(
                browser_m5_rootfs_check.check_root(root),
                "FIREFOX_M5_ROOTFS_PASS firefox=riscv64 sandbox=normal assets=local",
            )

    def test_rejects_missing_firefox_package(self) -> None:
        root = self._make_root()
        (root / "var/lib/dpkg/status").write_text(
            "Package: firefox-esr\nStatus: deinstall ok config-files\n"
        )
        with self.assertRaisesRegex(
            browser_m5_rootfs_check.CheckFailure, "firefox-esr"
        ):
            browser_m5_rootfs_check.check_root(root)

    def test_rejects_netsurf_in_firefox_profile(self) -> None:
        root = self._make_root(netsurf=True)
        with self.assertRaisesRegex(
            browser_m5_rootfs_check.CheckFailure, "netsurf-gtk"
        ):
            browser_m5_rootfs_check.check_root(root)

    def test_rejects_non_riscv_firefox_elf(self) -> None:
        root = self._make_root()
        with mock.patch.object(
            browser_m5_rootfs_check,
            "riscv_elf",
            side_effect=browser_m5_rootfs_check.CheckFailure("not RISC-V ELF"),
        ):
            with self.assertRaisesRegex(browser_m5_rootfs_check.CheckFailure, "RISC-V"):
                browser_m5_rootfs_check.check_root(root)

    def test_rejects_sandbox_bypass_in_launcher(self) -> None:
        root = self._make_root()
        (root / "usr/lib/asterinas/browser-m5-firefox").write_text(
            "#!/bin/sh\nexec firefox-esr --no-sandbox --marionette\n"
        )
        with mock.patch.object(browser_m5_rootfs_check, "riscv_elf"):
            with self.assertRaisesRegex(
                browser_m5_rootfs_check.CheckFailure, "no-sandbox"
            ):
                browser_m5_rootfs_check.check_root(root)

    def test_rejects_oversized_local_probe_asset(self) -> None:
        root = self._make_root()
        (root / "usr/share/asterinas/browser-m5/browser-m5.webm").write_bytes(
            b"x" * (browser_m5_rootfs_check.MAX_PROBE_ASSET_BYTES + 1)
        )
        with mock.patch.object(browser_m5_rootfs_check, "riscv_elf"):
            with self.assertRaisesRegex(browser_m5_rootfs_check.CheckFailure, "asset"):
                browser_m5_rootfs_check.check_root(root)

    def test_builder_runs_checker_before_publishing_browser_m5(self) -> None:
        builder = (
            Path(__file__).resolve().parents[3]
            / "tools/riscv/debian/rootfs/build_rootfs.sh"
        )
        source = builder.read_text(encoding="utf-8")
        self.assertIn("browser_m5_rootfs_check.py", source)
        self.assertIn("browser-m5-rootfs-static.log", source)
        self.assertIn(
            "FIREFOX_M5_ROOTFS_PASS firefox=riscv64 sandbox=normal assets=local",
            source,
        )


if __name__ == "__main__":
    unittest.main()
