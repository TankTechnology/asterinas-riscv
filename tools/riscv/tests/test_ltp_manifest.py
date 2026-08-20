#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ltp_manifest import main, select_manifest


REPO = Path(__file__).resolve().parents[3]
REPOSITORY_MANIFEST = REPO / "test/initramfs/src/conformance/ltp/testcases/all.txt"


class RepositoryManifestContractTests(unittest.TestCase):
    def test_reviewed_m27_manifest_has_779_unique_enabled_names(self) -> None:
        enabled = tuple(
            stripped.split()[0]
            for line in REPOSITORY_MANIFEST.read_text().splitlines()
            if (stripped := line.strip()) and not stripped.startswith("#")
        )

        self.assertEqual(len(enabled), 779)
        self.assertEqual(len(set(enabled)), 779)
        self.assertTrue(
            {
                "accept01",  # Network bind bucket.
                "mount01",  # Requires loop-device acquisition.
                "clock_gettime03",  # Requires procfs namespace files.
                "execve02",  # Requires a packaged exec helper.
                "sched_getattr01",  # Scheduling boundary semantics.
                "mmap04",  # Memory-map and procfs semantics.
            }.issubset(enabled)
        )


class ManifestSelectionTests(unittest.TestCase):
    def test_select_reports_every_unavailable_enabled_test(self) -> None:
        enabled = "# comment\nread01\nopen01\nmissing01\n"
        runtest = (
            "read01 read01\n"
            "open01 open01 -s\n"
            "missing01 missing01\n"
            "unselected01 unselected01\n"
        )

        selection = select_manifest(
            enabled,
            runtest,
            available={"read01", "open01"},
        )

        self.assertEqual(
            selection.lines,
            ("read01 read01", "open01 open01 -s"),
        )
        self.assertEqual(
            tuple((item.name, item.reason) for item in selection.unavailable),
            (("missing01", "missing-binary"),),
        )
        self.assertEqual(
            selection.requested,
            ("read01", "open01", "missing01"),
        )

    def test_select_reports_enabled_name_absent_from_runtest(self) -> None:
        selection = select_manifest(
            "munmap02\n",
            "read01 read01\n",
            available={"read01"},
        )

        self.assertEqual(selection.lines, ())
        self.assertEqual(
            tuple((item.name, item.reason) for item in selection.unavailable),
            (("munmap02", "not-in-runtest"),),
        )

    def test_select_rejects_duplicate_enabled_names(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate enabled test"):
            select_manifest(
                "read01\nread01\n",
                "read01 read01\n",
                available={"read01"},
            )

    def test_select_rejects_duplicate_runtest_names(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate runtest tag"):
            select_manifest(
                "read01\n",
                "read01 read01\nread01 read01 -i 2\n",
                available={"read01"},
            )

    def test_subset_preserves_requested_order(self) -> None:
        selection = select_manifest(
            "read01\nopen01\n",
            "read01 read01\nopen01 open01 -s\n",
            available={"read01", "open01"},
            subset=("open01", "read01"),
        )

        self.assertEqual(
            selection.lines,
            ("open01 open01 -s", "read01 read01"),
        )
        self.assertEqual(selection.requested, ("open01", "read01"))
        self.assertEqual(selection.unavailable, ())

    def test_subset_rejects_unknown_or_unavailable_names(self) -> None:
        arguments = {
            "enabled_text": "read01\nmissing01\n",
            "runtest_text": "read01 read01\nmissing01 missing01\n",
            "available": {"read01"},
        }

        with self.assertRaisesRegex(ValueError, "unknown subset tag"):
            select_manifest(**arguments, subset=("unknown01",))
        with self.assertRaisesRegex(ValueError, "subset tag is unavailable"):
            select_manifest(**arguments, subset=("missing01",))


class ManifestCommandLineTests(unittest.TestCase):
    def test_cli_publishes_selected_manifest_and_unavailable_json_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            enabled = directory / "all.txt"
            runtest = directory / "syscalls"
            binaries = directory / "bin"
            output = directory / "selected"
            unavailable = directory / "unavailable.json"
            binaries.mkdir()
            enabled.write_text("read01\nmissing01\nmunmap02\n")
            runtest.write_text("read01 read01\nmissing01 missing01\n")
            (binaries / "read01").write_text("binary")
            arguments = [
                "select",
                "--enabled",
                str(enabled),
                "--runtest",
                str(runtest),
                "--bin-dir",
                str(binaries),
                "--output",
                str(output),
                "--unavailable-output",
                str(unavailable),
                "--expected-count",
                "1",
            ]

            self.assertEqual(main(arguments), 0)
            self.assertEqual(output.read_text(), "read01 read01\n")
            self.assertEqual(
                json.loads(unavailable.read_text()),
                [
                    {"name": "missing01", "reason": "missing-binary"},
                    {"name": "munmap02", "reason": "not-in-runtest"},
                ],
            )
            with self.assertRaises(FileExistsError):
                main(arguments)

    def test_cli_rejects_unexpected_selected_count_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            enabled = directory / "all.txt"
            runtest = directory / "syscalls"
            binaries = directory / "bin"
            output = directory / "selected"
            unavailable = directory / "unavailable.json"
            binaries.mkdir()
            enabled.write_text("read01\n")
            runtest.write_text("read01 read01\n")
            (binaries / "read01").write_text("binary")

            with self.assertRaisesRegex(ValueError, "expected 2 selected tests"):
                main(
                    [
                        "select",
                        "--enabled",
                        str(enabled),
                        "--runtest",
                        str(runtest),
                        "--bin-dir",
                        str(binaries),
                        "--output",
                        str(output),
                        "--unavailable-output",
                        str(unavailable),
                        "--expected-count",
                        "2",
                    ]
                )
            self.assertFalse(output.exists())
            self.assertFalse(unavailable.exists())


if __name__ == "__main__":
    unittest.main()
