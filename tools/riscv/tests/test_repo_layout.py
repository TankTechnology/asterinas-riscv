# SPDX-License-Identifier: MPL-2.0

"""Keep the active RISC-V tooling tree from regrowing monoliths."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.riscv import repo_layout_guard


class RepoLayoutGuardTests(unittest.TestCase):
    def _large_file(self, root: Path, relative: str, lines: int = 2_001) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n" * lines, encoding="utf-8")
        return path

    def test_current_tree_has_no_unapproved_oversized_file(self) -> None:
        self.assertEqual(repo_layout_guard.scan(), [])

    def test_new_oversized_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._large_file(root, "tools/riscv/new_gate.py")
            self.assertTrue(repo_layout_guard.scan(root, {}))

    def test_allowlisted_file_may_not_grow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            name = "tools/riscv/legacy.py"
            self._large_file(root, name, lines=2_002)
            violations = repo_layout_guard.scan(root, {name: 2_001})
            self.assertEqual(
                violations,
                [f"{name}: grew from 2001 to 2002 lines; only shrink the allowlist"],
            )

    def test_missing_allowlist_entry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tools/riscv").mkdir(parents=True)
            self.assertEqual(
                repo_layout_guard.scan(root, {"tools/riscv/missing.py": 2_001}),
                ["allowlisted file disappeared: tools/riscv/missing.py"],
            )

    def test_archive_is_outside_the_active_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._large_file(root, "tools/riscv/archive/old/huge.py")
            self.assertEqual(repo_layout_guard.scan(root, {}), [])


if __name__ == "__main__":
    unittest.main()
