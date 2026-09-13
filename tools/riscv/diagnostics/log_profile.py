# SPDX-License-Identifier: MPL-2.0

"""Validate bounded boot log-profile evidence without discarding bad samples."""

import argparse
import json
from pathlib import Path
import re


STATS = r"DurationStats \{ count: (\d+), total_ticks: (\d+), max_ticks: (\d+), invalid: (\d+) \}"
BATCH = re.compile(
    r"LOG_PROFILE batch rep=(\d+) level=(\d+) measured=(true|false) records=32 "
    + " ".join(f"{name}={STATS}" for name in ("elapsed", "memory", "lock_wait", "locked_send"))
    + r" attempted_bytes=(\d+)"
)


def _stats(values, expected):
    count, total, maximum, invalid = map(int, values)
    if count != expected or invalid or maximum > total or total > count * maximum:
        raise ValueError("incomplete or inconsistent timing statistics")
    if count == 0 and (total or maximum):
        raise ValueError("empty timing statistics contain durations")
    return dict(count=count, total_ticks=total, max_ticks=maximum, invalid=invalid)


def validate(transcript, exit_code):
    if exit_code != 0:
        raise ValueError(f"probe runner failed: {exit_code}")
    if re.search(
        r"uncaught panic|kernel panic|unexpected exception|stack trace:|"
        r"panic handler panicked|aborting the system",
        transcript, re.I,
    ):
        raise ValueError("fatal output in probe transcript")
    # Fixed payload lines include LOG_PROFILE but are not control records.
    lines = [line for line in transcript.splitlines() if line.startswith("LOG_PROFILE ")]
    if len(lines) != 22 or lines[-1] != "LOG_PROFILE end records=640 source=user":
        raise ValueError("missing, duplicated, or unavailable profile")
    overhead = re.fullmatch(r"LOG_PROFILE overhead frequency=(\d+) stats=" + STATS, lines[0])
    if overhead is None or int(overhead[1]) == 0:
        raise ValueError("missing timing frequency or overhead")
    result = dict(frequency=int(overhead[1]), overhead=_stats(overhead.groups()[1:], 128), batches=[])
    seen = set()
    for line in lines[1:-1]:
        match = BATCH.fullmatch(line)
        if match is None:
            raise ValueError("malformed profile batch")
        groups = match.groups()
        rep, level, measured = int(groups[0]), int(groups[1]), groups[2] == "true"
        key = (rep, level, measured)
        if rep not in range(5) or level not in (4, 7) or key in seen:
            raise ValueError("unknown or duplicate profile condition")
        seen.add(key)
        memory = 32 if measured else 0
        sent = 32 if measured and level == 7 else 0
        batch = dict(rep=rep, level=level, measured=measured)
        for index, (name, count) in enumerate(zip(
            ("elapsed", "memory", "lock_wait", "locked_send"), (1, memory, sent, sent)
        )):
            begin = 3 + index * 4
            batch[name] = _stats(groups[begin:begin + 4], count)
        if batch["elapsed"]["total_ticks"] == 0:
            raise ValueError("zero elapsed batch duration")
        batch["attempted_bytes"] = int(groups[-1])
        if bool(batch["attempted_bytes"]) != bool(sent):
            raise ValueError("attempted output missing or unexpected")
        result["batches"].append(batch)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript", type=Path)
    parser.add_argument("--exit-code", required=True, type=int)
    args = parser.parse_args()
    print(json.dumps(validate(args.transcript.read_text(), args.exit_code), indent=2))


if __name__ == "__main__":
    main()
