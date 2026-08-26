# SPDX-License-Identifier: MPL-2.0

import tempfile
import unittest
from pathlib import Path

from tools.riscv.debian.rootfs.browser_m5 import (
    BROWSER_PACKAGE,
    UNSUPPORTED_BROWSER_PACKAGES,
    validate_probe_assets,
)


ROOTFS_DIR = Path(__file__).parents[1] / "debian/rootfs"


class DebianBrowserM5AdmissionTests(unittest.TestCase):
    def test_selects_prebuilt_riscv64_firefox_not_chromium(self) -> None:
        self.assertEqual(BROWSER_PACKAGE, "firefox-esr")
        self.assertIn("chromium", UNSUPPORTED_BROWSER_PACKAGES)

    def test_offline_silent_video_probe_is_self_contained(self) -> None:
        video = validate_probe_assets(
            ROOTFS_DIR / "browser_m5_probe.html",
            ROOTFS_DIR / "browser_m5.webm.base64",
        )
        self.assertLess(len(video), 1024)

    def test_probe_rejects_a_network_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            html = Path(directory) / "probe.html"
            source = (ROOTFS_DIR / "browser_m5_probe.html").read_text()
            html.write_text(source.replace("browser-m5.webm", "https://example/video"))
            with self.assertRaisesRegex(ValueError, "forbidden dependency"):
                validate_probe_assets(
                    html,
                    ROOTFS_DIR / "browser_m5.webm.base64",
                )


if __name__ == "__main__":
    unittest.main()
