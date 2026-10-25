#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Round-4 B: full-scale parallel hash of /usr in the Debian guest.
#
# Runs the tool the round-3 agent wrote (4 worker processes x 4 threads,
# per-write file locks) with a large enough budget to finish, then builds an
# independent `xargs -P8 sha256sum` baseline and compares every digest.
# Both the tool and the verification run as uid 1000 (asterinas), which also
# exercises the tmpfs-1777 fix indirectly: the tool no longer needs
# multiprocessing, but the guest user must be able to use /tmp, /dev/shm
# and the work tree normally.
#
# Markers: hash_<tag>, baseline_<tag>, verify_<tag>, usr_hash_* and
# DAILY_DONE=ok.

set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORK=/home/asterinas/work4
OUT=$WORK/out
LOG=/tmp/usr-hash-stress.log
BUDGET=${HASH_BUDGET:-1200}
FALLBACK_ROOT=${HASH_FALLBACK_ROOT:-/usr/lib}
FALLBACK_BUDGET=${HASH_FALLBACK_BUDGET:-600}

mkdir -p "$OUT"
: >"$LOG"
log() { printf '%s\n' "$*" | tee -a "$LOG" >/dev/console; }
now() { date +%s; }

log "usr_hash_stress start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

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

cat >"$WORK/verify_records.py" <<'PY'
import sys

def load_raw(path):
    data = open(path, 'rb').read()
    records = {}
    bad = 0
    for rec in data.split(b'\0'):
        if not rec:
            continue
        try:
            digest, name = rec.split(b'\t', 1)
        except ValueError:
            bad += 1
            continue
        if len(digest) != 64:
            bad += 1
            continue
        records[name] = digest
    return records, bad

def unescape(name):
    if not name.startswith(b'\\'):
        return name
    out = bytearray()
    i = 1
    while i < len(name):
        c = name[i:i+1]
        if c == b'\\' and i + 1 < len(name):
            n = name[i+1:i+2]
            out += b'\n' if n == b'n' else b'\\'
            i += 2
        else:
            out += c
            i += 1
    return bytes(out)

def load_baseline(path):
    data = open(path, 'rb').read()
    records = {}
    bad = 0
    for rec in data.split(b'\0'):
        if not rec:
            continue
        try:
            digest, name = rec.split(b'  ', 1)
        except ValueError:
            bad += 1
            continue
        name = unescape(name)
        records[name] = digest
    return records, bad

raw, raw_bad = load_raw(sys.argv[1])
base, base_bad = load_baseline(sys.argv[2])
common = raw.keys() & base.keys()
mismatch = sum(1 for k in common if raw[k] != base[k])
missing = len(base.keys() - raw.keys())
extra = len(raw.keys() - base.keys())
print('raw_records=%d baseline_records=%d raw_malformed=%d baseline_malformed=%d '
      'matched=%d missing=%d extra=%d mismatch=%d'
      % (len(raw), len(base), raw_bad, base_bad, len(common) - mismatch,
         missing, extra, mismatch))
ok = (not raw_bad and not base_bad and missing == 0 and extra == 0
      and mismatch == 0 and len(raw) > 0)
print('verify_ok=%d' % (1 if ok else 0))
sys.exit(0 if ok else 1)
PY

chown -R 1000:1000 "$WORK" 2>/dev/null || true
chmod 0755 "$WORK" "$OUT" 2>/dev/null || true

SAMPLES=$OUT/samples.txt
: >"$SAMPLES"
(
    while :; do
        read -r la _ < /proc/loadavg
        ma=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
        printf '%s %s\n' "$la" "$ma" >>"$SAMPLES"
        sleep 5
    done
) &
sampler=$!

as_agent() {
    setpriv --reuid=1000 --regid=1000 --init-groups \
        env HOME=/home/asterinas "$@"
}

