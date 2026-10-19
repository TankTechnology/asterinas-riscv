# SPDX-License-Identifier: MPL-2.0

"""Unit tests for the LMbench transcript verdict parser."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPOSITORY_ROOT / "tools/x86/debian/lmbench_transcript.py"

SPEC = importlib.util.spec_from_file_location("lmbench_transcript", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
lmbench_transcript = importlib.util.module_from_spec(SPEC)
sys.modules["lmbench_transcript"] = lmbench_transcript
SPEC.loader.exec_module(lmbench_transcript)

PREFIX = "lmbench-run.sh[42]: "


def report_lines(
    run: str,
    *,
    passed: bool,
    elapsed: float,
    missing: tuple[str, ...] = (),
) -> list[str]:
    import json

    report = {
        "passed": passed,
        "missing": list(missing),
        "observed": ["a", "b"],
        "errors": [],
        "elapsed_seconds": elapsed,
        "output": f"/opt/lmbench/asterinas-runs/run-{run}",
    }
    return [PREFIX + line for line in json.dumps(report, indent=2).splitlines()]


def transcript(
    dumps: list[str],
    reports: list[list[str]],
    console_seconds: int,
) -> str:
    lines = [
        PREFIX + f"LMBENCH_RUN_DUMP_BEGIN /opt/lmbench/asterinas-runs/run-{dump}"
        for dump in dumps
    ]
    for lines_of_report in reports:
        lines.extend(lines_of_report)
    lines.append(PREFIX + "LMBENCH_RUN_NATIVE_START 100")
    lines.append(PREFIX + f"LMBENCH_RUN_NATIVE_EXIT 0 {100 + console_seconds}")
    return "\n".join(lines) + "\n"


class TranscriptVerdictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def write(self, name: str, text: str) -> Path:
        path = self.directory / name
        path.write_text(text)
        return path

    def test_elapsed_match_selects_own_report(self) -> None:
        stale = report_lines("stale", passed=True, elapsed=20.0)
        own = report_lines("own", passed=False, elapsed=130.0, missing=("signal-prot",))
        path = self.write(
            "t.transcript", transcript(["stale", "own"], [stale, own], 130)
        )

        result = lmbench_transcript.verdict(path)

        self.assertEqual(result.run, "own")
        self.assertFalse(result.green)
        self.assertEqual(result.missing, ["signal-prot"])
        self.assertEqual(result.observed, 2)
        self.assertEqual(result.total, 3)

    def test_new_directory_wins_over_closer_stale_elapsed(self) -> None:
        # The previous boot's report is numerically closer to this boot's
        # console duration, but only `own` is new in the dump.
        stale = report_lines("stale", passed=True, elapsed=129.0)
        own = report_lines("own", passed=False, elapsed=130.0, missing=("signal-prot",))
        previous = transcript(["stale"], [stale], 130)
        current = transcript(["stale", "own"], [stale, own], 130)
        previous_path = self.write("previous.transcript", previous)
        current_path = self.write("current.transcript", current)

        result = lmbench_transcript.verdict(
            current_path, lmbench_transcript.read(previous_path)
        )

        self.assertEqual(result.run, "own")
        self.assertFalse(result.green)
        self.assertIn("new run directory own", result.selection)

    def test_previous_without_new_directory_falls_back_to_elapsed(self) -> None:
        stale = report_lines("stale", passed=True, elapsed=129.0)
        own = report_lines("own", passed=False, elapsed=130.0, missing=("rpc-udp",))
        previous = transcript(["stale"], [stale], 130)
        current = transcript(["stale"], [stale, own], 130)
        previous_path = self.write("previous.transcript", previous)
        current_path = self.write("current.transcript", current)

        result = lmbench_transcript.verdict(
            current_path, lmbench_transcript.read(previous_path)
        )

        self.assertEqual(result.run, "own")
        self.assertIn("elapsed match", result.selection)

    def test_transcript_without_report_is_not_green(self) -> None:
        path = self.write("empty.transcript", transcript([], [], 10))

        result = lmbench_transcript.verdict(path)

        self.assertIsNone(result.passed)
        self.assertFalse(result.green)
        self.assertIn("NO REPORT", result.line())

    def test_require_rate_gate(self) -> None:
        good = self.write(
            "good.transcript",
            transcript(
                ["good"], [report_lines("good", passed=True, elapsed=10.0)], 10
            ),
        )
        bad = self.write(
            "bad.transcript",
            transcript(
                ["bad"],
                [
                    report_lines(
                        "bad", passed=False, elapsed=20.0, missing=("signal-prot",)
                    )
                ],
                20,
            ),
        )

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(
                lmbench_transcript.main(
                    [str(good), str(bad), "--require-rate", "0.4"]
                ),
                0,
            )
        self.assertIn("summary: 1/2 transcripts green (50%)", stdout.getvalue())

        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            self.assertEqual(
                lmbench_transcript.main(
                    [str(good), str(bad), "--require-rate", "0.6"]
                ),
                1,
            )
        self.assertIn("below the required", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
