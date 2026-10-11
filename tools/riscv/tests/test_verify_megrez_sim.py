from __future__ import annotations

from pathlib import Path
import subprocess
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPOSITORY_ROOT / "tools/riscv/verify_megrez_sim.sh"


class VerifyMegrezSimTests(unittest.TestCase):
    def test_wrapper_delegates_to_canonical_make_target(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("make --no-print-directory run_riscv_uboot_booti", script)
        self.assertNotIn("qemu_uboot_booti.py run", script)

    def test_wrapper_preserves_megrez_defaults(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("megrez-sv48-svade-fast", script)
        self.assertIn("current-megrez", script)
        self.assertIn("u-boot-build-megrez", script)

    def test_wrapper_is_strict_shell(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", script)

    def test_wrapper_has_valid_shell_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT)],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
