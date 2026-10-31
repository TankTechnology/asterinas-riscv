#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Syntax and ownership guards for the active Debian tooling surface."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEBIAN_ROOT = REPO_ROOT / "tools/riscv/debian"
REMOVED_MODULE_NAMES = ("megrez", "physical_graphics", "desktop_drm")


def active_files(suffix: str) -> list[Path]:
    return sorted(
        path
        for path in DEBIAN_ROOT.rglob(f"*{suffix}")
        if "__pycache__" not in path.parts
    )


class PythonSurfaceTests(unittest.TestCase):
    def test_every_active_python_module_compiles(self) -> None:
        modules = active_files(".py")
        self.assertGreaterEqual(len(modules), 15)

        for path in modules:
            with self.subTest(path=path.relative_to(REPO_ROOT)):
                compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_active_modules_do_not_import_removed_verticals(self) -> None:
        for path in active_files(".py"):
            text = path.read_text(encoding="utf-8")
            for removed in REMOVED_MODULE_NAMES:
                with self.subTest(path=path.relative_to(REPO_ROOT), module=removed):
                    self.assertNotIn(f"import {removed}", text)
                    self.assertNotIn(f"from {removed}", text)


class ShellSurfaceTests(unittest.TestCase):
    def test_every_active_shell_script_parses(self) -> None:
        scripts = active_files(".sh")
        self.assertGreaterEqual(len(scripts), 10)

        for path in scripts:
            with self.subTest(path=path.relative_to(REPO_ROOT)):
                result = subprocess.run(
                    ["bash", "-n", str(path)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


class MakefileSurfaceTests(unittest.TestCase):
    def test_host_runner_targets_use_the_repository_runner(self) -> None:
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

        self.assertIn("test_riscv_host:", makefile)
        self.assertIn("tools/riscv/host_test_runner.py", makefile)
        self.assertIn("test_riscv_host_list:", makefile)
        self.assertIn('PYTHONPATH="$(CURDIR)"', makefile)
        self.assertIn("test_riscv_debian_product:", makefile)
        self.assertIn("test_riscv_debian_full:", makefile)
        self.assertIn("tools/riscv/debian/product_gate.py", makefile)
        self.assertIn("test_riscv_debian_desktop:", makefile)
        self.assertIn("tools/riscv/debian/desktop_gate.py", makefile)


if __name__ == "__main__":
    unittest.main()
