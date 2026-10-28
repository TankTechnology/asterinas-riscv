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

    def test_top_level_shell_scripts_are_allowlisted(self) -> None:
        observed = {
            path.name for path in TOOLS.glob("*.sh") if path.is_file()
        }
        self.assertLessEqual(
            observed, set(self.limit["top_level_shell"]), observed
        )

    def test_allowlist_names_still_exist_or_are_documented_for_removal(self) -> None:
        for name in self.limit["top_level_shell"]:
            self.assertTrue(
                (TOOLS / name).is_file(),
                f"remove {name} from tooling_surface.json after deleting it",
            )
