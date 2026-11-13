#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Asterinas vs Linux ratios on the board, checked against a recorded baseline.

The baseline (`board_al_baseline.json`) holds one measurement of the vendor
Linux kernel and one of Asterinas on the same board, same rootfs and same
static-musl benchmarks. This gate runs the Asterinas side again and fails when
a ratio the baseline pinned down has regressed.

    tools/riscv/perf/al_gate.py                 # run over SSH and check
    tools/riscv/perf/al_gate.py --from-file out # check a captured output

Only the three ratios the baseline gates on are compared: a path syscall
(`stat_path`), 4 KiB random I/O (`io4k`) and process creation
(`fork_exec_wait`). Everything else is reported for the record.

Why the ratios and not the raw numbers: the board's own timings drift by tens
of percent between boots (cache and clock state), so an absolute threshold
would flap. The Linux column was measured once and is frozen; the Asterinas
column is re-measured and compared to it.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASELINE = HERE / "board_al_baseline.json"

SSH_KEY = "~/.ssh/asterinas_megrez"
SSH_PORT = "2222"
SSH_TARGET = "root@58.206.232.122"


def run_remote() -> str:
    """Runs the two probes on the board and returns their output."""
    setup = (
        "cd /tmp && "
        "test -x ./board_l1 || exit 9; "
        "test -x ./io4k || exit 9; "
        "sync; ./board_l1; ./io4k /tmp/io4k.bin"
    )
    result = subprocess.run(
        ["ssh", "-i", SSH_KEY, "-p", SSH_PORT, "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null", "-o", "BatchMode=yes", SSH_TARGET, setup],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        sys.exit(f"benchmark run failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


def parse(text: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for name, value in re.findall(r"([a-z0-9_]+)=(-?[0-9.]+)", text):
        values[name] = float(value)
    return values


def ratio(name: str, measured: float, baseline: dict) -> float:
    linux = baseline["linux"][name]
    if name.endswith("_iops") or name.endswith("_MiB_s") or name.endswith("_Mops_s"):
        return linux / measured if measured else float("inf")
    return measured / linux if linux else float("inf")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-file", help="check a captured benchmark output instead of running")
    parser.add_argument("--baseline", default=str(BASELINE))
    args = parser.parse_args()

    baseline = json.loads(Path(args.baseline).read_text())
    text = Path(args.from_file).read_text() if args.from_file else run_remote()
    values = parse(text)
    if "stat_path" not in values or "fork_exec_wait" not in values:
        sys.exit("benchmark output is missing stat_path/fork_exec_wait")

    gates = baseline["gates"]
    checks = [
        ("syscall", "stat_path_us", values.get("stat_path", 0.0), gates["syscall_ratio_max"]),
        ("io4k-write", "io4k_write_iops", values.get("io4k_write", 0.0), gates["io4k_ratio_max"]),
        ("io4k-read", "io4k_read_iops", values.get("io4k_read", 0.0), gates["io4k_ratio_max"]),
        ("fork+exec", "fork_exec_wait_ms", values.get("fork_exec_wait", 0.0), gates["fork_exec_ratio_max"]),
    ]

    failures = 0
    print(f"{'gate':<12}{'metric':<20}{'measured':>12}{'ratio':>9}{'limit':>8}  verdict")
    for label, key, measured, limit in checks:
        if measured <= 0:
            print(f"{label:<12}{key:<20}{'n/a':>12}{'-':>9}{limit:>8}  SKIP")
            continue
        r = ratio(key, measured, baseline)
        ok = r <= limit
        failures += 0 if ok else 1
        print(f"{label:<12}{key:<20}{measured:>12.3f}{r:>9.2f}{limit:>8.1f}  {'ok' if ok else 'REGRESSED'}")

    print("\nother metrics (not gated, for the record):")
    for key in ("getpid_us", "open_close_us", "fstat_fd_us", "pipe_rtt_us",
                "fs_create_sync_us", "fs_unlink_us", "fs_write_MiB_s", "fs_read_MiB_s"):
        short = key.rsplit("_", 1)[0]
        measured = values.get(short)
        if measured:
            print(f"  {key:<20}{measured:>12.3f}  ratio {ratio(key, measured, baseline):.2f}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
