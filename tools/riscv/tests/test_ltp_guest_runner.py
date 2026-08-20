#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
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
    def test_init_reaps_any_child_and_uses_one_brok_prefix(self) -> None:
        source = INIT_SOURCE.read_text()

        self.assertIn("waitpid(-1, &child_status, 0)", source)
        self.assertIn("[BROK] LTP runner fork failed", source)
        self.assertIn("[BROK] LTP runner waitpid failed", source)

    def test_runner_rejects_warning_empty_and_exec_failure_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            binaries = directory / "bin"
            logs = directory / "logs"
            manifest = directory / "syscalls"
            runner = directory / "ltp-runner"
            binaries.mkdir()
            write_executable(
                binaries / "mixed",
                "#!/bin/sh\necho 'TCONF: skipped'\necho 'TWARN: cleanup'\nexit 36\n",
            )
            write_executable(binaries / "empty", "#!/bin/sh\nexit 0\n")
            write_executable(
                binaries / "broken-exec",
                "#!/missing/interpreter\n",
            )
            manifest.write_text(
                "mixed01 mixed\nempty01 empty\nbroken_exec01 broken-exec\n"
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
                    "-o",
                    str(runner),
                    str(RUNNER_SOURCE),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            result = subprocess.run(
                [str(runner), str(manifest)],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )

        self.assertIn("[FAIL] mixed01", result.stdout)
        self.assertIn("[FAIL] empty01", result.stdout)
        self.assertIn("[FAIL] broken_exec01", result.stdout)
        self.assertIn(
            "[summary] total=3 pass=0 fail=3 conf=0 crash=0 timeout=0",
            result.stdout,
        )

    def test_runner_survives_a_test_that_kills_its_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            binaries = directory / "bin"
            logs = directory / "logs"
            manifest = directory / "syscalls"
            runner = directory / "ltp-runner"
            binaries.mkdir()
            write_executable(
                binaries / "killer",
                "#!/bin/sh\nkill -KILL \"$PPID\"\n",
            )
            write_executable(binaries / "after", "#!/bin/sh\necho 'TPASS:'\n")
            manifest.write_text("killer01 killer\nafter01 after\n")
            subprocess.run(
                [
                    CC,
                    "-std=c11",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    f'-DBIN_DIR="{binaries}"',
                    f'-DLOG_DIR="{logs}"',
                    "-o",
                    str(runner),
                    str(RUNNER_SOURCE),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            result = subprocess.run(
                [str(runner), str(manifest)],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[CRASH] killer01", result.stdout)
        self.assertIn("[PASS] after01", result.stdout)
        self.assertIn("__LTP_GATE_DONE__", result.stdout)

    def test_runner_cleans_descendants_after_a_normal_test_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            binaries = directory / "bin"
            logs = directory / "logs"
            manifest = directory / "syscalls"
            marker = directory / "leaked"
            runner = directory / "ltp-runner"
            binaries.mkdir()
            leaker_source = directory / "leaker.c"
            leaker_source.write_text(
                "#include <fcntl.h>\n"
                "#include <stdio.h>\n"
                "#include <unistd.h>\n"
                "int main(void) {\n"
                "    pid_t child = fork();\n"
                "    if (child < 0) return 1;\n"
                "    if (child == 0) {\n"
                "        sleep(1);\n"
                f'        int fd = open("{marker.as_posix()}", '
                "O_WRONLY | O_CREAT | O_TRUNC, 0600);\n"
                "        if (fd >= 0) {\n"
                '            (void)write(fd, "leaked\\n", 7);\n'
                "            (void)close(fd);\n"
                "        }\n"
                "        _exit(0);\n"
                "    }\n"
                '    puts("TPASS:");\n'
                "    return 0;\n"
                "}\n"
            )
            subprocess.run(
                [
                    CC,
                    "-std=c11",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-o",
                    str(binaries / "leaker"),
                    str(leaker_source),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            manifest.write_text("leaker01 leaker\n")
            subprocess.run(
                [
                    CC,
                    "-std=c11",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    f'-DBIN_DIR="{binaries}"',
                    f'-DLOG_DIR="{logs}"',
                    "-o",
                    str(runner),
                    str(RUNNER_SOURCE),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            result = subprocess.run(
                [str(runner), str(manifest)],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            time.sleep(1.2)

            self.assertIn("[PASS] leaker01", result.stdout)
            self.assertFalse(marker.exists(), "background descendant survived")

    def test_runner_classifies_all_mutually_exclusive_verdicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            binaries = directory / "bin"
            logs = directory / "logs"
            manifest = directory / "syscalls"
            runner = directory / "ltp-runner"
            binaries.mkdir()
            write_executable(binaries / "pass", "#!/bin/sh\necho 'TPASS:'\n")
            write_executable(
                binaries / "fail",
                "#!/bin/sh\necho 'TFAIL:'\nexit 1\n",
            )
            write_executable(
                binaries / "conf",
                "#!/bin/sh\necho 'TCONF:'\nexit 32\n",
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
        self.assertIn("[RUN] 1 pass01", result.stdout)
        self.assertIn("[RUN] 5 timeout01", result.stdout)
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

    def test_init_runs_the_runner_as_a_child_and_remains_alive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            marker = directory / "runner-finished"
            fixture = directory / "runner"
            init = directory / "init"
            write_executable(
                fixture,
                f"#!/bin/sh\ntouch {marker}\nexit 23\n",
            )
            subprocess.run(
                [
                    CC,
                    "-std=c11",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-DSKIP_CONSOLE_ATTACH=1",
                    "-DSKIP_PSEUDO_FS_MOUNTS=1",
                    f'-DRUNNER_PATH="{fixture}"',
                    "-o",
                    str(init),
                    str(INIT_SOURCE),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            process = subprocess.Popen(
                [str(init)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                deadline = time.monotonic() + 2
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)

                self.assertTrue(marker.is_file(), "runner child did not execute")
                self.assertIsNone(process.poll(), "PID 1 shim exited with the runner")
            finally:
                process.terminate()
                output, _ = process.communicate(timeout=2)
            self.assertIn("runner exited with status 23", output)
            self.assertIn("__LTP_GATE_TERMINAL__", output)


class LtpBuildScriptContractTests(unittest.TestCase):
    def test_builder_requires_busybox_before_replacing_rootfs(self) -> None:
        source = BUILD_SCRIPT.read_text()

        preflight = 'if [[ ! -x "${BUSYBOX}" ]]; then'
        destructive_stage = 'rm -rf "${ROOTFS}"'
        self.assertIn(preflight, source)
        self.assertLess(source.index(preflight), source.index(destructive_stage))
        self.assertIn("missing required BusyBox", source)
        self.assertNotIn("WARN: no busybox", source)

    def test_build_script_selects_only_closed_named_suites(self) -> None:
        source = BUILD_SCRIPT.read_text()

        self.assertIn('SUITE="syscalls"', source)
        self.assertIn('--suite) SUITE="$2"; shift 2 ;;', source)
        self.assertIn('arch-riscv64)', source)
        self.assertIn('EXPECTED_SELECTED=138', source)
        self.assertIn('EXPECTED_UNAVAILABLE=1', source)
        self.assertIn('--expected-count "${EXPECTED_SELECTED}"', source)

    def test_builder_installs_account_databases_world_readable(self) -> None:
        source = BUILD_SCRIPT.read_text()

        self.assertIn('install -m 0644 "${SRC_DIR}/etc-passwd"', source)
        self.assertIn('install -m 0644 "${SRC_DIR}/etc-group"', source)

    def test_builder_uses_validated_manifest_and_never_shared_qemu_artifacts(
        self,
    ) -> None:
        source = BUILD_SCRIPT.read_text()

        self.assertIn("tools/riscv/ltp_manifest.py", source)
        self.assertIn("--unavailable-output", source)
        self.assertIn('--expected-count "${EXPECTED_SELECTED}"', source)
        for tool in ("aclocal", "autoconf", "automake"):
            self.assertIn(tool, source)
        self.assertIn("linux/limits.h", source)
        self.assertNotIn("target/qemu-uboot/current", source)
        self.assertNotIn("boot.ext4", source)


if __name__ == "__main__":
    unittest.main()
