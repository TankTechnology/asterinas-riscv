#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Contract tests for tools/check_secrets.sh."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "tools" / "check_secrets.sh"
GIT = shutil.which("git")


def _joined(parts: list[str]) -> str:
    """Builds a fixture at run time so this file is not itself a finding."""

    return "".join(parts)


def _pem_block() -> str:
    return _joined(["-----BEGIN RSA ", "PRIVATE KEY-----"])


def _public_address() -> str:
    return ".".join(["93", "184", "216", "34"])


@unittest.skipIf(GIT is None, "git is not available")
class CheckSecretsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.repository = Path(self.temporary_directory.name)
        self._git("init", "-q")
        self._git("config", "user.email", "check-secrets@example.invalid")
        self._git("config", "user.name", "check secrets")

    def _git(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=self.repository,
            capture_output=True,
            text=True,
            check=check,
        )

    def _stage(self, name: str, text: str) -> None:
        path = self.repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        self._git("add", name)

    def _scan(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), *arguments],
            cwd=self.repository,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_accepts_documentation_private_and_resolver_addresses(self) -> None:
        self._stage(
            "notes.md",
            "board 192.0.2.10 gateway 192.0.2.1 "
            "lab 10.0.0.5 172.16.3.4 192.168.1.7 dns 114.114.114.114",
        )

        result = self._scan()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")

    def test_accepts_an_empty_staging_area(self) -> None:
        result = self._scan()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_reports_the_file_and_line_of_an_api_credential(self) -> None:
        self._stage("config.py", "first line\n" + _joined(["api", "_key = 'value'"]))

        result = self._scan()

        self.assertEqual(result.returncode, 1)
        self.assertIn("config.py:2: API key:", result.stdout)

    def test_reports_every_secret_category(self) -> None:
        cases = (
            ("token.txt", _joined(["tok", "en=abc"]), 1, "token assignment"),
            ("password.txt", _joined(["pass", "word=abc"]), 1, "password assignment"),
            ("key.pem", _pem_block(), 1, "PEM private key"),
            ("aws.txt", _joined(["AK", "IA", "Q" * 16]), 1, "AWS access key id"),
            ("github.txt", _joined(["gh", "p_", "b" * 32]), 1, "GitHub token"),
            ("api.txt", _joined(["s", "k-", "d" * 20]), 1, "API secret key"),
            ("address.txt", _public_address(), 1, "public IPv4 address"),
        )
        for name, text, line, label in cases:
            with self.subTest(name=name):
                self._stage(name, text)

                result = self._scan()

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"{name}:{line}: {label}:", result.stdout)

    def test_all_mode_scans_committed_lines_that_are_not_in_the_diff(self) -> None:
        committed = self.repository / "app.py"
        committed.write_text(_joined(["pass", "word=abc"]) + "\n", encoding="utf-8")
        self._git("add", "app.py")
        self._git("commit", "-q", "-m", "history")
        with committed.open("a", encoding="utf-8") as stream:
            stream.write("clean tail\n")
        self._git("add", "app.py")

        staged = self._scan()
        everything = self._scan("--all")

        self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
        self.assertEqual(everything.returncode, 1)
        self.assertIn("app.py:1: password assignment:", everything.stdout)

    def test_unknown_option_is_a_usage_error(self) -> None:
        result = self._scan("--bogus")

        self.assertEqual(result.returncode, 2)
        self.assertIn("usage: tools/check_secrets.sh", result.stderr)


if __name__ == "__main__":
    unittest.main()
