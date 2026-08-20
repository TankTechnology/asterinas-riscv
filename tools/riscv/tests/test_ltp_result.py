#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import contextlib
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path

from ltp_result import build_result_document, main, parse_ltp_serial, summary_text


SERIAL = """\
[PASS] read01
[FAIL] open01
[CONF] bind04
[CRASH] connect01
[TIMEOUT] fcntl14
__LTP_GATE_DONE__
[summary] total=5 pass=1 fail=3 conf=1 crash=1 timeout=1
__LTP_GATE_FAIL__
"""


def boot_result(*, passed: bool = True) -> dict[str, object]:
    return {
        "passed": passed,
        "profile": "generic-sv39-ltp-smp1",
        "artifacts": {
            "kernel_sha256": "1" * 64,
            "dtb_sha256": "2" * 64,
            "initrd_sha256": "3" * 64,
        },
        "boot_disk_sha256_before": "4" * 64,
    }


class LtpSerialParserTests(unittest.TestCase):
    def test_parse_normalizes_aggregate_failures(self) -> None:
        result = parse_ltp_serial(SERIAL)

        self.assertEqual(result.counts.total, 5)
        self.assertEqual(result.counts.pass_count, 1)
        self.assertEqual(result.counts.fail_count, 1)
        self.assertEqual(result.counts.conf_count, 1)
        self.assertEqual(result.counts.crash_count, 1)
        self.assertEqual(result.counts.timeout_count, 1)
        self.assertEqual(result.counts.legacy_fail_total, 3)
        self.assertFalse(result.ltp_passed)

    def test_parse_rejects_inconsistent_summary_total(self) -> None:
        bad = SERIAL.replace("total=5", "total=6")

        with self.assertRaisesRegex(ValueError, "summary total"):
            parse_ltp_serial(bad)

    def test_parse_rejects_duplicate_done_markers(self) -> None:
        with self.assertRaisesRegex(ValueError, "DONE marker"):
            parse_ltp_serial(SERIAL + "__LTP_GATE_DONE__\n")

    def test_parse_rejects_duplicate_verdict_names(self) -> None:
        duplicate = SERIAL.replace("[PASS] read01", "[PASS] open01")

        with self.assertRaisesRegex(ValueError, "duplicate LTP verdict name"):
            parse_ltp_serial(duplicate)

    def test_parse_rejects_terminal_marker_that_disagrees_with_summary(self) -> None:
        inconsistent = SERIAL.replace("__LTP_GATE_FAIL__", "__LTP_GATE_PASS__")

        with self.assertRaisesRegex(ValueError, "terminal marker"):
            parse_ltp_serial(inconsistent)

    def test_parse_accepts_carriage_return_serial_lines(self) -> None:
        result = parse_ltp_serial(SERIAL.replace("\n", "\r\n"))

        self.assertEqual(result.counts.total, 5)


class LtpResultDocumentTests(unittest.TestCase):
    def test_document_separates_infrastructure_and_ltp_status(self) -> None:
        document = build_result_document(
            parse_ltp_serial(SERIAL),
            boot_result=boot_result(passed=True),
            git_commit="a" * 40,
            smp=1,
        )

        self.assertTrue(document["infrastructure_passed"])
        self.assertFalse(document["ltp_passed"])
        self.assertEqual(document["counts"]["fail"], 1)
        self.assertEqual(document["counts"]["legacy_fail_total"], 3)
        self.assertEqual(document["artifacts"]["boot_disk_sha256"], "4" * 64)
        self.assertEqual(len(document["verdicts"]), 5)

    def test_document_rejects_profile_smp_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "profile does not match SMP"):
            build_result_document(
                parse_ltp_serial(SERIAL),
                boot_result=boot_result(),
                git_commit="a" * 40,
                smp=4,
            )

    def test_summary_uses_mutually_exclusive_counts(self) -> None:
        document = build_result_document(
            parse_ltp_serial(SERIAL),
            boot_result=boot_result(),
            git_commit="a" * 40,
            smp=1,
        )

        self.assertEqual(
            summary_text(document),
            "infrastructure=PASS ltp=FAIL\n"
            "total=5 pass=1 fail=1 conf=1 crash=1 timeout=1 "
            "legacy_fail_total=3\n",
        )

    def test_write_cli_publishes_json_and_summary_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            serial = directory / "serial.log"
            boot = directory / "boot-result.json"
            result = directory / "result.json"
            summary = directory / "summary.txt"
            serial.write_text(SERIAL)
            boot.write_text(json.dumps(boot_result()) + "\n")
            arguments = [
                "write",
                "--serial",
                str(serial),
                "--boot-result",
                str(boot),
                "--result",
                str(result),
                "--summary",
                str(summary),
                "--git-commit",
                "a" * 40,
                "--smp",
                "1",
            ]

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(arguments), 0)
            payload = json.loads(result.read_text())
            self.assertEqual(payload["counts"]["legacy_fail_total"], 3)
            self.assertEqual(summary.read_text(), summary_text(payload))
            self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(summary.stat().st_mode), 0o644)
            with self.assertRaises(FileExistsError):
                with contextlib.redirect_stdout(io.StringIO()):
                    main(arguments)


if __name__ == "__main__":
    unittest.main()
