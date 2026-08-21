#!/usr/bin/env python3

# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import errno
import json
import os
import re
import signal
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[3]
M7_DIRECTORY = REPOSITORY / "tools/riscv/nixos/m7"
REPRODUCER = M7_DIRECTORY / "scm_repro.c"
README = M7_DIRECTORY / "README.md"
ADMISSION_MANIFEST = REPOSITORY / "tools/riscv/nixos/track-admission.v1.json"
ADMISSION_REPORT = REPOSITORY / "tools/riscv/nixos/TRACK-ADMISSION-M1-report.md"
SOURCE_COMMIT = "8a7396a1fae4dfce21b2d0e19794b83dd7771bd8"
REPRODUCER_PATH = "tools/riscv/nixos/m7/scm_repro.c"


class NixosM7AssetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(REPRODUCER.is_file(), f"missing admitted asset: {REPRODUCER}")
        self.assertTrue(README.is_file(), f"missing admitted asset: {README}")

    def test_reproducer_is_the_scoped_unix_credential_probe(self) -> None:
        source = REPRODUCER.read_text(encoding="utf-8")

        self.assertRegex(source, r"SPDX-License-Identifier: MPL-2\.0")
        self.assertIn("SCM_RIGHTS", source)
        self.assertIn("SO_PEERCRED", source)

    def test_readme_records_purpose_provenance_and_scope(self) -> None:
        readme = README.read_text(encoding="utf-8")

        self.assertIn(
            "Purpose: validate AF_UNIX SCM_RIGHTS and SO_PEERCRED required by "
            "nix-daemon.",
            readme,
        )
        self.assertIn(
            f"Provenance: track/nixos commit {SOURCE_COMMIT}.",
            readme,
        )
        self.assertIn(
            "Scope: source fixture only; build/boot integration belongs to the "
            "R3 child issue.",
            readme,
        )
        self.assertIn("Do not copy the host binary into a RISC-V guest.", readme)
        self.assertIn("/root/scm_repro.c", readme)
        self.assertIn("/root/asterinas-scm-repro --require-distinct-ids", readme)

    def test_fixture_has_no_checkout_or_target_dependency(self) -> None:
        source = REPRODUCER.read_text(encoding="utf-8")
        readme = README.read_text(encoding="utf-8")
        admitted_text = source + "\n" + readme

        self.assertNotIn("../", admitted_text)
        self.assertNotRegex(
            admitted_text,
            re.compile(r"/(?:[^\s\"']*/)*target(?:/|\b)"),
        )

    def test_reproducer_compiles_without_warnings(self) -> None:
        compiler = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
        if compiler is None:
            self.skipTest("no C compiler is available")

        subprocess.run(
            [
                compiler,
                "-Wall",
                "-Wextra",
                "-Werror",
                "-fsyntax-only",
                str(REPRODUCER),
            ],
            cwd=REPOSITORY,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_reproducer_runs_to_completion_with_verified_credentials(self) -> None:
        compiler = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
        if compiler is None:
            self.skipTest("no C compiler is available")

        probe_name = (
            b"\0asterinas-m7-test-"
            + str(os.getpid()).encode("ascii")
            + b"-"
            + str(id(self)).encode("ascii")
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(probe_name)
            except OSError as error:
                if error.errno in (errno.EPERM, errno.EACCES):
                    self.skipTest(
                        f"sandbox policy blocks abstract AF_UNIX bind: {error}"
                    )
                raise

        with tempfile.TemporaryDirectory() as temporary_directory:
            executable = Path(temporary_directory) / "scm_repro"
            subprocess.run(
                [
                    compiler,
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    str(REPRODUCER),
                    "-o",
                    str(executable),
                ],
                cwd=REPOSITORY,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            def run_reproducer(*arguments: str) -> tuple[int, int, str, str]:
                process = subprocess.Popen(
                    [str(executable), *arguments],
                    cwd=REPOSITORY,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    text=True,
                )
                try:
                    stdout, stderr = process.communicate(timeout=8)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    stdout, stderr = process.communicate()
                    self.fail(
                        "SCM reproducer exceeded its internal 5-second deadline\n"
                        f"stdout:\n{stdout}\nstderr:\n{stderr}"
                    )
                return process.pid, process.returncode, stdout, stderr

            parent_pid, returncode, stdout, stderr = run_reproducer()
            self.assertEqual(returncode, 0, f"stdout:\n{stdout}\nstderr:\n{stderr}")
            peer = re.search(
                r"__M7_PEERCRED_PID_OK__ pid=(\d+) distinct_ids=0",
                stdout,
            )
            self.assertIsNotNone(peer, stdout)
            peer_pid = int(peer.group(1))
            self.assertNotEqual(peer_pid, parent_pid)
            child = re.search(r"__M7_CHILD_SEND_OK__ pid=(\d+)", stdout)
            self.assertIsNotNone(child, stdout)
            self.assertEqual(peer_pid, int(child.group(1)))
            self.assertNotIn("__M7_PEERCRED_OK__", stdout)
            for marker in (
                "__M7_LISTEN_OK__",
                "__M7_PEERCRED_PID_OK__",
                "__M7_CHILD_SEND_OK__",
                "__M7_SCM_RIGHTS_OK__ read_back=[scm-rights-ok]",
            ):
                with self.subTest(mode="default", marker=marker):
                    self.assertIn(marker, stdout)
            self.assertIn(">>> M7 init: SCM_RIGHTS + SO_PEERCRED repro <<<", stdout)
            self.assertIn(">>> M7 repro done <<<", stdout)

            if os.geteuid() == 0:
                parent_pid, returncode, stdout, stderr = run_reproducer(
                    "--require-distinct-ids"
                )
                self.assertEqual(
                    returncode,
                    0,
                    f"stdout:\n{stdout}\nstderr:\n{stderr}",
                )
                peer = re.search(
                    r"__M7_PEERCRED_OK__ pid=(\d+) uid=(\d+) gid=(\d+) "
                    r"distinct_ids=1",
                    stdout,
                )
                self.assertIsNotNone(peer, stdout)
                peer_pid, peer_uid, peer_gid = map(int, peer.groups())
                self.assertNotEqual(peer_pid, parent_pid)
                self.assertEqual((peer_uid, peer_gid), (65534, 65534))
                child = re.search(r"__M7_CHILD_SEND_OK__ pid=(\d+)", stdout)
                self.assertIsNotNone(child, stdout)
                self.assertEqual(peer_pid, int(child.group(1)))
                self.assertNotIn("__M7_PEERCRED_PID_OK__", stdout)
            else:
                _, returncode, stdout, stderr = run_reproducer(
                    "--require-distinct-ids"
                )
                self.assertEqual(
                    returncode,
                    2,
                    f"stdout:\n{stdout}\nstderr:\n{stderr}",
                )
                self.assertIn("__M7_DISTINCT_IDS_REQUIRES_ROOT__", stdout)

            for argument, timeout_marker in (
                ("--test-exit-before-connect", "__M7_ACCEPT_TIMEOUT_FAIL__"),
                ("--test-stall-after-connect", "__M7_RECV_TIMEOUT_FAIL__"),
            ):
                _, returncode, stdout, stderr = run_reproducer(argument)
                self.assertNotEqual(
                    returncode,
                    0,
                    f"stdout:\n{stdout}\nstderr:\n{stderr}",
                )
                self.assertNotEqual(returncode, 124)
                self.assertIn(timeout_marker, stdout)

    def test_admission_inventory_records_the_checked_in_fixture(self) -> None:
        manifest = json.loads(ADMISSION_MANIFEST.read_text(encoding="utf-8"))
        record = next(
            item
            for item in manifest["records"]
            if item["source_commit"] == SOURCE_COMMIT
        )
        report = ADMISSION_REPORT.read_text(encoding="utf-8")

        self.assertEqual(record["classification"], "portable")
        self.assertIn("#67", record["destination"])
        self.assertIn(REPRODUCER_PATH, record["reason"])
        self.assertIn(
            "tools.riscv.tests.test_nixos_m7_assets",
            record["verification"],
        )
        self.assertIn(SOURCE_COMMIT, report)
        self.assertIn(REPRODUCER_PATH, report)


if __name__ == "__main__":
    unittest.main()
