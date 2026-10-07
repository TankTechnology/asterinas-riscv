#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Collect repeated command samples without treating wall time as guest latency.

The guest/wrapper must print exactly one PERF_SAMPLE=<number> line per run.
Raw stdout/stderr, including warmups and failed runs, are retained separately.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import time


def parse_sample(output: str) -> float:
    matches = re.findall(r"^PERF_SAMPLE=(\S+)\s*$", output, re.MULTILINE)
    if len(matches) != 1:
        raise ValueError("expected exactly one PERF_SAMPLE marker")
    value = float(matches[0])
    if not math.isfinite(value) or value < 0:
        raise ValueError("sample must be finite and non-negative")
    return value


def collect(command: list[str], output: Path, warmups: int, repeats: int,
            timeout_seconds: float) -> list[float]:
    if (not command or warmups < 1 or repeats < 3
            or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
        raise ValueError("requires a command, warmup, three repeats and positive timeout")
    # Refuse an existing directory so an interrupted run cannot be overwritten.
    output.mkdir(parents=True, exist_ok=False)
    samples = []
    for index in range(warmups + repeats):
        phase = "warmup" if index < warmups else "sample"
        prefix = output / f"{index:03d}-{phase}"
        started = time.monotonic()
        timed_out = False
        with prefix.with_suffix(".stdout").open("w") as stdout, \
                prefix.with_suffix(".stderr").open("w") as stderr:
            process = subprocess.Popen(command, stdout=stdout, stderr=stderr,
                                       start_new_session=True)
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                # Terminate this invocation's process group, not unrelated VMs.
                signal_group = process.pid
                try:
                    os.killpg(signal_group, signal.SIGTERM)
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(signal_group, signal.SIGKILL)
                    process.wait()
                except ProcessLookupError:
                    process.wait()
        record = {"command": command, "phase": phase,
                  "elapsed_seconds": time.monotonic() - started,
                  "returncode": process.returncode, "timed_out": timed_out}
        prefix.with_suffix(".json").write_text(json.dumps(record, indent=2) + "\n")
        if timed_out or process.returncode:
            raise ValueError(f"{prefix.name} failed; raw logs retained")
        value = parse_sample(prefix.with_suffix(".stdout").read_text())
        if phase == "sample":
            samples.append(value)
    (output / "samples.json").write_text(json.dumps(samples, indent=2) + "\n")
    return samples


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        collect(command, args.output, args.warmups, args.repeats, args.timeout_seconds)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
