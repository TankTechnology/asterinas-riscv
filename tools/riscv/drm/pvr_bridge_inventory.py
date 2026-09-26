#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Summarize the vendor PowerVR bridge calls made by a real client.

Input is the opt-in ``ASTERINAS_IOCTLTRACE_PVR_BRIDGE=1`` output from
``tools/riscv/perf/ioctltrace.c``. The outer ioctl result is *not* the bridge
operation's status; this tool deliberately does not claim that it is.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys


BRIDGE_CALL = re.compile(
    r"^IOCTL .*\bcmd=0xc0206440 bridge=(\d+) func=(\d+) "
    r"in=(\d+) out=(\d+) ret=(-?\d+)$"
)


def summarize(lines: list[str]) -> dict[str, object]:
    counts: Counter[tuple[int, int]] = Counter()
    sizes: dict[tuple[int, int], set[tuple[int, int]]] = {}
    first_seen: list[tuple[int, int]] = []
    outer_failures: list[dict[str, int]] = []

    for line_number, line in enumerate(lines, 1):
        match = BRIDGE_CALL.fullmatch(line.strip())
        if match is None:
            continue
        bridge, function, input_size, output_size, result = map(int, match.groups())
        key = (bridge, function)
        if key not in counts:
            first_seen.append(key)
            sizes[key] = set()
        counts[key] += 1
        sizes[key].add((input_size, output_size))
        if result != 0:
            outer_failures.append(
                {"line": line_number, "bridge": bridge, "function": function, "ret": result}
            )

    if not counts:
        raise ValueError("trace contains no decoded PVR_SRVKM_CMD bridge calls")

    return {
        "outer_ioctl": "0xc0206440",
        "total_calls": counts.total(),
        "distinct_functions": len(counts),
        "first_seen_order": [list(key) for key in first_seen],
        "functions": [
            {
                "bridge": bridge,
                "function": function,
                "calls": count,
                "buffer_sizes": [list(pair) for pair in sorted(sizes[(bridge, function)])],
            }
            for (bridge, function), count in sorted(
                counts.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "outer_ioctl_failures": outer_failures,
        "bridge_operation_status": "not captured by the fixed-width envelope trace",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--expect-calls", type=int)
    parser.add_argument("--expect-functions", type=int)
    args = parser.parse_args()

    try:
        summary = summarize(args.trace.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeError, ValueError) as error:
        parser.error(str(error))

    if args.expect_calls is not None and summary["total_calls"] != args.expect_calls:
        print("PVR bridge call count differs from the expected trace", file=sys.stderr)
        return 1
    if (
        args.expect_functions is not None
        and summary["distinct_functions"] != args.expect_functions
    ):
        print("PVR bridge function count differs from the expected trace", file=sys.stderr)
        return 1
    json.dump(summary, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
