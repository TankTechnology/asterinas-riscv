#!/usr/bin/env python3
"""Sample /proc/<pid>/syscall and /proc/<pid>/stat to attribute sys time.

For every target process (one pid or all processes whose `comm` matches a
name pattern) the sampler reads, every interval:

  /proc/<pid>/syscall  -> syscall number, or "running" in user mode
  /proc/<pid>/stat     -> minflt, majflt, utime, stime

It prints a syscall histogram (with x86_64 names), the minflt/majflt
increments and the utime/stime increment with the sys:user ratio, and writes
CSV + Markdown copies.

This replaces strace on kernels without working PTRACE_ATTACH.

Usage:
  syscall_sampler.py --name firefox --duration 20 --interval 0.02 --out /tmp/attr
  syscall_sampler.py --pid 1234 --duration 5
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter

# x86_64 syscall numbers (subset that desktop workloads actually hit).
NAMES = {
    0: "read", 1: "write", 2: "open", 3: "close", 4: "stat", 5: "fstat",
    6: "lstat", 7: "poll", 8: "lseek", 9: "mmap", 10: "mprotect",
    11: "munmap", 12: "brk", 13: "rt_sigaction", 14: "rt_sigprocmask",
    15: "rt_sigreturn", 16: "ioctl", 17: "pread64", 18: "pwrite64",
    19: "readv", 20: "writev", 21: "access", 22: "pipe", 23: "select",
    24: "sched_yield", 25: "mremap", 26: "msync", 27: "mincore",
    28: "madvise", 32: "dup", 33: "dup2", 35: "nanosleep", 39: "getpid",
    40: "sendfile", 41: "socket", 42: "connect", 43: "accept",
    44: "sendto", 45: "recvfrom", 46: "sendmsg", 47: "recvmsg",
    48: "shutdown", 49: "bind", 50: "listen", 51: "getsockname",
    52: "getpeername", 53: "socketpair", 54: "setsockopt",
    55: "getsockopt", 56: "clone", 57: "fork", 59: "execve", 60: "exit",
    61: "wait4", 62: "kill", 63: "uname", 72: "fcntl", 73: "flock",
    74: "fsync", 75: "fdatasync", 77: "ftruncate", 78: "getdents",
    79: "getcwd", 80: "chdir", 83: "mkdir", 87: "unlink", 89: "readlink",
    90: "chmod", 92: "chown", 95: "umask", 96: "gettimeofday",
    99: "sysinfo", 102: "getuid", 104: "getgid", 107: "geteuid",
    108: "getegid", 110: "getppid", 131: "sigaltstack", 137: "statfs",
    157: "prctl", 158: "arch_prctl", 186: "gettid", 202: "futex",
    203: "sched_setaffinity", 204: "sched_getaffinity",
    217: "getdents64", 218: "set_tid_address", 219: "restart_syscall",
    221: "fadvise64", 228: "clock_gettime", 229: "clock_getres",
    230: "clock_nanosleep", 231: "exit_group", 232: "epoll_wait",
    233: "epoll_ctl", 234: "tgkill", 247: "waitid", 257: "openat",
    262: "newfstatat", 263: "unlinkat", 264: "renameat", 273: "set_robust_list",
    274: "get_robust_list", 281: "epoll_pwait", 284: "eventfd",
    288: "accept4", 290: "eventfd2", 291: "epoll_create1", 292: "dup3",
    293: "pipe2", 295: "preadv", 296: "pwritev", 302: "prlimit64",
    307: "sendmmsg", 309: "getcpu", 318: "getrandom", 319: "memfd_create",
    322: "execveat", 324: "membarrier", 326: "copy_file_range",
    327: "preadv2", 328: "pwritev2", 332: "statx", 334: "rseq",
    435: "clone3", 437: "openat2", 439: "faccessat2", 441: "epoll_pwait2",
    449: "futex_waitv", 454: "futex_wake", 455: "futex_wait",
    456: "futex_requeue",
}

NAME_PATTERNS = (
    "firefox",
    "Web Content",
    "Isolated Web Co",
    "RDD",
    "Socket Thread",
    "Privileged Cont",
    "Utility Process",
    "Forkserver",
)


def syscall_name(category: str) -> str:
    if category == "running":
        return "running"
    try:
        nr = int(category)
    except ValueError:
        return category
    return NAMES.get(nr, f"syscall_{nr}")


def target_pids(name: str | None, pid: int | None) -> set[int]:
    if pid is not None:
        return {pid}
    pids: set[int] = set()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm", "r") as fh:
                comm = fh.read().strip()
        except OSError:
            continue
        if any(comm.startswith(pat) for pat in NAME_PATTERNS):
            pids.add(int(entry))
    return pids


def read_syscall(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/syscall", "r") as fh:
            text = fh.read().strip()
    except (OSError, ProcessLookupError):
        return None
    if not text:
        return "running"
    return text.split()[0]


def read_stat(pid: int) -> tuple[int, int, int, int] | None:
    """(utime, stime, minflt, majflt) in clock ticks / counts."""
    try:
        with open(f"/proc/{pid}/stat", "r") as fh:
            data = fh.read()
    except (OSError, ProcessLookupError):
        return None
    rest = data[data.rindex(")") + 2:].split()
    try:
        return int(rest[11]), int(rest[12]), int(rest[7]), int(rest[9])
    except (IndexError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--name", default="firefox")
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--interval", type=float, default=0.02)
    parser.add_argument("--out")
    args = parser.parse_args(argv)

    pids = target_pids(args.name, args.pid)
    if not pids:
        print("no matching processes", file=sys.stderr)
        return 1
    first: dict[int, tuple[int, int, int, int]] = {}
    last: dict[int, tuple[int, int, int, int]] = {}
    for pid in pids:
        stat = read_stat(pid)
        if stat is not None:
            first[pid] = stat

    histogram: Counter[str] = Counter()
    samples = 0
    started = time.monotonic()
    next_discovery = started
    while time.monotonic() - started < args.duration:
        now = time.monotonic()
        if args.pid is None and now >= next_discovery:
            pids |= target_pids(args.name, None)
            next_discovery = now + 0.5
        for pid in pids:
            category = read_syscall(pid)
            stat = read_stat(pid)
            if category is None and stat is None:
                continue
            if category is not None:
                histogram[category] += 1
                samples += 1
            if stat is not None:
                if pid not in first:
                    first[pid] = stat
                last[pid] = stat
        time.sleep(args.interval)
    elapsed = time.monotonic() - started

    d_ut = d_st = d_mf = d_Mf = 0
    for pid, start in first.items():
        end = last.get(pid)
        if end is None:
            continue
        d_ut += end[0] - start[0]
        d_st += end[1] - start[1]
        d_mf += end[2] - start[2]
        d_Mf += end[3] - start[3]

    total = sum(histogram.values()) or 1
    rows = [(cat, count, 100.0 * count / total)
            for cat, count in histogram.most_common()]
    lines = [
        f"samples={samples} duration={elapsed:.2f}s interval={args.interval}s pids={len(first)}",
        f"delta user_ticks={d_ut} sys_ticks={d_st} sys_user_ratio="
        f"{(d_st / d_ut) if d_ut else float('inf'):.2f} minflt={d_mf} majflt={d_Mf}",
    ]
    for cat, count, pct in rows[:10]:
        lines.append(f"SYSCALL {cat} {syscall_name(cat)} count={count} pct={pct:.1f}")
    print("\n".join(lines))

    if args.out:
        with open(f"{args.out}.csv", "w") as fh:
            fh.write("syscall,nr,name,count,percent\n")
            for cat, count, pct in rows:
                fh.write(f'"{cat}",{cat if cat != "running" else ""},'
                         f'"{syscall_name(cat)}",{count},{pct:.3f}\n')
        with open(f"{args.out}.md", "w") as fh:
            fh.write(f"# syscall sample: {args.name if args.pid is None else args.pid}\n\n")
            fh.write(f"- samples={samples}, duration={elapsed:.2f}s, "
                     f"interval={args.interval}s, pids={len(first)}\n")
            fh.write(f"- user_ticks={d_ut}, sys_ticks={d_st}, "
                     f"sys:user={(d_st / d_ut) if d_ut else float('inf'):.2f}\n")
            fh.write(f"- minflt={d_mf}, majflt={d_Mf}\n\n")
            fh.write("| # | syscall | nr | count | % |\n|---|---|---|---|---|\n")
            for idx, (cat, count, pct) in enumerate(rows[:10], 1):
                fh.write(f"| {idx} | {syscall_name(cat)} | {cat} | {count} | {pct:.1f}% |\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
