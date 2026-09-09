#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Bounded, read-only process-tree evidence, independent of Marionette.

Use an outer host/guest timeout as well: a kernel that blocks inside a procfs
read cannot be interrupted by Python's between-operation deadline checks.
This collector never publishes browser or physical acceptance.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import errno
import json
import math
import os
from pathlib import Path
import time


@dataclass(frozen=True)
class Limits:
    max_processes: int = 16
    max_threads: int = 128
    max_fds: int = 64
    max_scan: int = 1024
    max_file_bytes: int = 8192
    max_total_bytes: int = 262144
    max_seconds: float = 3.0

    def __post_init__(self):
        caps = {
            "max_processes": 128,
            "max_threads": 1024,
            "max_fds": 1024,
            "max_scan": 8192,
            "max_file_bytes": 65536,
            "max_total_bytes": 1048576,
        }
        for field, cap in caps.items():
            value = getattr(self, field)
            if type(value) is not int or not 0 < value <= cap:
                raise ValueError(f"{field} must be between 1 and {cap}")
        if not math.isfinite(self.max_seconds) or not 0 < self.max_seconds <= 30:
            raise ValueError("max_seconds must be finite and between 0 and 30")


class Reader:
    def __init__(self, limits: Limits):
        self.limits = limits
        self.started = time.monotonic()
        self.bytes_read = 0
        self.limitations: set[str] = set()

    def available(self):
        if time.monotonic() - self.started >= self.limits.max_seconds:
            self.limitations.add("time_limit")
            return False
        if self.bytes_read >= self.limits.max_total_bytes:
            self.limitations.add("byte_limit")
            return False
        return True

    def failure(self, status):
        self.limitations.add(status)
        return {"status": status}

    def read(self, path: Path, owner: Path):
        if not self.available():
            return {"status": "budget_exhausted"}
        remaining = self.limits.max_total_bytes - self.bytes_read
        count = min(remaining, self.limits.max_file_bytes + 1)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            try:
                chunks = bytearray()
                while len(chunks) < count:
                    if not self.available():
                        return {"status": "budget_exhausted"}
                    chunk = os.read(fd, count - len(chunks))
                    self.bytes_read += len(chunk)
                    if not chunk:
                        break
                    chunks.extend(chunk)
            finally:
                os.close(fd)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EPERM):
                status = "permission_denied"
            elif error.errno in (errno.ENOENT, errno.ESRCH):
                status = "unsupported" if owner.exists() else "process_gone"
            else:
                status = "io_error"
            return self.failure(status)
        if len(chunks) > self.limits.max_file_bytes:
            return self.failure("too_large")
        if len(chunks) == count and count < self.limits.max_file_bytes + 1:
            self.limitations.add("byte_limit")
            return {"status": "budget_exhausted"}
        return {"status": "ok", "value": chunks.decode("utf-8", errors="replace")}

    def numbers(self, directory: Path):
        if not self.available():
            return []
        numbers = []
        try:
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if not self.available():
                        break
                    if index >= self.limits.max_scan:
                        self.limitations.add("scan_limit")
                        break
                    if entry.name.isascii() and entry.name.isdecimal():
                        numbers.append(int(entry.name))
        except OSError:
            self.limitations.add("directory_unavailable")
        return sorted(numbers)


def _identity(reader: Reader, directory: Path):
    record = reader.read(directory / "stat", directory)
    if record["status"] != "ok":
        return None
    try:
        text = record["value"]
        pid = int(text[: text.index(" (")])
        fields = text[text.rindex(")") + 2 :].split()
        return {"pid": pid, "ppid": int(fields[1]), "start_time_ticks": int(fields[19])}
    except (ValueError, IndexError):
        reader.limitations.add("invalid_stat")
        return None


def _status(reader: Reader, directory: Path):
    record = reader.read(directory / "status", directory)
    if record["status"] != "ok":
        return record
    allowed = {
        "Name",
        "State",
        "Tgid",
        "Pid",
        "PPid",
        "Threads",
        "NSpid",
        "voluntary_ctxt_switches",
        "nonvoluntary_ctxt_switches",
    }
    return {
        "status": "ok",
        "value": {
            key: value.strip()
            for line in record["value"].splitlines()
            if ":" in line
            for key, value in [line.split(":", 1)]
            if key in allowed
        },
    }


