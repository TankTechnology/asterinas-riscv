#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Socket CPU optimization acceptance run.
#
#   1. UNIX-socket ping-pong microbenchmark (pingpong / sameproc / pipe /
#      pipepair), per-syscall CPU from /proc/syscall_time.
#   2. Firefox 20 s scroll with `top10 by cpu` and `top10 by total`.
#
# Reads /tmp/sock_pingpong.py (embedded below) and /tmp/syscall_time_report.py
# (embedded) so no repository is needed inside the guest.
#
# Markers: spp_*, soc_*, and DAILY_DONE=ok.

set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DISPLAY=:0
export HOME=/tmp
export MOZ_CRASHREPORTER_DISABLE=1
export MOZ_DISABLE_CONTENT_SANDBOX=1

log() { printf '%s\n' "$*" | tee -a /tmp/sock_opt.log >/dev/console; }

log "soc_start $(date -u '+%Y-%m-%dT%H:%M:%SZ') python=$(python3 -V 2>&1) cpus=$(grep -c ^processor /proc/cpuinfo)"

cat >/tmp/sock_pingpong.py <<'PY'
#!/usr/bin/env python3
"""UNIX-socket ping-pong microbenchmark with per-syscall CPU attribution.

Two processes exchange one message per round trip over a
``socket.socketpair(AF_UNIX, SOCK_STREAM)``.  Each direction uses
``sendmsg``/``recvmsg`` explicitly (that is the Firefox IPC pair under
study), so the kernel-side cost can be read from ``/proc/syscall_time``:
the counters are global, so both processes are included.

``--mode`` selects what is measured:

  pingpong   two processes, one round trip per message (real cross-task wakes)
  sameproc   both socket endpoints in one process (socket path, no wakes)
  pipe       same-process pipe write+read (fd lookup + copy, no sockets)
  pipepair   two processes over pipes (pipes with real cross-task wakes)

Per measurement the tool prints:

  spp mode=<m> size=<bytes> n=<round trips> rt_per_s=<round trips/s>
      wall_ns_rt=<ns/round trip> [sendmsg/recvmsg counters]
      cpu_ns_rt=<ns CPU per round trip> cpu_ns_syscall=<ns CPU per syscall>
      blocked_ns_rt=<ns parked per round trip> cpu_parent=<id> cpu_child=<id>

``cpu_ns_rt`` is ``total − blocked`` accumulated by the kernel, i.e. it
excludes the time either side spent descheduled.  ``blocked_ns_rt`` is the
excluded part, so a run that really blocks shows it.

Usage:
  sock_pingpong.py [--sizes 64,4096,65536] [--iters 200000,100000,20000]
                   [--warmup 2000] [--mode pingpong] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time

SENDMSG = 46
RECVMSG = 47


def read_counters() -> dict[int, tuple[int, int, int, int]]:
    """nr -> (count, total_ns, blocked_ns, cpu_ns) for every syscall seen."""
    out: dict[int, tuple[int, int, int, int]] = {}
    try:
        with open("/proc/syscall_time", "r") as fh:
            for line in fh:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.split()
                if len(parts) == 6:
                    nr, count, total, blocked, cpu, _avg = (int(x) for x in parts)
                elif len(parts) == 4:
                    nr, count, total, _avg = (int(x) for x in parts)
                    blocked, cpu = 0, total
                else:
                    continue
                out[nr] = (count, total, blocked, cpu)
    except OSError:
        pass
    return out


def snapshot() -> tuple[dict, float]:
    ctl("on")
    ctl("reset")
    return read_counters(), time.monotonic()


def finish(before: dict, started: float, nrs: tuple[int, ...]):
    after = read_counters()
    elapsed = time.monotonic() - started
    ctl("off")
    acc = {"count": 0, "total": 0, "blocked": 0, "cpu": 0}
    per = {}
    for nr in nrs:
        b = before.get(nr, (0, 0, 0, 0))
        a = after.get(nr, (0, 0, 0, 0))
        d = tuple(y - x for x, y in zip(b, a))
        per[nr] = d
        for key, val in zip(("count", "total", "blocked", "cpu"), d):
            acc[key] += val
    return acc, per, elapsed


def ctl(command: str) -> None:
    try:
        with open("/proc/syscall_time", "w") as fh:
            fh.write(command)
    except OSError:
        pass


def cpu_of(pid: int) -> str:
    """The processor field of /proc/<pid>/stat (field 39 after the comm)."""
    try:
        with open(f"/proc/{pid}/stat", "r") as fh:
            data = fh.read()
        return data[data.rindex(")") + 2:].split()[36]
    except (OSError, IndexError):
        return "?"


def recv_exact(sock: socket.socket, size: int) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        msg, _anc, flags, _addr = sock.recvmsg(size - len(buf))
        if not msg and flags & getattr(socket, "MSG_EOF", 0x80):
            raise RuntimeError("peer closed")
        buf += msg
    return bytes(buf)


def summary(mode: str, size: int, iters: int, acc: dict, per: dict, elapsed: float,
            parent_cpu: str = "?", child_cpu: str = "?") -> dict:
    row = {
        "mode": mode,
        "size": size,
        "iters": iters,
        "wall_ns_rt": elapsed / iters * 1e9 if iters else 0.0,
        "rt_per_s": iters / elapsed if elapsed else 0.0,
        "cpu_ns_rt": acc["cpu"] / iters if iters else 0.0,
        "blocked_ns_rt": acc["blocked"] / iters if iters else 0.0,
        "cpu_ns_syscall": acc["cpu"] / acc["count"] if acc["count"] else 0.0,
        "syscalls": acc["count"],
        "per_syscall": {str(nr): d for nr, d in per.items()},
        "cpu_parent": parent_cpu,
        "cpu_child": child_cpu,
    }
    print(
        f"spp mode={mode} size={size} n={iters} wall_ns_rt={row['wall_ns_rt']:.1f} "
        f"rt_per_s={row['rt_per_s']:.0f} syscalls={acc['count']} "
        f"cpu_ns_rt={row['cpu_ns_rt']:.1f} blocked_ns_rt={row['blocked_ns_rt']:.1f} "
        f"cpu_ns_syscall={row['cpu_ns_syscall']:.1f} "
        f"cpu_parent={parent_cpu} cpu_child={child_cpu}",
        flush=True,
    )
    return row


def run_pingpong(size: int, iters: int, warmup: int) -> dict:
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    payload = bytes((i * 7 + 3) & 0xFF for i in range(size))
    pid = os.fork()
    if pid == 0:  # child: receive a message, echo it back
        try:
            parent.close()
            for _ in range(warmup + iters):
                data = recv_exact(child, size)
                child.sendmsg([data])
        finally:
            os._exit(0)

    child.close()
    try:
        for _ in range(warmup):
            if parent.sendmsg([payload]) != size:
                raise RuntimeError("short sendmsg during warmup")
            recv_exact(parent, size)

        before, started = snapshot()
        for _ in range(iters):
            if parent.sendmsg([payload]) != size:
                raise RuntimeError("short sendmsg")
            data = recv_exact(parent, size)
        acc, per, elapsed = finish(before, started, (SENDMSG, RECVMSG))

        if data != payload:
            raise RuntimeError("payload mismatch")
        return summary("pingpong", size, iters, acc, per, elapsed,
                       cpu_of(os.getpid()), cpu_of(pid))
    finally:
        parent.close()
        os.waitpid(pid, 0)


def run_sameproc(size: int, iters: int, warmup: int) -> dict:
    """Both endpoints in one process: socket path without cross-task wakes."""
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    payload = bytes((i * 7 + 3) & 0xFF for i in range(size))
    try:
        for _ in range(warmup):
            if a.sendmsg([payload]) != size:
                raise RuntimeError("short sendmsg during warmup")
            recv_exact(b, size)

        before, started = snapshot()
        for _ in range(iters):
            if a.sendmsg([payload]) != size:
                raise RuntimeError("short sendmsg")
            recv_exact(b, size)
        acc, per, elapsed = finish(before, started, (SENDMSG, RECVMSG))
        return summary("sameproc", size, iters, acc, per, elapsed, cpu_of(os.getpid()))
    finally:
        a.close()
        b.close()


def run_pipe(size: int, iters: int, warmup: int) -> dict:
    """Same-process pipe write+read: fd lookup + user copy, no socket path."""
    read_fd, write_fd = os.pipe()
    payload = bytes((i * 7 + 3) & 0xFF for i in range(size))
    try:
        for _ in range(warmup):
            os.write(write_fd, payload)
            if len(os.read(read_fd, size)) != size:
                raise RuntimeError("short pipe read during warmup")

        before, started = snapshot()
        for _ in range(iters):
            if os.write(write_fd, payload) != size:
                raise RuntimeError("short write")
            if len(os.read(read_fd, size)) != size:
                raise RuntimeError("short read")
        acc, per, elapsed = finish(before, started, (0, 1))
        return summary("pipe", size, iters, acc, per, elapsed, cpu_of(os.getpid()))
    finally:
        os.close(read_fd)
        os.close(write_fd)


def run_pipe_pair(size: int, iters: int, warmup: int) -> dict:
    """Two processes over pipes: the wake/block cost without any socket code."""
    up_r, up_w = os.pipe()      # parent -> child
    down_r, down_w = os.pipe()  # child -> parent
    payload = bytes((i * 7 + 3) & 0xFF for i in range(size))
    pid = os.fork()
    if pid == 0:
        try:
            os.close(up_w)
            os.close(down_r)
            for _ in range(warmup + iters):
                data = b""
                while len(data) < size:
                    data += os.read(up_r, size - len(data))
                os.write(down_w, data)
        finally:
            os._exit(0)

    os.close(up_r)
    os.close(down_w)
    try:
        for _ in range(warmup):
            os.write(up_w, payload)
            if len(os.read(down_r, size)) != size:
                raise RuntimeError("short pipe read during warmup")

        before, started = snapshot()
        for _ in range(iters):
            if os.write(up_w, payload) != size:
                raise RuntimeError("short write")
            if len(os.read(down_r, size)) != size:
                raise RuntimeError("short read")
        acc, per, elapsed = finish(before, started, (0, 1))
        return summary("pipepair", size, iters, acc, per, elapsed,
                       cpu_of(os.getpid()), cpu_of(pid))
    finally:
        os.close(up_w)
        os.close(down_r)
        os.waitpid(pid, 0)


MODES = {
    "pingpong": run_pingpong,
    "sameproc": run_sameproc,
    "pipe": run_pipe,
    "pipepair": run_pipe_pair,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="64,4096,65536")
    parser.add_argument("--iters", default="200000,100000,20000")
    parser.add_argument("--warmup", type=int, default=2000)
    parser.add_argument("--mode", default="pingpong", choices=tuple(MODES))
    parser.add_argument("--json")
    args = parser.parse_args(argv)

    sizes = [int(x) for x in args.sizes.split(",")]
    iter_list = [int(x) for x in args.iters.split(",")]
    if len(iter_list) == 1:
        iter_list *= len(sizes)
    if len(iter_list) != len(sizes):
        parser.error("--sizes and --iters must have the same length (or one iter count)")

    rows = [MODES[args.mode](size, iters, args.warmup)
            for size, iters in zip(sizes, iter_list)]

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(rows, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())

PY

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

# --- Firefox: top10 by cpu and by total ------------------------------------
if [ ! -S /tmp/.X11-unix/X0 ]; then
    /usr/lib/xorg/Xorg :0 -noreset -nolisten tcp \
        -config /etc/X11/xorg-noudev.conf >/tmp/soc-xorg.log 2>&1 &
    i=0
    while [ "$i" -lt 30 ]; do [ -S /tmp/.X11-unix/X0 ] && break; sleep 0.5; i=$((i+1)); done
    # Without a window manager Firefox creates a tiny 10x10 toplevel.
    if command -v openbox >/dev/null 2>&1; then
        openbox >/tmp/cpc-openbox.log 2>&1 &
        sleep 1
    fi
fi
python3 - <<'PY2'
lines = "".join("<p>line %04d: the quick brown fox jumps over the lazy dog</p>\n" % i for i in range(1, 401))
open("/tmp/soc-page.html", "w").write("<!doctype html><meta charset=utf-8><title>lag</title><body>%s</body>" % lines)
PY2
rm -rf /tmp/soc-prof && mkdir -p /tmp/soc-prof
firefox-esr --no-remote --new-instance --profile /tmp/soc-prof /tmp/soc-page.html >/tmp/soc-ff.log 2>&1 &
ff=$!
any=""; i=0
while [ "$i" -lt 120 ]; do
    any=$(timeout 1 xdotool search --class firefox 2>/dev/null | head -1)
    [ -n "$any" ] && break
    sleep 0.5; i=$((i+1))
done
sleep 1
win=""; best=0
cands=$(timeout 2 xdotool search --name 'lag' 2>/dev/null)
[ -n "$cands" ] || cands=$(timeout 2 xdotool search --class firefox 2>/dev/null)
for w in $cands; do
    eval "$(timeout 2 xdotool getwindowgeometry --shell "$w" 2>/dev/null)"
    area=$(( ${WIDTH:-0} * ${HEIGHT:-0} ))
    if [ "$area" -gt "$best" ]; then best=$area; win=$w; fi
done
if [ -n "$win" ]; then
    # Without a WM Firefox may create a 10x10 toplevel; force a real size.
    timeout 3 xdotool windowsize "$win" 1024 768 2>/dev/null
    sleep 1
fi
log "soc_ff win=${win:-none} area=$best geom=$(timeout 2 xdotool getwindowgeometry --shell "$win" 2>/dev/null | tr '\n' ' ' | head -c 120)"
if [ -n "$win" ]; then
    timeout 3 xdotool windowactivate "$win" 2>/dev/null
    sleep 2
    printf on >/proc/syscall_time
    printf reset >/proc/syscall_time
    (
        i=0
        while [ "$i" -lt 40 ]; do
            xdotool key Page_Down 2>/dev/null
            sleep 0.5
            i=$((i+1))
        done
    ) &
    scroller=$!
    sleep 20
    kill "$scroller" 2>/dev/null
    wait "$scroller" 2>/dev/null
    python3 -I /tmp/syscall_time_report.py /proc/syscall_time --by cpu --top 10 \
        --json /tmp/soc-cpu.json > /tmp/soc-cpu.txt 2>&1
    python3 -I /tmp/syscall_time_report.py /proc/syscall_time --by total --top 10 \
        --json /tmp/soc-total.json > /tmp/soc-total.txt 2>&1
    while IFS= read -r line; do log "soc_cpu|$line"; done < /tmp/soc-cpu.txt
    while IFS= read -r line; do log "soc_total|$line"; done < /tmp/soc-total.txt
fi
kill "$ff" 2>/dev/null
pkill -f firefox-esr 2>/dev/null
pkill -f 'Web Content' 2>/dev/null
printf off >/proc/syscall_time
log "soc_done $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo DAILY_DONE=ok >/dev/console
