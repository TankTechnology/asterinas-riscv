#!/usr/bin/env python3

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
ATOMIC_MODE = ROOT / "ostd/src/task/atomic_mode.rs"


class AtomicModeDiagnosticTests(unittest.TestCase):
    def test_violation_path_does_not_allocate_while_panicking(self) -> None:
        source = ATOMIC_MODE.read_text()

        self.assertNotIn(
            "alloc::format!",
            source,
            "atomic-mode diagnostics must remain allocation-free during a panic",
        )
        self.assertIn(
            "ATOMIC_GUARD_TAKEN",
            source,
            "the guard location must be emitted before entering panic formatting",
        )

    def test_guard_factory_preserves_the_lock_callsite(self) -> None:
        source = (
            ROOT / "ostd/src/sync/guard.rs"
        ).read_text()

        self.assertGreaterEqual(source.count("#[track_caller]"), 6)
        self.assertIn(
            "#[track_caller]\n    fn guard()",
            source,
            "guard acquisition must retain its caller for atomic-mode diagnostics",
        )
        self.assertIn(
            "#[track_caller]\n    fn read_guard()",
            source,
            "read-guard acquisition must retain its caller for atomic-mode diagnostics",
        )


if __name__ == "__main__":
    unittest.main()
