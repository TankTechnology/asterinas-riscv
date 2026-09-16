#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Parses bounded procfs CPU snapshots without inventing missing metrics."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import time


class TimeEvidenceError(ValueError):
    """The bounded system-time snapshot is unavailable or inconsistent."""


MAX_U64 = 2**64 - 1


@dataclass(frozen=True)
class ProcessCpu:
    pid: int
    comm: str
    utime_ticks: int
    stime_ticks: int
    starttime_ticks: int


@dataclass(frozen=True)
class SystemCpu:
    ticks: tuple[int, ...]
    ctxt: int
    procs_running: int
    per_cpu: tuple[tuple[int, tuple[int, ...]], ...] = ()


@dataclass(frozen=True)
class ThreadSchedstat:
    cpu_runtime_ns: int
    runqueue_wait_ns: int
    dispatch_count: int

    def as_dict(self) -> dict[str, int]:
        return {
            "cpu_runtime_ns": self.cpu_runtime_ns,
            "runqueue_wait_ns": self.runqueue_wait_ns,
            "dispatch_count": self.dispatch_count,
        }


def _natural(token: str, label: str) -> int:
    if not token.isascii() or not token.isdecimal():
        raise TimeEvidenceError(f"{label} is not a natural number")
    return int(token)


def parse_schedstat(raw: str) -> ThreadSchedstat:
    """Reads the exact three-field Linux per-thread schedstat ABI."""

    if not raw.endswith("\n") or raw.count("\n") != 1:
        raise TimeEvidenceError("schedstat record is not newline terminated")
    tokens = raw[:-1].split(" ")
    if len(tokens) != 3 or any(not token for token in tokens):
        raise TimeEvidenceError("schedstat record is not canonical")

    values = tuple(_natural(token, "schedstat") for token in tokens)
    if any(value > MAX_U64 for value in values):
        raise TimeEvidenceError("schedstat field exceeds u64")
    return ThreadSchedstat(*values)


def parse_pid_stat(raw: str) -> ProcessCpu:
    """Reads Linux field 14, 15, and 22 while preserving a parenthesized comm."""

    opening = raw.find(" (")
    closing = raw.rfind(") ")
    if opening <= 0 or closing <= opening:
        raise TimeEvidenceError("process stat has no bounded comm")

    pid = _natural(raw[:opening], "pid")
    fields = raw[closing + 2 :].split()
    if (
        pid == 0
        or closing - opening > 256
        or len(fields) < 20
        or len(fields[0]) != 1
        or fields[0] not in "RSDTtZXI"
    ):
        raise TimeEvidenceError("process stat fields are incomplete")

    return ProcessCpu(
        pid=pid,
        comm=raw[opening + 2 : closing],
        utime_ticks=_natural(fields[11], "utime"),
        stime_ticks=_natural(fields[12], "stime"),
        starttime_ticks=_natural(fields[19], "starttime"),
    )


def parse_cpu_stat(raw: str) -> SystemCpu:
    """Reads global CPU, context-switch, and runnable-process counts."""

    selected: dict[str, list[str]] = {}
    per_cpu: dict[int, tuple[int, ...]] = {}
    for line in raw.splitlines():
        words = line.split()
        if not words:
            continue
        name, *tokens = words
        if name in ("cpu", "ctxt", "procs_running"):
            if name in selected:
                raise TimeEvidenceError("system stat field is duplicated")
            selected[name] = tokens
        elif name.startswith("cpu") and name[3:].isascii() and name[3:].isdecimal():
            cpu_id = _natural(name[3:], "cpu id")
            if cpu_id >= 256 or cpu_id in per_cpu or len(tokens) != 10:
                raise TimeEvidenceError("per-CPU stat field is malformed")
            per_cpu[cpu_id] = tuple(_natural(token, "cpu") for token in tokens)

    if (
        set(selected) != {"cpu", "ctxt", "procs_running"}
        or len(selected["cpu"]) != 10
        or len(selected["ctxt"]) != 1
        or len(selected["procs_running"]) != 1
    ):
        raise TimeEvidenceError("system stat fields are incomplete")

    return SystemCpu(
        ticks=tuple(_natural(token, "cpu") for token in selected["cpu"]),
        ctxt=_natural(selected["ctxt"][0], "ctxt"),
        procs_running=_natural(selected["procs_running"][0], "running"),
        per_cpu=tuple(sorted(per_cpu.items())),
    )


@dataclass(frozen=True)
class Snapshot:
    guest_monotonic_ns: int
    system: SystemCpu
    processes: tuple[ProcessCpu, ...]


