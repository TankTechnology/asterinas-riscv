#!/usr/bin/env python3
"""Report per-syscall CPU time from /proc/syscall_time.

Reads the kernel counters written by the syscall dispatch path:

  # enabled=<0|1>
  <nr> <count> <total_ns> <avg_ns>

and prints the top-N syscalls by total time with x86_64 names and the share
of the total.  Counters must be enabled (`echo on > /proc/syscall_time`) and
are usually reset before the workload (`echo reset > /proc/syscall_time`).

Usage:
  syscall_time_report.py [/proc/syscall_time] [--top 10] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Reuse the name table from the sampler when both files sit next to each other.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from syscall_sampler import syscall_name
except Exception:  # pragma: no cover - fallback for a standalone copy

    def syscall_name(nr: str) -> str:
        return f"syscall_{nr}"


def parse(path: str) -> tuple[int, list[tuple[int, int, int, int, int, int]]]:
    enabled = 0
    rows: list[tuple[int, int, int, int, int, int]] = []
    with open(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                if "enabled=" in line:
                    enabled = int(line.split("enabled=", 1)[1].split()[0] or "0")
                continue
            parts = line.split()
            if len(parts) == 6:
                nr, count, total_ns, blocked_ns, cpu_ns, avg_cpu_ns = (int(x) for x in parts)
            elif len(parts) == 4:
                nr, count, total_ns, avg_cpu_ns = (int(x) for x in parts)
                blocked_ns, cpu_ns = 0, total_ns
            else:
                continue
            rows.append((nr, count, total_ns, blocked_ns, cpu_ns, avg_cpu_ns))
    return enabled, rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default="/proc/syscall_time")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--by", choices=("cpu", "total"), default="cpu")
    parser.add_argument("--json")
    args = parser.parse_args(argv)

    enabled, rows = parse(args.path)
    metric = 4 if args.by == "cpu" else 2
    rows.sort(key=lambda row: row[metric], reverse=True)
    total = sum(row[metric] for row in rows) or 1

    print(f"enabled={enabled} by={args.by} syscalls={len(rows)} total_ns={total}")
    print("| # | syscall | nr | count | total_ms | blocked_ms | cpu_ms | avg_cpu_us | % |")
    print("|---|---|---|---|---|---|---|---|---|")
    top = []
    for idx, (nr, count, total_ns, blocked_ns, cpu_ns, avg_cpu_ns) in enumerate(rows[: args.top], 1):
        pct = 100.0 * (cpu_ns if args.by == "cpu" else total_ns) / total
        name = syscall_name(str(nr))
        print(f"| {idx} | {name} | {nr} | {count} | {total_ns / 1e6:.1f} "
              f"| {blocked_ns / 1e6:.1f} | {cpu_ns / 1e6:.1f} "
              f"| {avg_cpu_ns / 1e3:.2f} | {pct:.1f}% |")
        top.append({"rank": idx, "nr": nr, "name": name, "count": count,
                    "total_ns": total_ns, "blocked_ns": blocked_ns,
                    "cpu_ns": cpu_ns, "avg_cpu_ns": avg_cpu_ns,
                    "pct": round(pct, 3)})

    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"enabled": enabled, "by": args.by, "total_ns": total,
                       "top": top}, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
