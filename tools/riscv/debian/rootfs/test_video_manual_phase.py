#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Check the DRM counter format used to bracket one video playback."""

import sys
import types
import unittest

sys.modules.setdefault("browser_m5_marionette_gate", types.ModuleType("browser_m5_marionette_gate"))
sys.modules["browser_m5_marionette_gate"].Marionette = object

from video_manual_phase import parse_scanout_snapshot, scanout_delta


def snapshot(at_ns: int, count: int, read_ns: int) -> dict[str, int]:
    line = (
        f"at_ns={at_ns} phase_profile=1 successes={count} full_count=0 "
        f"full_bytes=0 full_total_ns=0 dirty_count={count} dirty_bytes=4096 "
        f"dirty_total_ns=2000 sampled_rows=1 sampled_bytes=4096 "
        f"read_ns={read_ns} write_and_sync_ns=1000 direct_copy_and_sync_ns=0"
    )
    return parse_scanout_snapshot(line)


class ScanoutSnapshotTests(unittest.TestCase):
    def test_phase_window_uses_counter_deltas(self):
        before = snapshot(100, 3, 200)
        after = snapshot(300, 5, 500)
        result = scanout_delta(before, after)
        self.assertEqual(result["window_ns"], 200)
        self.assertEqual(result["successes"], 2)
        self.assertEqual(result["dirty_count"], 2)
        self.assertEqual(result["read_ns"], 300)

    def test_missing_or_unavailable_kernel_stats_are_explicit(self):
        self.assertIsNone(parse_scanout_snapshot("unavailable\n"))
        self.assertIsNone(scanout_delta(None, snapshot(300, 5, 500)))

    def test_counter_reset_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "went backwards"):
            scanout_delta(snapshot(100, 5, 500), snapshot(300, 3, 200))


if __name__ == "__main__":
    unittest.main()