def _unsigned(value, bits=64):
    return type(value) is int and 0 <= value < 2**bits


def _valid_call(call, *, completed=False):
    if call is None:
        return True
    if not isinstance(call, dict):
        return False
    if not all(
        _unsigned(call.get(key)) for key in ("sequence", "number", "entered_jiffies")
    ):
        return False
    args = call.get("args")
    if (
        not isinstance(args, list)
        or len(args) != 6
        or not all(_unsigned(arg) for arg in args)
    ):
        return False
    if completed:
        if not _unsigned(call.get("finished_jiffies")) or "result" not in call:
            return False
        if call.get("outcome") not in ("return", "error", "no_return"):
            return False
        if call["outcome"] == "no_return":
            return call.get("result") is None
        return type(call["result"]) is int and -(2**63) <= call["result"] < 2**63
    return True


def _valid_history(value):
    history = value.get("history")
    if not isinstance(history, list) or len(history) > 32:
        return False
    if not all(
        _valid_call(call, completed=True) and call is not None for call in history
    ):
        return False
    if any(
        older["sequence"] >= newer["sequence"]
        for older, newer in zip(history, history[1:])
    ):
        return False
    completed = value.get("completed")
    return (not history and completed is None) or (history and history[-1] == completed)


def _syscall(reader: Reader, directory: Path):
    record = reader.read(directory / "asterinas_syscall", directory)
    if record["status"] != "ok":
        return record
    try:
        value = json.loads(record["value"])
    except (ValueError, RecursionError):
        return reader.failure("invalid_json")
    version = value.get("version") if isinstance(value, dict) else None
    valid = (
        isinstance(value, dict)
        and type(version) is int
        and version in (1, 2)
        and type(value.get("enabled")) is bool
        and _unsigned(value.get("pid"), 32)
        and _unsigned(value.get("tid"), 32)
        and _unsigned(value.get("snapshot_jiffies"))
        and "current" in value
        and _valid_call(value["current"])
        and "completed" in value
        and _valid_call(value["completed"], completed=True)
        and (version == 1 or _valid_history(value))
    )
    if not valid:
        return reader.failure("invalid_schema")
    if not value["enabled"]:
        reader.limitations.add("diagnostics_disabled")
        return {"status": "disabled", "value": value}
    return {"status": "ok", "value": value}


def _ancestors_match(reader, proc_root, ancestors, identities, invalid):
    for ancestor in ancestors:
        if ancestor in invalid:
            reader.limitations.add("ancestor_identity_unverified")
            return False
        if _identity(reader, proc_root / str(ancestor)) != identities[ancestor]:
            invalid.add(ancestor)
            reader.limitations.add("ancestor_identity_unverified")
            return False
    return True


