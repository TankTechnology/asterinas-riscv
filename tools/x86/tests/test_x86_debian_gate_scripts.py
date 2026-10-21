# SPDX-License-Identifier: MPL-2.0

"""Inventory test for the x86 Debian filesystem gates.

Every gate script in ``tools/x86/debian`` must exist, be executable and
answer ``--help`` with a usage block.  This keeps newly added gates (for
example the extent-depth and EA-inode ones) inside the default tooling test
run instead of letting them rot.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
GATE_DIRECTORY = REPOSITORY_ROOT / "tools/x86/debian"


class GateScriptInventoryTests(unittest.TestCase):
    def test_gate_scripts_exist_and_print_usage(self) -> None:
        scripts = sorted(GATE_DIRECTORY.glob("fs_*.sh"))
        self.assertGreaterEqual(len(scripts), 10, f"no gates found in {GATE_DIRECTORY}")

        for script in scripts:
            with self.subTest(script=script.name):
                self.assertTrue(script.is_file())
                self.assertTrue(os.access(script, os.X_OK), "gate is not executable")
                result = subprocess.run(
                    ["/bin/bash", str(script), "--help"],
                    cwd=REPOSITORY_ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    f"{script.name} --help failed: {result.stderr.strip()}",
                )
                self.assertIn("Usage:", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