def interval(
    before: Snapshot, after: Snapshot, *, clock_ticks_per_second: int
) -> dict[str, object]:
    """Calculates validated CPU deltas without conflating them with wall time."""

    if after.guest_monotonic_ns <= before.guest_monotonic_ns:
        raise TimeEvidenceError("guest monotonic clock did not advance")
    if isinstance(clock_ticks_per_second, bool) or clock_ticks_per_second <= 0:
        raise TimeEvidenceError("clock ticks per second is invalid")

    prior = {item.pid: item for item in before.processes}
    current = {item.pid: item for item in after.processes}
    if (
        len(prior) != len(before.processes)
        or len(current) != len(after.processes)
        or prior.keys() != current.keys()
    ):
        raise TimeEvidenceError("process identities changed")

    process_deltas: list[dict[str, int | float]] = []
    for pid, old in prior.items():
        new = current[pid]
        if (
            old.starttime_ticks != new.starttime_ticks
            or new.utime_ticks < old.utime_ticks
            or new.stime_ticks < old.stime_ticks
        ):
            raise TimeEvidenceError("process identity or CPU count regressed")

        process_deltas.append(
            {
                "pid": pid,
                "starttime_ticks": old.starttime_ticks,
                "cpu_user_ms": (new.utime_ticks - old.utime_ticks)
                * 1000
                / clock_ticks_per_second,
                "cpu_kernel_ms": (new.stime_ticks - old.stime_ticks)
                * 1000
                / clock_ticks_per_second,
            }
        )

    if (
        len(before.system.ticks) != 10
        or len(after.system.ticks) != 10
        or any(new < old for old, new in zip(before.system.ticks, after.system.ticks))
        or after.system.ctxt < before.system.ctxt
    ):
        raise TimeEvidenceError("system CPU count regressed")

    system_deltas = {
        "cpu_ticks": [
            new - old for old, new in zip(before.system.ticks, after.system.ticks)
        ],
        "context_switches": after.system.ctxt - before.system.ctxt,
        "procs_running_before": before.system.procs_running,
        "procs_running_after": after.system.procs_running,
    }
    if tuple(cpu_id for cpu_id, _ in before.system.per_cpu) != tuple(
        cpu_id for cpu_id, _ in after.system.per_cpu
    ):
        raise TimeEvidenceError("per-CPU identities changed")
    per_cpu_deltas: list[dict[str, object]] = []
    duration_ns = after.guest_monotonic_ns - before.guest_monotonic_ns
    # Leave ample room for asynchronous procfs reads and tick rounding while
    # rejecting a stale image whose counters advance about ten times faster
    # than the guest clock. A single CPU cannot execute three CPU-seconds in
    # one elapsed second.
    max_core_ticks = max(50, 3 * duration_ns * clock_ticks_per_second // 1_000_000_000)
    for (cpu_id, old), (_, new) in zip(before.system.per_cpu, after.system.per_cpu):
        if (
            len(old) != 10
            or len(new) != 10
            or any(newer < older for older, newer in zip(old, new))
        ):
            raise TimeEvidenceError("per-CPU count regressed")
        deltas = [newer - older for older, newer in zip(old, new)]
        # guest and guest_nice are included in user/nice and must not be
        # counted twice. Idle and iowait are excluded from busy time.
        accounted_ticks = sum(deltas[:8])
        if accounted_ticks > max_core_ticks:
            raise TimeEvidenceError("per-core CPU tick rate exceeds guest time bound")
        per_cpu_deltas.append(
            {
                "cpu_id": cpu_id,
                "cpu_ticks": deltas,
                "busy_fraction": (
                    (accounted_ticks - deltas[3] - deltas[4]) / accounted_ticks
                    if accounted_ticks
                    else None
                ),
            }
        )
    system_deltas["per_cpu"] = per_cpu_deltas
    return {
        "clock_domain": "guest-monotonic",
        "guest_monotonic_start_ns": before.guest_monotonic_ns,
        "guest_monotonic_end_ns": after.guest_monotonic_ns,
        "duration_ms": (after.guest_monotonic_ns - before.guest_monotonic_ns)
        / 1_000_000,
        "processes": process_deltas,
        "system": system_deltas,
        "unsupported": [
            "per-process-io",
            "per-thread-runnable-wait",
            "physical-hdmi-scanout",
        ],
    }


MAX_PROC_READ_BYTES = 8 * 1024
MAX_REPORT_BYTES = 256 * 1024
MAX_INTERVALS = 64
MAX_THREADS = 128
MAX_THREAD_INTERVALS = 20
MIN_INTERVAL_SECONDS = 0.25
MAX_INTERVAL_SECONDS = 10.0


def read_proc_text(path: Path) -> str:
    """Reads one procfs snapshot with a fixed size bound and no symlinks."""

    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        )
    except OSError as error:
        raise TimeEvidenceError("procfs read is unavailable") from error

    try:
        chunks: list[bytes] = []
        size = 0
        while size <= MAX_PROC_READ_BYTES:
            part = os.read(descriptor, min(1024, MAX_PROC_READ_BYTES + 1 - size))
            if not part:
                break
            chunks.append(part)
            size += len(part)
        payload = b"".join(chunks)
        if not payload or len(payload) > MAX_PROC_READ_BYTES:
            raise TimeEvidenceError("procfs snapshot is empty or oversized")
        return payload.decode("ascii")
    except OSError as error:
        raise TimeEvidenceError("procfs read is unavailable") from error
    except UnicodeDecodeError as error:
        raise TimeEvidenceError("procfs snapshot is not ASCII") from error
    finally:
        os.close(descriptor)


