#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""One-shot desktop performance baseline for the Megrez board.

Pushes two self-timing guest scripts through the console bridge, runs them, and
writes one comparison table. Nothing is measured host-side: the guest times its
own syscalls, IPC, X round trip and redraw rate, so a slow serial line cannot
pollute the numbers.

    megrez_desktop_baseline.py --label release
    megrez_desktop_baseline.py --label dev --skip-x11 --compare

Results accumulate in --json (default /tmp/megrez-baseline.json); --compare
prints every recorded run against the most recent one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "board"))

from megrez_console import Console  # noqa: E402

HERE = Path(__file__).resolve().parent
GUEST_SCRIPTS = (
    (HERE / "diagnostics" / "syscall_ipc_bench_guest.py", "/tmp/w1_guest.py"),
    (HERE / "diagnostics" / "x11_bench_guest.sh", "/tmp/w1_x11.sh"),
)

# Order matters for the comparison table.
METRICS = (
    "ctrl_us",
    "getpid_us",
    "getuid_us",
    "clock_us",
    "write_us",
    "pipe_us",
    "sockpair_us",
    "sockrtt_us",
    "x_sync_ms",
    "scroll_per_s",
)


def collect(con: Console, command: str, done: str, timeout: float) -> str:
    # Take the offset before typing: the guest starts printing well inside the
    # second the typing helper spends pacing characters.
    offset = os.path.getsize(con.log)
    con.type_line(command)
    deadline = time.time() + timeout
    text = ""
    while time.time() < deadline:
        time.sleep(2)
        chunk, offset = con.read_since(offset)
        text += chunk
        if done in text:
            break
    return text


def parse(text: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for name in METRICS:
        match = re.search(rf"M_{name}\s+([0-9.]+)", text)
        if match:
            values[name] = float(match.group(1))
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="run")
    parser.add_argument("--log", default="/tmp/board.log")
    parser.add_argument("--fifo", default="/tmp/board.cmd")
    parser.add_argument("--json", default="/tmp/megrez-baseline.json")
    parser.add_argument("--push-pace", type=float, default=0.15)
    parser.add_argument("--skip-x11", action="store_true")
    parser.add_argument("--compare", action="store_true")
    args = parser.parse_args()

    con = Console(args.log, args.fifo)
    for local, remote in GUEST_SCRIPTS:
        if not con.push(str(local), remote, args.push_pace):
            print(f"push failed: {local}", file=sys.stderr)
            return 1

    print("[1/2] syscall and IPC cost", flush=True)
    values = parse(collect(con, "python3 /tmp/w1_guest.py", "ZZBENCHDONE", 180))
    if not args.skip_x11:
        print("[2/2] X round trip and redraw", flush=True)
        values.update(parse(collect(con, "sh /tmp/w1_x11.sh", "ZZX11DONE", 240)))

    record = {"label": args.label, "measured_at": time.strftime("%Y-%m-%d %H:%M:%S"), **values}
    path = Path(args.json)
    history = json.loads(path.read_text()) if path.exists() else []
    history.append(record)
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2))

    print(f"\n=== Megrez baseline: {args.label} ===")
    print(f"{'metric':<16}{'value':>12}")
    for name in METRICS:
        value = values.get(name)
        print(f"{name:<16}{'-' if value is None else f'{value:g}':>12}")

    if args.compare and len(history) > 1:
        print("\n=== history ===")
        labels = [entry["label"] for entry in history]
        print(f"{'metric':<16}" + "".join(f"{label:>14}" for label in labels))
        for name in METRICS:
            cells = "".join(
                f"{'-' if entry.get(name) is None else f'{entry[name]:g}':>14}" for entry in history
            )
            print(f"{name:<16}{cells}")

    print("saved:", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
