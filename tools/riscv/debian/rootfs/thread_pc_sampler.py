#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Take bounded user-PC samples from named threads with Linux ptrace regsets.

Each sample stops and detaches one thread. The recorded PC is its saved user
context, not an in-kernel instruction pointer or a call stack. Run an identical
workload without this sampler to measure the sampling overhead.
"""

import argparse
import ctypes
import json
import os
import platform
import sys
import time
from pathlib import Path


PTRACE_ATTACH = 16
PTRACE_DETACH = 17
PTRACE_GETREGSET = 0x4204
NT_PRSTATUS = 1
WAIT_ALL_THREADS = 0x40000000
STOP_TIMEOUT_SECONDS = 2.0


class IOVec(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("length", ctypes.c_size_t)]


LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.ptrace.argtypes = [
    ctypes.c_uint,
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_void_p,
]
LIBC.ptrace.restype = ctypes.c_long


def ptrace(request: int, tid: int, addr: int = 0, data: ctypes.c_void_p | None = None) -> None:
    result = LIBC.ptrace(request, tid, ctypes.c_void_p(addr), data)
    if result == -1:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), f"ptrace request {request} tid {tid}")


def matching_threads(pid: int, names: set[str]) -> list[tuple[int, str]]:
    task_dir = Path(f"/proc/{pid}/task")
    matches = []
    for thread_dir in task_dir.iterdir():
        try:
            name = (thread_dir / "comm").read_text().strip()
        except FileNotFoundError:
            continue
        if name in names:
            matches.append((int(thread_dir.name), name))
    return sorted(matches)


def wait_for_ptrace_stop(tid: int) -> None:
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        waited_tid, status = os.waitpid(tid, os.WNOHANG | WAIT_ALL_THREADS)
        if waited_tid == tid:
            if not os.WIFSTOPPED(status):
                raise RuntimeError(f"thread {tid} exited during ptrace attach: {status}")
            return
        time.sleep(0.001)
    raise TimeoutError(f"thread {tid} did not stop within {STOP_TIMEOUT_SECONDS}s")


def sample_thread(tid: int, pc_index: int) -> tuple[int, int | None, int]:
    started_ns = time.monotonic_ns()
    stopped = False
    ptrace(PTRACE_ATTACH, tid)
    try:
        wait_for_ptrace_stop(tid)
        stopped = True
        registers = (ctypes.c_ulong * 32)()
        iov = IOVec(ctypes.cast(registers, ctypes.c_void_p), ctypes.sizeof(registers))
        ptrace(PTRACE_GETREGSET, tid, NT_PRSTATUS, ctypes.cast(ctypes.byref(iov), ctypes.c_void_p))
        if iov.length < (pc_index + 1) * ctypes.sizeof(ctypes.c_ulong):
            raise RuntimeError(f"short NT_PRSTATUS regset for thread {tid}: {iov.length}")
        pc = registers[pc_index]
        if pc == 0:
            raise RuntimeError(f"zero user PC for thread {tid}")
        ra = registers[1] if pc_index == 0 else None
    finally:
        if stopped:
            ptrace(PTRACE_DETACH, tid)
    return pc, ra, time.monotonic_ns() - started_ns


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True, help="target process/thread-group ID")
    parser.add_argument("--comm", action="append", required=True, help="exact /proc thread name")
    parser.add_argument("--samples", type=int, default=50, help="samples per matched thread, 1–200")
    parser.add_argument("--interval-ms", type=int, default=200, help="time between rounds, 10–1000 ms")
    parser.add_argument("--output", type=Path, required=True, help="new JSONL output file")
    args = parser.parse_args()

    if args.pid <= 0 or not 1 <= args.samples <= 200 or not 10 <= args.interval_ms <= 1000:
        parser.error("pid must be positive, samples 1–200, and interval-ms 10–1000")

    machine = platform.machine().lower()
    pc_index = {"riscv64": 0, "x86_64": 16}.get(machine)
    if pc_index is None:
        parser.error(f"unsupported NT_PRSTATUS layout on {machine}")

    threads = matching_threads(args.pid, set(args.comm))
    if not threads:
        parser.error(f"no matching threads in process {args.pid}: {args.comm}")
    if args.output.exists() or args.output.with_suffix(".maps").exists():
        parser.error("output or matching maps file already exists")

    maps = Path(f"/proc/{args.pid}/maps").read_text()
    args.output.with_suffix(".maps").write_text(maps)
    with args.output.open("x") as stream:
        metadata = {
            "kind": "metadata",
            "arch": machine,
            "pid": args.pid,
            "threads": [{"tid": tid, "comm": name} for tid, name in threads],
            "samples_per_thread": args.samples,
            "interval_ms": args.interval_ms,
            "maps": str(args.output.with_suffix(".maps")),
        }
        stream.write(json.dumps(metadata) + "\n")
        stream.flush()

        start_ns = time.monotonic_ns()
        for round_index in range(args.samples):
            target_ns = start_ns + round_index * args.interval_ms * 1_000_000
            remaining_ns = target_ns - time.monotonic_ns()
            if remaining_ns > 0:
                time.sleep(remaining_ns / 1_000_000_000)
            for tid, name in threads:
                pc, ra, stop_ns = sample_thread(tid, pc_index)
                stream.write(
                    json.dumps(
                        {
                            "kind": "sample",
                            "round": round_index,
                            "elapsed_ms": round((time.monotonic_ns() - start_ns) / 1_000_000, 3),
                            "tid": tid,
                            "comm": name,
                            "pc": pc,
                            "ra": ra,
                            "stop_ns": stop_ns,
                        }
                    )
                    + "\n"
                )
            stream.flush()

    print(f"THREAD_PC_SAMPLER PASS threads={len(threads)} samples={args.samples}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, TimeoutError) as error:
        print(f"THREAD_PC_SAMPLER FAIL: {error}", file=sys.stderr)
        sys.exit(1)
