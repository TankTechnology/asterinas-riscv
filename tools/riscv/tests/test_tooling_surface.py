#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Keep the RISC-V tooling surface from growing back."""

from __future__ import annotations

import json
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[3]
TOOLS = REPO_ROOT / "tools/riscv"
SURFACE = TOOLS / "tooling_surface.json"


class ToolingSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.limit = json.loads(SURFACE.read_text(encoding="utf-8"))

    def _production_files(self, suffix: str) -> list[Path]:
        return [
            path
            for path in TOOLS.rglob(f"*{suffix}")
            if path.is_file()
            and "tests" not in path.parts
            and "archive" not in path.parts
            and "__pycache__" not in path.parts
        ]

    def test_production_file_counts_only_shrink(self) -> None:
        self.assertEqual(self.limit["schema_version"], 1)
        self.assertLessEqual(
            len(self._production_files(".py")),
            self.limit["max_production_python"],
        )
        self.assertLessEqual(
            len(self._production_files(".sh")),
            self.limit["max_production_shell"],
        )

    def test_top_level_shell_scripts_are_allowlisted(self) -> None:
        observed = {
            path.name for path in TOOLS.glob("*.sh") if path.is_file()
        }
        self.assertLessEqual(
            observed, set(self.limit["top_level_shell"]), observed
        )
        self.assertLessEqual(
            len(observed), self.limit["max_top_level_shell"]
        )

    def test_allowlist_names_still_exist_or_are_documented_for_removal(self) -> None:
        for name in self.limit["top_level_shell"]:
            self.assertTrue(
                (TOOLS / name).is_file(),
                f"remove {name} from tooling_surface.json after deleting it",
            )