def collect_snapshot(root_pid: int, *, proc_root=Path("/proc"), limits=None):
    if type(root_pid) is not int or not 0 < root_pid <= 2**31 - 1:
        raise ValueError("root_pid must be a positive PID")
    limits = limits or Limits()
    reader = Reader(limits)
    proc_root = Path(proc_root)
    root_path = proc_root / str(root_pid)
    root_identity = _identity(reader, root_path)
    identities = {}
    if root_identity is None:
        reader.limitations.add(
            "root_identity_unavailable" if root_path.exists() else "root_process_gone"
        )
    else:
        identities[root_pid] = root_identity
        for pid in reader.numbers(proc_root):
            if pid == root_pid:
                continue
            identity = _identity(reader, proc_root / str(pid))
            if identity is not None:
                identities[pid] = identity

    selected = [root_pid] if root_identity else []
    ancestors = {root_pid: ()}
    seen = set(selected)
    for parent in selected:
        for pid, identity in identities.items():
            if identity["ppid"] == parent and pid not in seen:
                seen.add(pid)
                if len(selected) >= limits.max_processes:
                    reader.limitations.add("process_limit")
                else:
                    selected.append(pid)
                    ancestors[pid] = (*ancestors[parent], parent)

    processes = []
    invalid = set()
    thread_count = fd_count = 0
    for pid in selected:
        if not reader.available():
            break
        if not _ancestors_match(reader, proc_root, ancestors[pid], identities, invalid):
            continue
        path = proc_root / str(pid)
        identity = identities[pid]
        if _identity(reader, path) != identity:
            invalid.add(pid)
            reader.limitations.add("process_identity_changed")
            continue
        process = {
            **identity,
            "status": _status(reader, path),
            "threads": [],
            "fds": [],
        }
        for tid in reader.numbers(path / "task"):
            if thread_count >= limits.max_threads:
                reader.limitations.add("thread_limit")
                break
            thread_count += 1
            thread_path = path / "task" / str(tid)
            before = _identity(reader, thread_path)
            thread = {
                "tid": tid,
                "identity": before,
                "status": _status(reader, thread_path),
                "comm": reader.read(thread_path / "comm", thread_path),
                "syscall": _syscall(reader, thread_path),
            }
            after = _identity(reader, thread_path)
            thread["identity_verified_after"] = before is not None and after == before
            if before is None or (after is not None and after != before):
                reader.limitations.add("thread_identity_changed")
                thread = {"tid": tid, "status": {"status": "identity_changed"}}
            elif after is None:
                reader.limitations.add("thread_identity_unverified")
            process["threads"].append(thread)
        for fd in reader.numbers(path / "fd"):
            if not reader.available():
                break
            if fd_count >= limits.max_fds:
                reader.limitations.add("fd_limit")
                break
            fd_count += 1
            try:
                target = os.readlink(path / "fd" / str(fd))
                size = len(target.encode("utf-8", errors="replace"))
                if size > limits.max_file_bytes:
                    link = reader.failure("too_large")
                elif size > limits.max_total_bytes - reader.bytes_read:
                    link = reader.failure("byte_limit")
                else:
                    reader.bytes_read += size
                    link = {"status": "ok", "value": target}
            except OSError as error:
                link = reader.failure(
                    "permission_denied"
                    if error.errno in (errno.EACCES, errno.EPERM)
                    else "fd_unavailable"
                )
            process["fds"].append(
                {
                    "fd": fd,
                    "target": link,
                    "info": reader.read(path / "fdinfo" / str(fd), path),
                }
            )
        after = _identity(reader, path)
        process["identity_verified_after"] = after == identity
        if after is not None and after != identity:
            invalid.add(pid)
            reader.limitations.add("process_identity_changed")
            process = {"pid": pid, "status": {"status": "identity_changed"}}
        elif after is None:
            invalid.add(pid)
            reader.limitations.add("process_identity_unverified")
        if not _ancestors_match(reader, proc_root, ancestors[pid], identities, invalid):
            process = {"pid": pid, "status": {"status": "ancestor_identity_unverified"}}
        processes.append(process)

    if root_identity is not None:
        after = _identity(reader, root_path)
        if after is None:
            reader.limitations.add("root_identity_unverified")
        elif after != root_identity:
            invalid.add(root_pid)
            reader.limitations.add("root_identity_changed")
            processes = [
                {"pid": item["pid"], "status": {"status": "ancestor_identity_changed"}}
                for item in processes
            ]
    # A later failed ancestor check invalidates already sampled descendants too.
    processes = [
        item
        for item in processes
        if not any(parent in invalid for parent in ancestors[item["pid"]])
    ]
    return {
        "version": 1,
        "physical": False,
        "root_pid": root_pid,
        "root_identity": root_identity,
        "duration_seconds": time.monotonic() - reader.started,
        "bytes_read": reader.bytes_read,
        "complete": not reader.limitations,
        "limitations": sorted(reader.limitations),
        "processes": processes,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root-pid", type=int, required=True)
    parser.add_argument("--max-seconds", type=float, default=3)
    parser.add_argument("--max-processes", type=int, default=16)
    parser.add_argument("--max-threads", type=int, default=128)
    parser.add_argument("--max-fds", type=int, default=64)
    parser.add_argument("--max-file-bytes", type=int, default=8192)
    parser.add_argument("--max-total-bytes", type=int, default=262144)
    parser.add_argument("--max-scan", type=int, default=1024)
    values = parser.parse_args()
    try:
        limits = Limits(
            max_seconds=values.max_seconds,
            max_processes=values.max_processes,
            max_threads=values.max_threads,
            max_fds=values.max_fds,
            max_file_bytes=values.max_file_bytes,
            max_total_bytes=values.max_total_bytes,
            max_scan=values.max_scan,
        )
        result = collect_snapshot(values.root_pid, limits=limits)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")), flush=True)
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
