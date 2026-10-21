# SPDX-License-Identifier: MPL-2.0

"""Inventory test for the x86 Debian checks.

Every check script in ``tools/x86/debian`` must exist, be executable, parse,
and answer ``--help`` with a usage block.  This keeps newly added gates (for
example the extent-depth, EA-inode, interactive-login and daily-use ones)
inside the default tooling test run instead of letting them rot.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
GATE_DIRECTORY = REPOSITORY_ROOT / "tools/x86/debian"


def check_scripts() -> list[Path]:
    return sorted(GATE_DIRECTORY.glob("*_check.sh"))


def run_bash(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", *arguments],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


class GateScriptInventoryTests(unittest.TestCase):
    def test_gate_scripts_exist_and_print_usage(self) -> None:
        scripts = check_scripts()
        self.assertGreaterEqual(len(scripts), 10, f"no gates found in {GATE_DIRECTORY}")

        for script in scripts:
            with self.subTest(script=script.name):
                self.assertTrue(script.is_file())
                self.assertTrue(os.access(script, os.X_OK), "gate is not executable")
                result = run_bash(str(script), "--help")
                self.assertEqual(
                    result.returncode,
                    0,
                    f"{script.name} --help failed: {result.stderr.strip()}",
                )
                self.assertIn("Usage:", result.stdout + result.stderr)

    def test_gate_scripts_parse(self) -> None:
        for script in check_scripts():
            with self.subTest(script=script.name):
                result = run_bash("-n", str(script))
                self.assertEqual(
                    result.returncode,
                    0,
                    f"{script.name} has a syntax error: {result.stderr.strip()}",
                )


if __name__ == "__main__":
    unittest.main()
