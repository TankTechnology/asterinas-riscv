#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# UNIX-socket ping-pong microbenchmark (task A of SOCK_OPT).
#
# Runs /tmp/sock_pingpong.py in several modes:
#   pingpong   two processes over a socketpair (real cross-task wakes)
#   sameproc   both socket endpoints in one process (no wakes)
#   pipe       same-process pipe write+read (no socket code)
#   pipepair   two processes over pipes (wakes without socket code)
# and logs one `spp ...` line per measurement with the per-syscall CPU and
# blocked time read from /proc/syscall_time.
#
# Markers: spp_* and DAILY_DONE=ok.

set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

log() { printf '%s\n' "$*" | tee -a /tmp/sock_pingpong.log >/dev/console; }

log "spp_start $(date -u '+%Y-%m-%dT%H:%M:%SZ') python=$(python3 -V 2>&1) cpus=$(grep -c ^processor /proc/cpuinfo)"

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

if ! command -v python3 >/dev/null 2>&1; then
    log "spp_error no-python3"
    echo DAILY_DONE=fail >/dev/console
    exit
fi

run() {
    python3 -I /tmp/sock_pingpong.py "$@" 2>&1 | while IFS= read -r line; do log "$line"; done
}

run --sizes 64,65536 --iters 200000,20000 --json /tmp/spp-pingpong.json
run --mode sameproc --sizes 64 --iters 200000 --json /tmp/spp-sameproc.json
run --mode pipe --sizes 64 --iters 200000 --json /tmp/spp-pipe.json
run --mode pipepair --sizes 64 --iters 200000 --json /tmp/spp-pipepair.json

log "spp_done $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo DAILY_DONE=ok >/dev/console
