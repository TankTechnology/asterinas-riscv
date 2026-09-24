# SPDX-License-Identifier: MPL-2.0

"""Contract tests for the controlled video sampling comparison."""

import unittest

from tools.riscv.debian.rootfs.video_filter_probe import render_page


class VideoFilterProbeTest(unittest.TestCase):
    def test_only_sampling_style_changes_between_native_size_variants(self):
        baseline = render_page("baselinea", "large", "auto")
        candidate = render_page("candidate", "large", "crisp")
        self.assertIn(b'width="1280" height="720"', baseline)
        self.assertIn(b'width="1280" height="720"', candidate)
        self.assertIn(b'image-rendering: auto', baseline)
        self.assertIn(b'image-rendering: crisp-edges', candidate)
        self.assertEqual(
            baseline.replace(b"baselinea", b"candidate").replace(
                b"image-rendering: auto", b"image-rendering: crisp-edges"
            ),
            candidate,
        )

    def test_rejects_noncontract_runs(self):
        for run, size, sampling in (
            ("../escape", "large", "auto"),
            ("valid", "720p", "auto"),
            ("valid", "large", "bilinear"),
        ):
            with self.subTest(run=run, size=size, sampling=sampling):
                with self.assertRaises(ValueError):
                    render_page(run, size, sampling)


if __name__ == "__main__":
    unittest.main()
