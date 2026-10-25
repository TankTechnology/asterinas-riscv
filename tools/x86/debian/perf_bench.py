#!/usr/bin/env python3
"""Isolate a parallel-hash slowdown: processes, threads, locks, fork/exec.

Modes
  hash   hash every file, no output writes
  lock   hash + append one record per file to a shared file under flock,
         exactly the record path of the round-3 agent's stress_hash.py
  read   read every file, no hashing (plain I/O)
  fork   fork N children that exit immediately
  thread create/join N empty threads
  exec   spawn /bin/true N times

Every measured mode prints one JSON line with wall time, throughput, CPU
seconds and context switches (from getrusage), so the caller can tell
"waiting for CPU" from "waiting for locks or I/O".
"""

import argparse
import fcntl
import hashlib
import json
import os
import resource
import stat
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor


def discover(root):
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                entries.append((path, st.st_size))
    return entries


def hash_one(path):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    with os.fdopen(fd, "rb", buffering=0) as fh:
        return hashlib.file_digest(fh, "sha256").digest()


def read_one(path):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    total = 0
    with os.fdopen(fd, "rb", buffering=0) as fh:
        while True:
            chunk = fh.read(1 << 18)
            if not chunk:
                break
            total += len(chunk)
    return total


def run_slice(paths, mode, out_fd, thread_fd_path=None):
    fd = out_fd
    if mode == "lock_tfd":
        # Like stress_hash.py: every thread owns its own open file description.
        fd = os.open(thread_fd_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        done = 0
        for path in paths:
            if mode == "hash":
                hash_one(path)
            elif mode == "read":
                read_one(path)
            elif mode in ("lock", "lock_tfd"):
                digest = hash_one(path).hex().encode()
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    os.write(fd, digest + b"\t")
                    os.write(fd, os.fsencode(path) + b"\0")
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            done += 1
        return done
    finally:
        if mode == "lock_tfd":
            os.close(fd)


def worker(index, paths, threads, mode, out_path, report_fd):
    out_fd = -1
    if mode == "lock":
        out_fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    rss_before = resource.getrusage(resource.RUSAGE_SELF)
    started = time.monotonic()
    try:
        if threads == 1:
            done = run_slice(paths, mode, out_fd, out_path)
        else:
            slices = [paths[i::threads] for i in range(threads)]
            with ThreadPoolExecutor(max_workers=threads) as pool:
                done = sum(pool.map(
                    lambda s: run_slice(s, mode, out_fd, out_path), slices))
    finally:
        if out_fd >= 0:
            os.close(out_fd)
    elapsed = time.monotonic() - started
    rss_after = resource.getrusage(resource.RUSAGE_SELF)
    report = {
        "worker": index,
        "pid": os.getpid(),
        "files": len(paths),
        "done": done,
        "elapsed_sec": round(elapsed, 4),
        "utime": round(rss_after.ru_utime - rss_before.ru_utime, 4),
        "stime": round(rss_after.ru_stime - rss_before.ru_stime, 4),
        "nvcsw": rss_after.ru_nvcsw - rss_before.ru_nvcsw,
        "nivcsw": rss_after.ru_nivcsw - rss_before.ru_nivcsw,
        "maxrss_kb": rss_after.ru_maxrss,
    }
    os.write(report_fd, json.dumps(report).encode() + b"\n")
    os.close(report_fd)
    os._exit(0)


def run_files(args):
    entries = discover(args.root)
    paths = [p for p, _ in entries]
    total_bytes = sum(s for _, s in entries)

    if args.warm:
        for path in paths[: min(len(paths), 64)]:
            hash_one(path)

    out_path = args.out or "/tmp/perf-bench-out.bin"
    pipes = [os.pipe() for _ in range(args.procs)]
    started = time.monotonic()
    pids = []
    for index in range(args.procs):
        slice_ = paths[index :: args.procs]
        pid = os.fork()
        if pid == 0:
            for other, (r, w) in enumerate(pipes):
                if other != index:
                    os.close(r)
                    os.close(w)
            os.close(pipes[index][0])
            worker(index, slice_, args.threads, args.mode, out_path, pipes[index][1])
        pids.append(pid)
    reports = []
    for index, pid in enumerate(pids):
        os.close(pipes[index][1])
        data = b""
        while True:
            chunk = os.read(pipes[index][0], 65536)
            if not chunk:
                break
            data += chunk
        os.close(pipes[index][0])
        os.waitpid(pid, 0)
        for line in data.splitlines():
            if line.strip():
                reports.append(json.loads(line))
    elapsed = time.monotonic() - started

    cpu = sum(r["utime"] + r["stime"] for r in reports)
    nvcsw = sum(r["nvcsw"] for r in reports)
    nivcsw = sum(r["nivcsw"] for r in reports)
    summary = {
        "mode": args.mode,
        "procs": args.procs,
        "threads": args.threads,
        "files": len(paths),
        "bytes": total_bytes,
        "elapsed_sec": round(elapsed, 4),
        "files_per_sec": round(len(paths) / elapsed, 1) if elapsed else 0,
        "mib_per_sec": round(total_bytes / (1 << 20) / elapsed, 2) if elapsed else 0,
        "cpu_sec": round(cpu, 4),
        "cpu_pct_one_core": round(100.0 * cpu / elapsed, 1) if elapsed else 0,
        "nvcsw": nvcsw,
        "nivcsw": nivcsw,
        "workers": reports,
    }
    print(json.dumps(summary, ensure_ascii=False))


def run_fork(args):
    started = time.monotonic()
    pids = []
    for _ in range(args.ops):
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        pids.append(pid)
    for pid in pids:
        os.waitpid(pid, 0)
    elapsed = time.monotonic() - started
    print(json.dumps({
        "mode": "fork", "ops": args.ops, "elapsed_sec": round(elapsed, 4),
        "ops_per_sec": round(args.ops / elapsed, 1) if elapsed else 0,
    }))


def run_threads(args):
    started = time.monotonic()
    for _ in range(args.ops):
        thread = threading.Thread(target=lambda: None)
        thread.start()
        thread.join()
    elapsed = time.monotonic() - started
    print(json.dumps({
        "mode": "thread", "ops": args.ops, "elapsed_sec": round(elapsed, 4),
        "ops_per_sec": round(args.ops / elapsed, 1) if elapsed else 0,
    }))


def run_exec(args):
    started = time.monotonic()
    for _ in range(args.ops):
        pid = os.posix_spawn("/bin/true", ["/bin/true"], os.environ)
        os.waitpid(pid, 0)
    elapsed = time.monotonic() - started
    print(json.dumps({
        "mode": "exec", "ops": args.ops, "elapsed_sec": round(elapsed, 4),
        "ops_per_sec": round(args.ops / elapsed, 1) if elapsed else 0,
    }))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/tmp/perf-sample")
    parser.add_argument("--procs", type=int, default=1)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--mode", default="hash",
                        choices=["hash", "lock", "lock_tfd", "read", "fork",
                                 "thread", "exec"])
    parser.add_argument("--out")
    parser.add_argument("--ops", type=int, default=100)
    parser.add_argument("--warm", action="store_true")
    args = parser.parse_args(argv)

    if args.mode == "fork":
        run_fork(args)
    elif args.mode == "thread":
        run_threads(args)
    elif args.mode == "exec":
        run_exec(args)
    else:
        run_files(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
