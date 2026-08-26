# SPDX-License-Identifier: MPL-2.0

import base64
import shutil
import tempfile
import unittest
from pathlib import Path

from tools.riscv.debian.rootfs.browser_m5 import (
    probe_video_file,
    validate_probe_assets,
)


ROOTFS_DIR = Path(__file__).parents[1] / "debian/rootfs"


class DebianBrowserM5AdmissionTests(unittest.TestCase):
    def test_offline_silent_video_probe_is_self_contained(self) -> None:
        video = validate_probe_assets(
            ROOTFS_DIR / "browser_m5_probe.html",
            ROOTFS_DIR / "browser_m5.webm.base64",
        )
        self.assertLess(len(video), 1024)

    @unittest.skipUnless(shutil.which("ffprobe") and shutil.which("ffmpeg"), "FFmpeg tools unavailable")
    def test_fixture_is_one_fully_decodable_silent_vp8_stream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video_path = Path(directory) / "browser-m5.webm"
            encoded = b"".join(
                (ROOTFS_DIR / "browser_m5.webm.base64").read_bytes().splitlines()
            )
            video_path.write_bytes(base64.b64decode(encoded, validate=True))
            metadata = probe_video_file(video_path)
        self.assertEqual(metadata["streams"][0]["codec_name"], "vp8")

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
