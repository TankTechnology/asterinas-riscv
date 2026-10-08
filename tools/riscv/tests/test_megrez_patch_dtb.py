"""Unit tests for safe Megrez DWC3 host-mode selection."""

import unittest
from pathlib import Path
from unittest import mock

import megrez_patch_dtb as patch_dtb


class HostModeTests(unittest.TestCase):
    def test_explicit_host_mode_is_accepted_without_mmio_fallback(self):
        with mock.patch.object(patch_dtb, "run", return_value="host") as run:
            self.assertTrue(
                patch_dtb.is_host_mode(Path("board.dtb"), "/soc/usb@50480000")
            )
        run.assert_called_once()

    def test_explicit_non_host_modes_are_rejected_without_mmio_fallback(self):
        for mode in ("peripheral", "otg", "device", ""):
            with (
                self.subTest(mode=mode),
                mock.patch.object(patch_dtb, "run", return_value=mode) as run,
            ):
                self.assertFalse(
                    patch_dtb.is_host_mode(Path("board.dtb"), "/soc/usb@50480000")
                )
            run.assert_called_once()

    def test_absent_mode_uses_expected_mmio_fallback(self):
        with mock.patch.object(
            patch_dtb,
            "run",
            side_effect=[RuntimeError("missing dr_mode"), "50480000 10000"],
        ) as run:
            self.assertTrue(
                patch_dtb.is_host_mode(Path("board.dtb"), "/soc/usb@50480000")
            )
        self.assertEqual(run.call_count, 2)

    def test_absent_mode_rejects_unexpected_mmio(self):
        with mock.patch.object(
            patch_dtb,
            "run",
            side_effect=[RuntimeError("missing dr_mode"), "deadbeef 10000"],
        ):
            self.assertFalse(
                patch_dtb.is_host_mode(Path("board.dtb"), "/soc/usb@deadbeef")
            )


if __name__ == "__main__":
    unittest.main()
