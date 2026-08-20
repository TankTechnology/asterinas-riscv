#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1]
LTP = TOOLS / "nixos" / "ltp"
RUNNER_SOURCE = LTP / "ltp_runner.c"
INIT_SOURCE = LTP / "init_ltp.c"
BUILD_SCRIPT = LTP / "build_ltp.sh"
CC = shutil.which("cc")


def write_executable(path: Path, source: str) -> None:
    path.write_text(source)
    path.chmod(0o755)


@unittest.skipUnless(CC, "requires a host C compiler")
class LtpGuestRunnerTests(unittest.TestCase):
    def test_runner_classifies_all_mutually_exclusive_verdicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            binaries = directory / "bin"
            logs = directory / "logs"
            manifest = directory / "syscalls"
            runner = directory / "ltp-runner"
            binaries.mkdir()
            write_executable(binaries / "pass", "#!/bin/sh\necho TPASS\n")
            write_executable(
                binaries / "fail",
                "#!/bin/sh\necho TFAIL\nexit 1\n",
            )
            write_executable(
                binaries / "conf",
                "#!/bin/sh\necho TCONF\nexit 32\n",
            )
            write_executable(
                binaries / "crash",
                "#!/bin/sh\nkill -SEGV $$\n",
            )
            write_executable(
                binaries / "timeout",
                "#!/bin/sh\nsleep 5\n",
            )
            manifest.write_text(
                "pass01 pass\n"
                "fail01 fail\n"
                "conf01 conf\n"
                "crash01 crash\n"
                "timeout01 timeout\n"
            )
            subprocess.run(
                [
                    CC,
                    "-std=c11",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    f'-DBIN_DIR="{binaries}"',
                    f'-DLOG_DIR="{logs}"',
                    "-DDEFAULT_TIMEOUT_SEC=1",
                    "-o",
                    str(runner),
                    str(RUNNER_SOURCE),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            environment = os.environ.copy()
            environment["LTP_PER_TEST_TIMEOUT"] = "1"
            environment["LTP_TIMEOUT_MUL"] = "1"

            result = subprocess.run(
                [str(runner), str(manifest)],
                check=True,
                capture_output=True,
                env=environment,
                text=True,
                timeout=10,
            )

        for verdict in (
            "[PASS] pass01",
            "[FAIL] fail01",
            "[CONF] conf01",
            "[CRASH] crash01",
            "[TIMEOUT] timeout01",
        ):
            self.assertIn(verdict, result.stdout)
        self.assertIn("__LTP_GATE_DONE__", result.stdout)
        self.assertIn("__LTP_GATE_FAIL__", result.stdout)
        self.assertIn(
            "[summary] total=5 pass=1 fail=3 conf=1 crash=1 timeout=1",
            result.stdout,
        )

    def test_init_source_compiles_without_warnings(self) -> None:
        subprocess.run(
            [
                CC,
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-fsyntax-only",
                str(INIT_SOURCE),
            ],
            check=True,
            capture_output=True,
            text=True,
        )


class LtpBuildScriptContractTests(unittest.TestCase):
    def test_builder_uses_validated_manifest_and_never_shared_qemu_artifacts(
        self,
    ) -> None:
        source = BUILD_SCRIPT.read_text()

        self.assertIn("tools/riscv/ltp_manifest.py", source)
        self.assertIn("--unavailable-output", source)
        self.assertIn("--expected-count 767", source)
        for tool in ("aclocal", "autoconf", "automake"):
            self.assertIn(tool, source)
        self.assertNotIn("target/qemu-uboot/current", source)
        self.assertNotIn("boot.ext4", source)


if __name__ == "__main__":
    unittest.main()
