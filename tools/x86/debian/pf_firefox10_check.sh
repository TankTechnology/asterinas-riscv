#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# 10 rounds of Firefox scrolling used as the regression for the page-fault
# atomic-mode panic (/tmp/tp/FIX_PF_PANIC.md, task C).
#
# Markers: pfx_* and DAILY_DONE=ok.

set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DISPLAY=:0
export HOME=/tmp
export MOZ_CRASHREPORTER_DISABLE=1
export MOZ_DISABLE_CONTENT_SANDBOX=1

log() { printf '%s\n' "$*" | tee -a /tmp/pf_firefox.log >/dev/console; }

log "pfx_start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

cat >/tmp/syscall_time_report.py <<'RPT'
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

RPT

cat >/tmp/syscall_sampler.py <<'SMP'
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
    274: "get_robust_list", 270: "pselect6", 271: "ppoll", 281: "epoll_pwait", 284: "eventfd",
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

SMP

run() {
    python3 -I /tmp/sock_pingpong.py "$@" 2>&1 | while IFS= read -r line; do log "$line"; done
}

run --sizes 64,65536 --iters 200000,20000 --json /tmp/spp-pingpong.json
run --mode sameproc --sizes 64 --iters 200000 --json /tmp/spp-sameproc.json
run --mode pipe --sizes 64 --iters 200000 --json /tmp/spp-pipe.json
run --mode pipepair --sizes 64 --iters 200000 --json /tmp/spp-pipepair.json


if [ ! -S /tmp/.X11-unix/X0 ]; then
    /usr/lib/xorg/Xorg :0 -noreset -nolisten tcp \
        -config /etc/X11/xorg-noudev.conf >/tmp/pff-xorg.log 2>&1 &
    i=0
    while [ "$i" -lt 30 ]; do [ -S /tmp/.X11-unix/X0 ] && break; sleep 0.5; i=$((i+1)); done
    if command -v openbox >/dev/null 2>&1; then
        openbox >/tmp/pff-openbox.log 2>&1 &
        sleep 1
    fi
fi

python3 - <<'PY2'
lines = "".join("<p>line %04d: the quick brown fox jumps over the lazy dog</p>\n" % i for i in range(1, 401))
open("/tmp/pff-page.html", "w").write("<!doctype html><meta charset=utf-8><title>lag</title><body>%s</body>" % lines)
PY2

rm -rf /tmp/pff-prof && mkdir -p /tmp/pff-prof
firefox-esr --no-remote --new-instance --profile /tmp/pff-prof /tmp/pff-page.html >/tmp/pff-ff.log 2>&1 &
ff=$!
any=""; i=0
while [ "$i" -lt 120 ]; do
    any=$(timeout 1 xdotool search --class firefox 2>/dev/null | head -1)
    [ -n "$any" ] && break
    sleep 0.5; i=$((i+1))
done
sleep 2
win=""; best=0
cands=$(timeout 2 xdotool search --name 'lag' 2>/dev/null)
[ -n "$cands" ] || cands=$(timeout 2 xdotool search --class firefox 2>/dev/null)
for w in $cands; do
    eval "$(timeout 2 xdotool getwindowgeometry --shell "$w" 2>/dev/null)"
    area=$(( ${WIDTH:-0} * ${HEIGHT:-0} ))
    if [ "$area" -gt "$best" ]; then best=$area; win=$w; fi
done
if [ -n "$win" ]; then
    timeout 3 xdotool windowsize "$win" 1024 768 2>/dev/null
fi
log "pfx_ff win=${win:-none} area=$best"

for round in $(seq 1 10); do
    printf on >/proc/syscall_time
    printf reset >/proc/syscall_time
    (
        i=0
        while [ "$i" -lt 20 ]; do
            xdotool key Page_Down 2>/dev/null
            sleep 0.5
            i=$((i+1))
        done
    ) &
    scroller=$!
    sleep 10
    kill "$scroller" 2>/dev/null
    wait "$scroller" 2>/dev/null
    python3 -I /tmp/syscall_time_report.py /proc/syscall_time --by cpu --top 5 \
        > /tmp/pff-cpu.txt 2>&1
    while IFS= read -r line; do log "pff_r$round|$line"; done < /tmp/pff-cpu.txt
    printf off >/proc/syscall_time
done

kill "$ff" 2>/dev/null
pkill -f firefox-esr 2>/dev/null
pkill -f 'Web Content' 2>/dev/null
log "pff_done $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo DAILY_DONE=ok >/dev/console
