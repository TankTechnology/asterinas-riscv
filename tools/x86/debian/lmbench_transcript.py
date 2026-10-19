#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Select the LMbench run that a boot transcript actually produced.

The guest dumps every directory under ``/opt/lmbench/asterinas-runs`` on each
boot, so a transcript also contains reports from earlier boots.  The run that
belongs to a boot is the directory that did not exist in the previous
transcript's dump; without a previous transcript the console-measured
duration (``LMBENCH_RUN_NATIVE_START``/``EXIT``) selects the closest report.

Acceptance is rate-based.  LMbench's timing-sensitive groups are sensitive to
host scheduling, so the suite is not expected to be green on every boot:
a transcript is green when the selected report has ``passed: true`` and no
missing group.  Use ``--require-rate`` to gate a batch on a minimum rate.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


PREFIX = re.compile(r"^[^:]*lmbench-run\.sh\[\d+\]: ")
DUMP = re.compile(
    r"LMBENCH_RUN_DUMP_BEGIN /opt/lmbench/asterinas-runs/run-([A-Za-z0-9_.-]+)"
)
START = re.compile(r"LMBENCH_RUN_NATIVE_START (\d+)")
EXIT = re.compile(r"LMBENCH_RUN_NATIVE_EXIT (-?\d+) (\d+)")


@dataclass
class Verdict:
    transcript: str
    passed: bool | None
    observed: int
    total: int
    missing: list[str]
    errors: list[str]
    elapsed_seconds: float | None
    console_seconds: int | None
    run: str | None
    selection: str

    @property
    def green(self) -> bool:
        return self.passed is True and not self.missing

    def line(self) -> str:
        if self.passed is None:
            return f"{self.transcript}: NO REPORT (console {self.console_seconds}s)"
        missing = ",".join(self.missing) if self.missing else "-"
        return (
            f"{self.transcript}: passed={self.passed} "
            f"observed={self.observed}/{self.total} "
            f"missing={missing} elapsed={self.elapsed_seconds}s "
            f"(console {self.console_seconds}s; {self.selection})"
        )

    def as_dict(self) -> dict:
        return {
            "transcript": self.transcript,
            "passed": self.passed,
            "observed": self.observed,
            "total": self.total,
            "missing": self.missing,
            "errors": self.errors,
            "elapsed_seconds": self.elapsed_seconds,
            "console_seconds": self.console_seconds,
            "run": self.run,
            "selection": self.selection,
            "green": self.green,
        }


def strip_prefix(text: str) -> str:
    return "\n".join(PREFIX.sub("", line) for line in text.splitlines())


def reports(text: str) -> list[dict]:
    """Extract the JSON report objects printed by the runner."""
    lines = strip_prefix(text).splitlines()
    found: list[dict] = []
    for start, line in enumerate(lines):
        if line.strip() != "{":
            continue
        depth = 0
        for end in range(start, len(lines)):
            depth += lines[end].count("{") - lines[end].count("}")
            if depth != 0:
                continue
            try:
                report = json.loads("\n".join(lines[start : end + 1]))
            except json.JSONDecodeError:
                break
            if "passed" in report and "missing" in report:
                found.append(report)
            break
    return found


def dump_dirs(text: str) -> set[str]:
    return set(DUMP.findall(text))


def read(path: Path) -> str:
    return path.read_text(errors="replace")


def console_duration(text: str) -> int | None:
    start, end = START.search(text), EXIT.search(text)
    if start is None or end is None:
        return None
    return int(end.group(2)) - int(start.group(1))


def run_name(report: dict) -> str | None:
    output = report.get("output")
    if not output:
        return None
    return Path(output).name.removeprefix("run-")


def select(
    text: str, previous: str | None
) -> tuple[dict | None, str]:
    candidates = reports(text)
    if not candidates:
        return None, ""

    duration = console_duration(text)
    if previous is not None:
        new = dump_dirs(text) - dump_dirs(previous)
        matching = [r for r in candidates if run_name(r) in new]
        if len(matching) == 1:
            return matching[0], f"new run directory {run_name(matching[0])}"
        if matching and duration is not None:
            chosen = min(
                matching,
                key=lambda r: abs(float(r.get("elapsed_seconds", -1e9)) - duration),
            )
            return chosen, f"new run directory {run_name(chosen)}"

    if duration is None:
        return candidates[0], "only report"

    chosen = min(
        candidates,
        key=lambda r: abs(float(r.get("elapsed_seconds", -1e9)) - duration),
    )
    delta = abs(float(chosen.get("elapsed_seconds", -1e9)) - duration)
    return chosen, f"elapsed match, delta {delta:.1f}s"


def verdict(path: Path, previous: str | None = None) -> Verdict:
    text = read(path)
    report, why = select(text, previous)
    duration = console_duration(text)
    if report is None:
        return Verdict(
            transcript=path.name,
            passed=None,
            observed=0,
            total=0,
            missing=[],
            errors=[],
            elapsed_seconds=None,
            console_seconds=duration,
            run=None,
            selection="",
        )
    observed = report.get("observed", [])
    missing = report.get("missing", [])
    return Verdict(
        transcript=path.name,
        passed=report.get("passed"),
        observed=len(observed),
        total=len(observed) + len(missing),
        missing=list(missing),
        errors=list(report.get("errors", [])),
        elapsed_seconds=report.get("elapsed_seconds"),
        console_seconds=duration,
        run=run_name(report),
        selection=why,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcripts", nargs="+", type=Path)
    parser.add_argument(
        "--previous",
        type=Path,
        help=(
            "transcript from the preceding boot, used to identify the run "
            "directory that boot created (single transcript only)"
        ),
    )
    parser.add_argument(
        "--require-rate",
        type=float,
        metavar="RATE",
        help="exit non-zero unless at least this fraction of transcripts are green",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    arguments = parser.parse_args(argv)

    if arguments.previous is not None and len(arguments.transcripts) != 1:
        parser.error("--previous accepts exactly one transcript")

    previous_text = (
        read(arguments.previous) if arguments.previous is not None else None
    )
    verdicts = [verdict(path, previous_text) for path in arguments.transcripts]

    if arguments.json:
        print(json.dumps([item.as_dict() for item in verdicts], indent=2))
    else:
        for item in verdicts:
            print(item.line())
            if item.errors:
                print(f"    errors={item.errors}")

    green = sum(item.green for item in verdicts)
    total = len(verdicts)
    if not arguments.json:
        rate = green / total if total else 0.0
        print(f"summary: {green}/{total} transcripts green ({rate:.0%})")

    if arguments.require_rate is not None:
        rate = green / total if total else 0.0
        if rate < arguments.require_rate:
            print(
                f"error: green rate {rate:.0%} is below the required "
                f"{arguments.require_rate:.0%}",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