def wait_for_ready_marker(
    marker_path: Path,
    *,
    timeout_seconds: float,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int:
    """Waits for an opt-in guest-monotonic READY trigger with a finite deadline."""

    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 900:
        raise TimeEvidenceError("ready marker timeout is invalid")
    deadline = time.monotonic() + timeout_seconds
    while True:
        if marker_path.exists():
            raw = read_proc_text(marker_path).strip()
            marker_ns = _natural(raw, "ready marker timestamp")
            if marker_ns == 0 or marker_ns > time.monotonic_ns():
                raise TimeEvidenceError("ready marker clock is invalid")
            return marker_ns
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeEvidenceError("ready marker deadline expired")
        sleep_fn(min(0.05, remaining))


def read_snapshot(
    proc_root: Path, pids: tuple[int, ...], guest_monotonic_ns: int
) -> Snapshot:
    """Reads one system and selected process snapshot without mutating them."""

    system = parse_cpu_stat(read_proc_text(proc_root / "stat"))
    processes: list[ProcessCpu] = []
    for pid in pids:
        parsed = parse_pid_stat(read_proc_text(proc_root / str(pid) / "stat"))
        if parsed.pid != pid:
            raise TimeEvidenceError("requested process identity changed")
        processes.append(parsed)
    return Snapshot(guest_monotonic_ns, system, tuple(processes))


def read_thread_snapshot(
    proc_root: Path, pid: int, guest_monotonic_ns: int
) -> dict[str, object]:
    """Reads at most 128 named TID CPU counters for one stable process."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise TimeEvidenceError("thread process id is invalid")
    process_dir = proc_root / str(pid)
    process = parse_pid_stat(read_proc_text(process_dir / "stat"))
    if process.pid != pid:
        raise TimeEvidenceError("thread process identity changed")

    tids: list[int] = []
    try:
        with os.scandir(process_dir / "task") as entries:
            for entry in entries:
                if entry.name.isascii() and entry.name.isdecimal():
                    tids.append(int(entry.name))
                    if len(tids) > MAX_THREADS:
                        raise TimeEvidenceError("thread count exceeds bound")
    except OSError as error:
        raise TimeEvidenceError("thread directory is unavailable") from error

    threads: dict[int, dict[str, int | str]] = {}
    for tid in sorted(tids):
        raw = read_proc_text(process_dir / "task" / str(tid) / "stat")
        parsed = parse_pid_stat(raw)
        fields = raw[raw.rindex(") ") + 2 :].split()
        if parsed.pid != tid or len(fields) <= 36:
            raise TimeEvidenceError("thread stat identity or CPU field is invalid")
        threads[tid] = {
            "comm": parsed.comm,
            "utime_ticks": parsed.utime_ticks,
            "stime_ticks": parsed.stime_ticks,
            "starttime_ticks": parsed.starttime_ticks,
            "last_cpu": _natural(fields[36], "thread last CPU"),
            "schedstat": parse_schedstat(
                read_proc_text(process_dir / "task" / str(tid) / "schedstat")
            ).as_dict(),
        }
    return {
        "guest_monotonic_ns": guest_monotonic_ns,
        "process_starttime_ticks": process.starttime_ticks,
        "threads": threads,
    }


def run_sampler(
    proc_root: Path,
    pids: tuple[int, ...],
    output: Path,
    *,
    interval_seconds: float,
    samples: int,
    clock_ns: Callable[[], int] = time.monotonic_ns,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_ticks_per_second: int | None = None,
) -> dict[str, object]:
    """Publishes bounded Firefox/Xorg CPU intervals to an exclusive JSON file."""

    if (
        isinstance(samples, bool)
        or not isinstance(samples, int)
        or not 1 <= samples <= MAX_INTERVALS
        or isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, (int, float))
        or not math.isfinite(interval_seconds)
        or not MIN_INTERVAL_SECONDS <= interval_seconds <= MAX_INTERVAL_SECONDS
    ):
        raise TimeEvidenceError("sampling bounds are invalid")
    if (
        not pids
        or len(pids) > 8
        or len(set(pids)) != len(pids)
        or any(
            isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
            for pid in pids
        )
    ):
        raise TimeEvidenceError("process identities are invalid")

    snapshots = []
    for sample_index in range(samples + 1):
        snapshots.append(read_snapshot(proc_root, pids, clock_ns()))
        if sample_index < samples:
            sleep_fn(interval_seconds)

    ticks_per_second = (
        os.sysconf("SC_CLK_TCK")
        if clock_ticks_per_second is None
        else clock_ticks_per_second
    )
    report: dict[str, object] = {
        "schema_version": 1,
        "samples": samples,
        "process_ids": list(pids),
        "intervals": [
            interval(before, after, clock_ticks_per_second=ticks_per_second)
            for before, after in zip(snapshots, snapshots[1:])
        ],
    }
    payload = (
        json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    if len(payload) > MAX_REPORT_BYTES:
        raise TimeEvidenceError("time evidence exceeds the bounded output")

    try:
        descriptor = os.open(
            output,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as error:
        raise TimeEvidenceError("time evidence output is not exclusive") from error

    try:
        cursor = 0
        while cursor < len(payload):
            count = os.write(descriptor, payload[cursor:])
            if count <= 0:
                raise TimeEvidenceError("time evidence write did not advance")
            cursor += count
    finally:
        os.close(descriptor)
    return report


def run_thread_sampler(
    proc_root: Path,
    pid: int,
    ready_marker_path: Path,
    output: Path,
    *,
    interval_seconds: float,
    samples: int,
    physical: bool = False,
    clock_ns: Callable[[], int] = time.monotonic_ns,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_ticks_per_second: int | None = None,
) -> dict[str, object]:
    """Publishes TID CPU intervals after a real, bounded READY condition."""

    if (
        isinstance(samples, bool)
        or not isinstance(samples, int)
        or not 1 <= samples <= MAX_THREAD_INTERVALS
        or isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, (int, float))
        or not math.isfinite(interval_seconds)
        or not MIN_INTERVAL_SECONDS <= interval_seconds <= MAX_INTERVAL_SECONDS
        or type(physical) is not bool
    ):
        raise TimeEvidenceError("thread sampling bounds are invalid")
    if output.exists():
        raise TimeEvidenceError("thread evidence output is not exclusive")

    ready_marker_ns = wait_for_ready_marker(ready_marker_path, timeout_seconds=600)
    snapshots = []
    for index in range(samples + 1):
        snapshots.append(read_thread_snapshot(proc_root, pid, clock_ns()))
        if index < samples:
            sleep_fn(interval_seconds)

    hz = (
        os.sysconf("SC_CLK_TCK")
        if clock_ticks_per_second is None
        else clock_ticks_per_second
    )
    if isinstance(hz, bool) or not isinstance(hz, int) or hz <= 0:
        raise TimeEvidenceError("thread clock tick rate is invalid")
    if snapshots[0]["guest_monotonic_ns"] < ready_marker_ns:
        raise TimeEvidenceError("thread sample began before READY")

    intervals: list[dict[str, object]] = []
    limitations: set[str] = set()
    for before, after in zip(snapshots, snapshots[1:]):
        if (
            before["process_starttime_ticks"] != after["process_starttime_ticks"]
            or after["guest_monotonic_ns"] <= before["guest_monotonic_ns"]
        ):
            raise TimeEvidenceError("thread process identity or clock changed")
        prior = before["threads"]
        current = after["threads"]
        common = prior.keys() & current.keys()
        new_tids = sorted(current.keys() - prior.keys())
        gone_tids = sorted(prior.keys() - current.keys())
        if new_tids or gone_tids:
            limitations.add("thread-churn")
        duration_ns = after["guest_monotonic_ns"] - before["guest_monotonic_ns"]
        max_thread_ticks = max(50, 3 * duration_ns * hz // 1_000_000_000)
        deltas: list[dict[str, object]] = []
        for tid in sorted(common):
            old, new = prior[tid], current[tid]
            user_ticks = new["utime_ticks"] - old["utime_ticks"]
            kernel_ticks = new["stime_ticks"] - old["stime_ticks"]
            if (
                old["starttime_ticks"] != new["starttime_ticks"]
                or user_ticks < 0
                or kernel_ticks < 0
                or user_ticks + kernel_ticks > max_thread_ticks
            ):
                raise TimeEvidenceError("thread identity or CPU tick rate is invalid")
            old_schedstat = old["schedstat"]
            new_schedstat = new["schedstat"]
            schedstat_delta = {
                key: new_schedstat[key] - old_schedstat[key]
                for key in (
                    "cpu_runtime_ns",
                    "runqueue_wait_ns",
                    "dispatch_count",
                )
            }
            if any(value < 0 for value in schedstat_delta.values()):
                raise TimeEvidenceError("thread schedstat counter regressed")
            max_thread_runtime_ns = max(
                50 * 1_000_000_000 // hz,
                3 * duration_ns,
            )
            if schedstat_delta["cpu_runtime_ns"] > max_thread_runtime_ns:
                raise TimeEvidenceError("thread schedstat runtime rate is invalid")
            deltas.append(
                {
                    "tid": tid,
                    "comm": new["comm"],
                    "cpu_user_ms": user_ticks * 1000 / hz,
                    "cpu_kernel_ms": kernel_ticks * 1000 / hz,
                    "last_cpu_before": old["last_cpu"],
                    "last_cpu_after": new["last_cpu"],
                    "schedstat": {
                        "before": old_schedstat,
                        "after": new_schedstat,
                        "delta": schedstat_delta,
                    },
                }
            )
        intervals.append(
            {
                "guest_monotonic_start_ns": before["guest_monotonic_ns"],
                "guest_monotonic_end_ns": after["guest_monotonic_ns"],
                "duration_ms": duration_ns / 1_000_000,
                "threads": deltas,
                "new_tids": new_tids,
                "gone_tids": gone_tids,
            }
        )

    affinity: dict[str, list[int] | None] = {}
    for tid in snapshots[0]["threads"]:
        try:
            affinity[str(tid)] = sorted(os.sched_getaffinity(tid))
        except OSError:
            affinity[str(tid)] = None
            limitations.add("thread-affinity-unavailable")
    report: dict[str, object] = {
        "schema_version": 2,
        "physical": physical,
        "clock_domain": "guest-monotonic",
        "process_id": pid,
        "process_starttime_ticks": snapshots[0]["process_starttime_ticks"],
        "clock_ticks_per_second": hz,
        "ready_marker_ns": ready_marker_ns,
        "samples": samples,
        "affinity": affinity,
        "intervals": intervals,
        "limitations": sorted(limitations),
        "unsupported": ["physical-hdmi-scanout"],
    }
    payload = (
        json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    if len(payload) > MAX_REPORT_BYTES:
        raise TimeEvidenceError("thread evidence exceeds the bounded output")
    try:
        descriptor = os.open(
            output,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as error:
        raise TimeEvidenceError("thread evidence output is not exclusive") from error
    try:
        cursor = 0
        while cursor < len(payload):
            count = os.write(descriptor, payload[cursor:])
            if count <= 0:
                raise TimeEvidenceError("thread evidence write did not advance")
            cursor += count
    finally:
        os.close(descriptor)
    return report


def main() -> int:
    """Samples only the PIDs explicitly selected by the caller."""

    parser = argparse.ArgumentParser(description=__doc__)
    selected = parser.add_mutually_exclusive_group(required=True)
    selected.add_argument("--pid", type=int, action="append")
    selected.add_argument("--thread-pid", type=int)
    parser.add_argument("--ready-marker", type=Path)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument(
        "--physical",
        action="store_true",
        help="record that this sample came from physical hardware",
    )
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    if options.thread_pid is not None:
        if options.ready_marker is None:
            parser.error("thread sampling requires --ready-marker")
        run_thread_sampler(
            Path("/proc"),
            options.thread_pid,
            options.ready_marker,
            options.output,
            interval_seconds=options.interval_seconds,
            samples=options.samples,
            physical=options.physical,
        )
    else:
        if options.ready_marker is not None:
            parser.error("process sampling does not use --ready-marker")
        run_sampler(
            Path("/proc"),
            tuple(options.pid),
            options.output,
            interval_seconds=options.interval_seconds,
            samples=options.samples,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