run_hash() {
    root=$1
    tag=$2
    budget=$3
    t0=$(now)
    as_agent timeout "$budget" python3 -I "$WORK/stress_hash.py" \
        --root "$root" --procs 4 --threads 4 --batch 32 \
        --out "$OUT/$tag.raw" --errors "$OUT/$tag.err" \
        >"$OUT/$tag.summary" 2>&1
    rc=$?
    dur=$(( $(now) - t0 ))
    log "hash_$tag root=$root rc=$rc dur=${dur}s raw_bytes=$(wc -c <"$OUT/$tag.raw" 2>/dev/null || echo 0) err_bytes=$(wc -c <"$OUT/$tag.err" 2>/dev/null || echo 0)"
    log "hash_${tag}_summary=$(tr '\n' ' ' <"$OUT/$tag.summary" 2>/dev/null | head -c 500)"
    return "$rc"
}

run_baseline() {
    root=$1
    tag=$2
    t0=$(now)
    # Append mode matters: with -P8 several sha256sum processes share one
    # stdout.  A plain '>' does not set O_APPEND, so their writes race on the
    # shared file offset and corrupt each other's records.
    : >"$OUT/$tag.baseline.z"
    # One writer only.  With -P8 the independent sha256sum processes append to
    # the same file concurrently; on this kernel their records splice into
    # each other (114 of 21,657 records were malformed even with O_APPEND), so
    # the baseline would be unusable for digest comparison.
    find "$root" -xdev -type f -print0 | sort -z | \
        xargs -0 -r -n 64 -P 1 sha256sum --zero \
        >>"$OUT/$tag.baseline.z" 2>"$OUT/$tag.baseline.err"
    rc=$?
    dur=$(( $(now) - t0 ))
    log "baseline_$tag root=$root rc=$rc dur=${dur}s records=$(tr -cd '\0' <"$OUT/$tag.baseline.z" | wc -c) err_bytes=$(wc -c <"$OUT/$tag.baseline.err" 2>/dev/null || echo 0)"
    return "$rc"
}

verify() {
    tag=$1
    out=$(python3 -I "$WORK/verify_records.py" "$OUT/$tag.raw" "$OUT/$tag.baseline.z" 2>&1)
    rc=$?
    log "verify_$tag rc=$rc $(printf '%s' "$out" | tr '\n' ' ' | head -c 400)"
    return "$rc"
}

full_ok=0
hash_rc=1
if run_hash /usr usr "$BUDGET"; then
    if run_baseline /usr usr && verify usr; then
        full_ok=1
    fi
else
    hash_rc=$?
    log "usr_hash_full=failed rc=$hash_rc; falling back to $FALLBACK_ROOT"
fi

fallback_ok=0
if [ "$full_ok" -ne 1 ]; then
    if run_hash "$FALLBACK_ROOT" fallback "$FALLBACK_BUDGET"; then
        if run_baseline "$FALLBACK_ROOT" fallback && verify fallback; then
            fallback_ok=1
        fi
    fi
fi

kill "$sampler" 2>/dev/null || true
wait "$sampler" 2>/dev/null || true

max_load=$(awk 'BEGIN{m=0} {if ($1+0 > m) m=$1+0} END{printf "%.3f", m}' "$SAMPLES" 2>/dev/null)
min_mem=$(awk 'BEGIN{m=999999999} {if ($2+0 < m) m=$2+0} END{print m}' "$SAMPLES" 2>/dev/null)
full_records=$(tr -cd '\0' <"$OUT/usr.raw" 2>/dev/null | wc -c)

log "usr_hash_stress_full=$full_ok fallback=$fallback_ok full_records=$full_records max_load=${max_load:-0} min_memavail_kb=${min_mem:-0}"
log "usr_hash_stress_files=$(ls -l "$OUT" 2>/dev/null | tail -n +2 | awk '{print $9}' | tr '\n' ';' | head -c 400)"
log "usr_hash_stress_done=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo DAILY_DONE=ok >/dev/console
