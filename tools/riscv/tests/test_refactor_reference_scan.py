import json
import tempfile
import unittest
from pathlib import Path

from tools.riscv.refactor_reference_scan import scan, validate_manifest


class RefactorReferenceScanTests(unittest.TestCase):
    def test_code_reference_is_active_and_document_reference_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".git").mkdir()
            (repo / "tools/riscv/debian/rootfs").mkdir(parents=True)
            (repo / "tools/riscv/tests").mkdir(parents=True)
            (repo / "Makefile").write_text("", encoding="utf-8")
            candidate = repo / "tools/riscv/debian/rootfs/old_gate.py"
            candidate.write_text("print('old')\n", encoding="utf-8")
            (repo / "tools/riscv/tests/use.py").write_text(
                "import old_gate\n", encoding="utf-8"
            )
            (repo / "docs").mkdir()
            (repo / "docs/history.md").write_text("old_gate.py\n", encoding="utf-8")
            # The scanner asks git for tracked files; use the repository checkout
            # only for the command and make the candidate lookup independent of it.
            import subprocess

            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            result = scan(repo, Path("tools/riscv/debian/rootfs"), Path("tools/riscv/tests"), Path("Makefile"))
            found = next(item for item in result if item.path.endswith("old_gate.py"))
            self.assertIn("tools/riscv/tests/use.py", [ref.path for ref in found.active_references])
            self.assertIn("docs/history.md", [ref.path for ref in found.references])

    def test_manifest_rejects_protected_and_unreviewed_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs/porting/evidence").mkdir(parents=True)
            protected = repo / "docs/porting/evidence/record.md"
            protected.write_text("x\n", encoding="utf-8")
            candidate = repo / "old.py"
            candidate.write_text("x\n", encoding="utf-8")
            manifest = repo / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "keep": ["old.py"],
                        "delete": [
                            {"path": "old.py", "references_checked": False},
                            {"path": "docs/porting/evidence/record.md", "references_checked": True},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            errors = validate_manifest(repo, manifest)
            self.assertTrue(any("overlap" in error for error in errors))
            self.assertTrue(any("protected" in error for error in errors))
            self.assertTrue(any("unreviewed" in error for error in errors))

    def test_manifest_accepts_removed_entry_only_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manifest = repo / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "keep": [],
                        "delete": [
                            {
                                "path": "old.py",
                                "references_checked": True,
                                "state": "removed",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(validate_manifest(repo, manifest), [])
            (repo / "old.py").write_text("", encoding="utf-8")
            self.assertEqual(
                validate_manifest(repo, manifest),
                ["removed path still exists: old.py"],
            )


if __name__ == "__main__":
    unittest.main()
