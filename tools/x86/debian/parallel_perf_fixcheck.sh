#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Regression check for the flock wake-one fix (per-inode wait queue):
# the real tool and the per-thread-fd benchmark must complete the 200-file
# sample in about a second instead of ~56-95 seconds.
#
# Markers: fix_* and DAILY_DONE=ok.

set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORK=/home/asterinas/work4
SAMPLE=$WORK/perf-sample
OUT=$WORK/out
LOG=/tmp/perf-fixcheck.log
BUDGET=${PERF_BUDGET:-300}

mkdir -p "$SAMPLE" "$OUT"
: >"$LOG"
log() { printf '%s\n' "$*" | tee -a "$LOG" >/dev/console; }
now() { date +%s; }

log "perf_fixcheck start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

cat >"$WORK/perf_bench.py" <<'PY'
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
PY

cat >"$WORK/stress_hash.py" <<'PY'
#!/usr/bin/env python3
"""Parallel SHA-256 stress tool: processes + threads + per-write file locks.

Architecture
  parent  : snapshot regular files, split them into size-balanced buckets,
            fork one worker process per bucket, collect tiny JSON reports
  worker  : one process, owns a ThreadPoolExecutor (--threads workers)
  output  : every result is appended to one file as  <hex>\t<path>\0
            under fcntl.flock(LOCK_EX).  Each *thread* opens its own fd:
            flock() on a shared open-file-description is a no-op between
            threads of the same process, so a shared fd would not serialize.
  record  : deliberately appended in two write(2) calls while the lock is
            held, which makes interleaving from a broken lock visible to the
            verifier as a malformed record.

The process layer uses os.fork() + pipes instead of multiprocessing.Pool:
this sandbox mounts /dev/shm as root-only 0755, so CPython's POSIX named
semaphores fail with PermissionError before a Pool can start.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import stat
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Iterator

_TLS = threading.local()
_CONFIG: tuple[str, str, int] | None = None
_EXECUTOR: ThreadPoolExecutor | None = None


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[written:]


def _thread_fd(kind: str, path: str) -> int:
    attr = f"_fd_{kind}"
    fd = getattr(_TLS, attr, None)
    if fd is None:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC,
            0o644,
        )
        setattr(_TLS, attr, fd)
    return fd


def _append_locked(kind: str, path: str, first: bytes, second: bytes) -> None:
    """Append one two-part record atomically across threads and processes."""
    fd = _thread_fd(kind, path)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        _write_all(fd, first)
        _write_all(fd, second)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _emit_result(path: str, digest: str) -> None:
    assert _CONFIG is not None
    _append_locked(
        "results",
        _CONFIG[0],
        digest.encode("ascii") + b"\t",
        os.fsencode(path) + b"\0",
    )


def _emit_error(path: str, exc: OSError) -> None:
    assert _CONFIG is not None
    label = f"{type(exc).__name__}:{exc.errno}".encode("ascii", "replace")
    message = str(exc).encode("utf-8", "backslashreplace")
    _append_locked(
        "errors",
        _CONFIG[1],
        label + b"\t",
        os.fsencode(path) + b"\t" + message + b"\0",
    )


def _hash_one(path: str) -> tuple[int, int]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise OSError(errno.EINVAL, "not a regular file")
            with os.fdopen(fd, "rb", buffering=0) as fh:
                fd = -1  # ownership transferred to fh
                digest = hashlib.file_digest(fh, "sha256").hexdigest()
        finally:
            if fd >= 0:
                os.close(fd)
    except OSError as exc:
        _emit_error(path, exc)
        return 0, 1

    _emit_result(path, digest)
    return 1, 0


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _discover(root: str) -> tuple[list[tuple[str, int]], list[str], int]:
    """Snapshot regular files on the root filesystem, like find -xdev -type f."""
    root_dev = os.stat(root).st_dev
    entries: list[tuple[str, int]] = []
    errors: list[str] = []
    total_bytes = 0

    def onerror(exc: OSError) -> None:
        errors.append(f"{type(exc).__name__}:{exc.errno}:{exc.filename}:{exc}")

    for dirpath, dirnames, filenames in os.walk(
        root, topdown=True, onerror=onerror, followlinks=False
    ):
        kept_dirs = []
        for name in dirnames:
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path, follow_symlinks=False)
            except OSError as exc:
                errors.append(f"{type(exc).__name__}:{exc.errno}:{path}:{exc}")
                continue
            if st.st_dev == root_dev and stat.S_ISDIR(st.st_mode):
                kept_dirs.append(name)
        dirnames[:] = kept_dirs

        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path, follow_symlinks=False)
            except OSError as exc:
                errors.append(f"{type(exc).__name__}:{exc.errno}:{path}:{exc}")
                continue
            if st.st_dev == root_dev and stat.S_ISREG(st.st_mode):
                entries.append((path, st.st_size))
                total_bytes += st.st_size
    return entries, errors, total_bytes


def _partition(entries: list[tuple[str, int]], count: int) -> list[list[str]]:
    """Greedy longest-processing-time split by file size."""
    buckets: list[list] = [[[], 0] for _ in range(count)]
    for path, size in sorted(entries, key=lambda item: item[1], reverse=True):
        index = min(range(count), key=lambda i: buckets[i][1])
        buckets[index][0].append(path)
        buckets[index][1] += size
    return [bucket[0] for bucket in buckets]


def _worker_main(
    index: int,
    paths: list[str],
    results_path: str,
    errors_path: str,
    threads: int,
    batch: int,
    report_fd: int,
) -> int:
    global _CONFIG, _EXECUTOR
    _CONFIG = (results_path, errors_path, threads)
    _EXECUTOR = ThreadPoolExecutor(
        max_workers=threads, thread_name_prefix=f"hash-{os.getpid()}"
    )
    started = time.monotonic()
    hashed = failed = 0
    error_text: str | None = None
    exit_code = 0

    try:
        for part in _chunks(paths, batch):
            for ok, bad in _EXECUTOR.map(_hash_one, part):
                hashed += ok
                failed += bad
    except BaseException:
        traceback.print_exc()
        error_text = traceback.format_exc(limit=8)
        exit_code = 1
    finally:
        _EXECUTOR.shutdown(wait=True)

    summary = {
        "worker": index,
        "pid": os.getpid(),
        "files": len(paths),
        "hashed": hashed,
        "hash_errors": failed,
        "elapsed_sec": round(time.monotonic() - started, 3),
        "status": "ok" if exit_code == 0 else "FAILED",
    }
    if error_text is not None:
        summary["error"] = error_text[-2000:]
    try:
        _write_all(report_fd, json.dumps(summary).encode() + b"\n")
    except OSError:
        traceback.print_exc()
        exit_code = 1
    finally:
        os.close(report_fd)
    return exit_code


def _child_entry(
    index: int,
    paths: list[str],
    results_path: str,
    errors_path: str,
    threads: int,
    batch: int,
    report_fd: int,
) -> None:
    try:
        code = _worker_main(
            index, paths, results_path, errors_path, threads, batch, report_fd
        )
    except BaseException:
        traceback.print_exc()
        code = 1
    os._exit(code)


def _read_all(fd: int, limit: int = 1 << 20) -> bytes:
    chunks = []
    total = 0
    while total < limit:
        block = os.read(fd, 65536)
        if not block:
            break
        chunks.append(block)
        total += len(block)
    return b"".join(chunks)


def _run_workers(
    buckets: list[list[str]],
    results_path: str,
    errors_path: str,
    threads: int,
    batch: int,
) -> tuple[list[dict], int]:
    pipes = [os.pipe() for _ in buckets]
    pids: list[int] = []
    try:
        for index, paths in enumerate(buckets):
            sys.stdout.flush()
            sys.stderr.flush()
            pid = os.fork()
            if pid == 0:
                for other, (read_fd, write_fd) in enumerate(pipes):
                    if other != index:
                        os.close(read_fd)
                        os.close(write_fd)
                os.close(pipes[index][0])
                _child_entry(
                    index,
                    paths,
                    results_path,
                    errors_path,
                    threads,
                    batch,
                    pipes[index][1],
                )
            pids.append(pid)
    except BaseException:
        for read_fd, write_fd in pipes:
            for fd in (read_fd, write_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass
        for pid in pids:
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass
        for pid in pids:
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        raise

    for _, write_fd in pipes:
        os.close(write_fd)

    reports: list[dict] = []
    failures = 0
    for index, pid in enumerate(pids):
        _, status = os.waitpid(pid, 0)
        exit_code = os.waitstatus_to_exitcode(status)
        data = _read_all(pipes[index][0])
        os.close(pipes[index][0])
        report: dict
        if data:
            try:
                report = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                report = {"status": "FAILED", "error": "bad worker report"}
        else:
            report = {"status": "FAILED", "error": "empty worker report"}
        report["exit_code"] = exit_code
        if exit_code != 0 or report.get("status") != "ok":
            failures += 1
        reports.append(report)
    return reports, failures


def _first_error(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            data = fh.read(4096)
    except OSError:
        return None
    record = data.split(b"\0", 1)[0]
    if not record:
        return None
    return record.decode("utf-8", "backslashreplace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/usr")
    parser.add_argument("--procs", type=int, default=4)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--batch", type=int, default=32)
    parser.add_argument("--out", required=True)
    parser.add_argument("--errors", required=True)
    args = parser.parse_args(argv)

    if args.procs < 1 or args.threads < 1 or args.batch < 1:
        parser.error("--procs, --threads and --batch must be >= 1")

    root = os.path.realpath(args.root)
    out = os.path.abspath(args.out)
    errors = os.path.abspath(args.errors)
    for target in (out, errors):
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        try:
            if os.path.commonpath((root, os.path.realpath(target))) == root:
                parser.error("output files must not live under --root")
        except ValueError:
            pass

    started = time.monotonic()
    entries, discovery_errors, total_bytes = _discover(root)

    # Truncate only after discovery: output is outside root, but this also
    # avoids a stale file from a previous run being mistaken for this run.
    for target in (out, errors):
        with open(target, "wb"):
            pass

    for message in discovery_errors[:5]:
        print(f"discover error: {message}", file=sys.stderr)

    buckets = _partition(entries, args.procs)
    reports, worker_failures = _run_workers(
        buckets, out, errors, args.threads, args.batch
    )
    hashed = sum(int(report.get("hashed", 0)) for report in reports)
    failed = sum(int(report.get("hash_errors", 0)) for report in reports)

    with open(out, "rb+") as fh:
        os.fsync(fh.fileno())
    with open(errors, "rb+") as fh:
        os.fsync(fh.fileno())

    elapsed = time.monotonic() - started
    accounted = hashed + failed
    ok = (
        not discovery_errors
        and not worker_failures
        and failed == 0
        and accounted == len(entries)
    )
    summary = {
        "root": root,
        "procs": args.procs,
        "threads": args.threads,
        "threads_total": args.procs * args.threads,
        "files_discovered": len(entries),
        "bytes_discovered": total_bytes,
        "hashed": hashed,
        "hash_errors": failed,
        "discovery_errors": len(discovery_errors),
        "worker_failures": worker_failures,
        "accounted": accounted,
        "elapsed_sec": round(elapsed, 3),
        "first_error": _first_error(errors),
        "worker_stats": reports,
        "status": "ok" if ok else "FAILED",
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
PY

chown -R 1000:1000 "$WORK" 2>/dev/null || true
rm -rf "$SAMPLE"
mkdir -p "$SAMPLE"
find /usr/lib -xdev -type f -size -64k 2>/dev/null | head -n 200 | while read -r f; do
    n=$(find "$SAMPLE" -maxdepth 1 -type f | wc -l)
    cp "$f" "$SAMPLE/f$(printf '%03d' $((n + 1)))" 2>/dev/null || true
done
chown -R 1000:1000 "$SAMPLE"
log "fix_sample files=$(find "$SAMPLE" -maxdepth 1 -type f | wc -l)"

as_agent() {
    setpriv --reuid=1000 --regid=1000 --init-groups env HOME=/home/asterinas "$@"
}

run_bench() {
    tag=$1
    shift
    out=$(as_agent timeout "$BUDGET" python3 -I "$WORK/perf_bench.py" "$@" 2>&1)
    rc=$?
    log "fix_$tag rc=$rc $(printf '%s' "$out" | tr '\n' ' ' | head -c 500)"
}

run_bench hash_4p4t --root "$SAMPLE" --mode hash --procs 4 --threads 4 --warm
run_bench lock_tfd_4p4t --root "$SAMPLE" --mode lock_tfd --procs 4 --threads 4 --out "$OUT/fix-lt44.bin"
run_bench lock_tfd_1p4t --root "$SAMPLE" --mode lock_tfd --procs 1 --threads 4 --out "$OUT/fix-lt14.bin"
run_bench lock_tfd_4p1t --root "$SAMPLE" --mode lock_tfd --procs 4 --threads 1 --out "$OUT/fix-lt41.bin"

t0=$(now)
as_agent timeout "$BUDGET" python3 -I "$WORK/stress_hash.py" --root "$SAMPLE" \
    --procs 4 --threads 4 --batch 32 \
    --out "$OUT/fix-tool.raw" --errors "$OUT/fix-tool.err" \
    >"$OUT/fix-tool.summary" 2>&1
trc=$?
log "fix_tool44 rc=$trc dur=$(( $(now) - t0 ))s records=$(tr -cd '\0' <"$OUT/fix-tool.raw" | wc -c) err_bytes=$(wc -c <"$OUT/fix-tool.err" 2>/dev/null || echo 0)"
log "fix_tool44_summary=$(tr '\n' ' ' <"$OUT/fix-tool.summary" | head -c 400)"

log "perf_fixcheck_done=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo DAILY_DONE=ok >/dev/console
