# Firefox System-Time Ledger Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record bounded, low-disturbance CPU and scheduling snapshots for Firefox and Xorg without mistaking missing Linux procfs fields for zero.

**Architecture:** A pure parser handles `/proc/<pid>/stat` and `/proc/stat` as separate clock domains. A guest-side sampler writes one private JSON artifact with process identity, per-process user/kernel CPU deltas, global CPU deltas, and explicit unsupported fields. This is the first independent part of the accepted Firefox interaction-performance design; the browser input/navigation fixture and measured hot-path fixes get separate plans after this ledger is verified.

**Tech Stack:** Python 3 standard library, `unittest`, Asterinas procfs, persistent project Docker for later guest qualification.

---

### Task 1: Parse Linux-compatible procfs CPU fields

**Files:**
- Create: `tools/riscv/debian/rootfs/browser_system_time.py`
- Create: `tools/riscv/tests/test_browser_system_time.py`

- [ ] **Step 1: Write failing parser tests**

```python
from tools.riscv.debian.rootfs.browser_system_time import (
    TimeEvidenceError, parse_cpu_stat, parse_pid_stat,
)

def test_pid_stat_handles_parentheses_in_comm(self):
    fields = ["S"] + ["0"] * 49
    fields[11] = "120"  # field 14: utime
    fields[12] = "30"   # field 15: stime
    fields[19] = "777"  # field 22: starttime
    parsed = parse_pid_stat("42 (Firefox (Main)) " + " ".join(fields))
    self.assertEqual((parsed.pid, parsed.comm, parsed.utime_ticks,
                      parsed.stime_ticks, parsed.starttime_ticks),
                     (42, "Firefox (Main)", 120, 30, 777))

def test_cpu_stat_rejects_missing_or_negative_counts(self):
    with self.assertRaises(TimeEvidenceError):
        parse_cpu_stat("cpu 1 2 3\nctxt 4\n")
    with self.assertRaises(TimeEvidenceError):
        parse_cpu_stat("cpu -1 2 3 4 5 6 7 8 9 10\nctxt 4\nprocs_running 1\n")
```

- [ ] **Step 2: Run and confirm the missing-module failure**

Run: `python3 -m unittest tools.riscv.tests.test_browser_system_time -v`

Expected: import failure because `browser_system_time.py` does not exist.

- [ ] **Step 3: Implement the bounded pure parser**

```python
from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import json
import os
from pathlib import Path
import time

class TimeEvidenceError(ValueError):
    """The bounded system-time snapshot is unavailable or inconsistent."""

@dataclass(frozen=True)
class ProcessCpu:
    pid: int
    comm: str
    utime_ticks: int
    stime_ticks: int
    starttime_ticks: int

def _natural(token: str, label: str) -> int:
    if not token.isascii() or not token.isdecimal():
        raise TimeEvidenceError(f"{label} is not a natural number")
    return int(token)

def parse_pid_stat(raw: str) -> ProcessCpu:
    opening, closing = raw.find(" ("), raw.rfind(") ")
    if opening <= 0 or closing <= opening:
        raise TimeEvidenceError("process stat has no bounded comm")
    pid = _natural(raw[:opening], "pid")
    fields = raw[closing + 2:].split()
    if (pid == 0 or closing - opening > 256 or len(fields) < 20 or
        len(fields[0]) != 1 or fields[0] not in "RSDTtZXI"):
        raise TimeEvidenceError("process stat fields are incomplete")
    return ProcessCpu(pid, raw[opening + 2:closing],
                      _natural(fields[11], "utime"),
                      _natural(fields[12], "stime"),
                      _natural(fields[19], "starttime"))

@dataclass(frozen=True)
class SystemCpu:
    ticks: tuple[int, ...]
    ctxt: int
    procs_running: int

def parse_cpu_stat(raw: str) -> SystemCpu:
    selected = {}
    for line in raw.splitlines():
        words = line.split()
        if not words:
            continue
        name, *tokens = words
        if name in ("cpu", "ctxt", "procs_running"):
            if name in selected:
                raise TimeEvidenceError("system stat field is duplicated")
            selected[name] = tokens
    if (set(selected) != {"cpu", "ctxt", "procs_running"} or
        len(selected["cpu"]) != 10 or len(selected["ctxt"]) != 1 or
        len(selected["procs_running"]) != 1):
        raise TimeEvidenceError("system stat fields are incomplete")
    return SystemCpu(tuple(_natural(token, "cpu") for token in selected["cpu"]),
                     _natural(selected["ctxt"][0], "ctxt"),
                     _natural(selected["procs_running"][0], "running"))
```

`parse_cpu_stat` must accept exactly ten nonnegative global `cpu` counters, one nonnegative `ctxt`, and one nonnegative `procs_running`. It must reject missing, duplicate, malformed, or negative fields. The parser does not consume `/proc/stat`'s current placeholder `procs_blocked` as evidence.

- [ ] **Step 4: Run focused tests**

Run: `python3 -m unittest tools.riscv.tests.test_browser_system_time -v`

Expected: parser tests pass, including malformed-field cases.

### Task 2: Sample bounded process and system time

**Files:**
- Modify: `tools/riscv/debian/rootfs/browser_system_time.py`
- Modify: `tools/riscv/tests/test_browser_system_time.py`

- [ ] **Step 1: Add failing interval and identity tests**

```python
def pid_stat(utime: int, stime: int, starttime: int) -> str:
    fields = ["S"] + ["0"] * 49
    fields[11], fields[12], fields[19] = map(str, (utime, stime, starttime))
    return "42 (Firefox (Main)) " + " ".join(fields)

CPU_A = "cpu 100 0 20 80 0 0 0 0 0 0\nctxt 50\nprocs_running 2\n"
CPU_B = "cpu 110 0 25 100 0 0 0 0 0 0\nctxt 60\nprocs_running 1\n"
CPU_REGRESSED = "cpu 99 0 20 80 0 0 0 0 0 0\nctxt 50\nprocs_running 2\n"
PID_A = pid_stat(120, 30, 777)
PID_B = pid_stat(130, 35, 777)

def test_interval_reports_cpu_deltas_and_clock_domain(self):
    before = Snapshot(1_000_000_000, parse_cpu_stat(CPU_A),
                      (parse_pid_stat(PID_A),))
    after = Snapshot(2_000_000_000, parse_cpu_stat(CPU_B),
                     (parse_pid_stat(PID_B),))
    report = interval(before, after, clock_ticks_per_second=100)
    self.assertEqual(report["clock_domain"], "guest-monotonic")
    self.assertEqual(report["processes"][0]["cpu_user_ms"], 100)
    self.assertEqual(report["processes"][0]["cpu_kernel_ms"], 50)

def test_interval_rejects_pid_reuse_and_counter_regression(self):
    before = Snapshot(1_000_000_000, parse_cpu_stat(CPU_A),
                      (parse_pid_stat(PID_A),))
    changed_starttime = Snapshot(2_000_000_000, parse_cpu_stat(CPU_B),
                                 (parse_pid_stat(pid_stat(130, 35, 778)),))
    regressed_cpu = Snapshot(2_000_000_000, parse_cpu_stat(CPU_REGRESSED),
                             (parse_pid_stat(PID_B),))
    with self.assertRaises(TimeEvidenceError):
        interval(before, changed_starttime, clock_ticks_per_second=100)
    with self.assertRaises(TimeEvidenceError):
        interval(before, regressed_cpu, clock_ticks_per_second=100)
```

- [ ] **Step 2: Run and confirm interval tests fail for missing API**

Run: `python3 -m unittest tools.riscv.tests.test_browser_system_time -v`

Expected: import failure for `Snapshot` or `interval`.

- [ ] **Step 3: Implement interval accounting**

```python
@dataclass(frozen=True)
class Snapshot:
    guest_monotonic_ns: int
    system: SystemCpu
    processes: tuple[ProcessCpu, ...]

def interval(before: Snapshot, after: Snapshot,
             *, clock_ticks_per_second: int) -> dict[str, object]:
    if after.guest_monotonic_ns <= before.guest_monotonic_ns:
        raise TimeEvidenceError("guest monotonic clock did not advance")
    if clock_ticks_per_second <= 0:
        raise TimeEvidenceError("clock ticks per second is invalid")
    prior = {item.pid: item for item in before.processes}
    current = {item.pid: item for item in after.processes}
    if (len(prior) != len(before.processes) or
        len(current) != len(after.processes) or prior.keys() != current.keys()):
        raise TimeEvidenceError("process identities changed")
    process_deltas = []
    for pid, old in prior.items():
        new = current[pid]
        if (old.starttime_ticks != new.starttime_ticks or
            new.utime_ticks < old.utime_ticks or new.stime_ticks < old.stime_ticks):
            raise TimeEvidenceError("process identity or CPU count regressed")
        process_deltas.append({
            "pid": pid, "starttime_ticks": old.starttime_ticks,
            "cpu_user_ms": (new.utime_ticks - old.utime_ticks) * 1000 /
                           clock_ticks_per_second,
            "cpu_kernel_ms": (new.stime_ticks - old.stime_ticks) * 1000 /
                             clock_ticks_per_second,
        })
    if (any(new < old for old, new in zip(before.system.ticks, after.system.ticks))
        or after.system.ctxt < before.system.ctxt):
        raise TimeEvidenceError("system CPU count regressed")
    system_deltas = {
        "cpu_ticks": [new - old for old, new in
                      zip(before.system.ticks, after.system.ticks)],
        "context_switches": after.system.ctxt - before.system.ctxt,
        "procs_running_before": before.system.procs_running,
        "procs_running_after": after.system.procs_running,
    }
    return {"clock_domain": "guest-monotonic",
            "duration_ms": (after.guest_monotonic_ns - before.guest_monotonic_ns) / 1_000_000,
            "processes": process_deltas,
            "system": system_deltas,
            "unsupported": ["per-process-io", "per-thread-runnable-wait",
                            "physical-hdmi-scanout"]}
```

- [ ] **Step 4: Run focused tests and static check**

Run: `python3 -m unittest tools.riscv.tests.test_browser_system_time -v`

Expected: all parser and interval tests pass.

Run: `python3 -m compileall -q tools/riscv/debian/rootfs/browser_system_time.py`

Expected: exit 0.

### Task 3: Publish one private, non-invasive guest evidence artifact

**Files:**
- Modify: `tools/riscv/debian/rootfs/browser_system_time.py`
- Modify: `tools/riscv/tests/test_browser_system_time.py`
- Modify: `tools/riscv/debian/rootfs/README.md`

- [ ] **Step 1: Add failing CLI tests**

Use real temporary `proc/stat` and `proc/42/stat` files. A controlled `sleep_fn` replaces those two files between samples, so the parser, sampler, interval calculation, and exclusive output are exercised together. Repeat with 65 samples, 249 ms, and a pre-existing or symlink output to assert `TimeEvidenceError` and no overwrite.

```python
def test_sampler_publishes_one_private_report(self):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        proc_root = root / "proc"
        (proc_root / "42").mkdir(parents=True)
        (proc_root / "stat").write_text(CPU_A)
        (proc_root / "42" / "stat").write_text(PID_A)
        output = root / "time.json"
        ticks = iter((1_000_000_000, 2_000_000_000))
        def advance(_seconds):
            (proc_root / "stat").write_text(CPU_B)
            (proc_root / "42" / "stat").write_text(PID_B)
        report = run_sampler(proc_root, (42,), output,
                             interval_seconds=0.25, samples=1,
                             clock_ns=lambda: next(ticks), sleep_fn=advance,
                             clock_ticks_per_second=100)
        self.assertEqual(report["intervals"][0]["processes"][0]["pid"], 42)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(TimeEvidenceError):
            run_sampler(proc_root, (42,), output,
                        interval_seconds=0.25, samples=1,
                        clock_ns=lambda: 1, sleep_fn=lambda _: None,
                        clock_ticks_per_second=100)
```

- [ ] **Step 2: Run and confirm sampler test failure**

Run: `python3 -m unittest tools.riscv.tests.test_browser_system_time -v`

Expected: `run_sampler` is missing.

- [ ] **Step 3: Implement read-only sampling and exclusive publication**

```python
def run_sampler(proc_root: Path, pids: tuple[int, ...], output: Path,
                *, interval_seconds: float, samples: int,
                clock_ns: Callable[[], int] = time.monotonic_ns,
                sleep_fn: Callable[[float], None] = time.sleep,
                clock_ticks_per_second: int | None = None) -> dict[str, object]:
    if not 1 <= samples <= 64 or not 0.25 <= interval_seconds <= 10.0:
        raise TimeEvidenceError("sampling bounds are invalid")
    if not pids or len(set(pids)) != len(pids):
        raise TimeEvidenceError("process identities are invalid")
    snapshots = []
    for sample_index in range(samples + 1):
        snapshots.append(read_snapshot(proc_root, pids, clock_ns()))
        if sample_index < samples:
            sleep_fn(interval_seconds)
    ticks_per_second = (os.sysconf("SC_CLK_TCK") if
                        clock_ticks_per_second is None else clock_ticks_per_second)
    report = {"schema_version": 1, "samples": samples,
              "intervals": [interval(a, b, clock_ticks_per_second=ticks_per_second)
                            for a, b in zip(snapshots, snapshots[1:])]}
    payload = (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(payload) > 256 * 1024:
        raise TimeEvidenceError("time evidence exceeds the bounded output")
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), 0o600)
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
```

`read_snapshot` limits each procfs read to 8 KiB. A process exit, PID reuse, malformed snapshot, or missing metric yields a terminal structured error, not a fabricated zero. The CLI accepts `--pid` twice (Firefox and Xorg), `--interval-seconds`, `--samples`, and `--output` and never starts or restarts the browser.

```python
MAX_PROC_READ_BYTES = 8 * 1024

def read_proc_text(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC |
                         getattr(os, "O_NOFOLLOW", 0))
    try:
        chunks = []
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
    finally:
        os.close(descriptor)

def read_snapshot(proc_root: Path, pids: tuple[int, ...],
                  guest_monotonic_ns: int) -> Snapshot:
    return Snapshot(guest_monotonic_ns,
                    parse_cpu_stat(read_proc_text(proc_root / "stat")),
                    tuple(parse_pid_stat(read_proc_text(proc_root / str(pid) / "stat"))
                          for pid in pids))

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, action="append", required=True)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    run_sampler(Path("/proc"), tuple(options.pid), options.output,
                interval_seconds=options.interval_seconds,
                samples=options.samples)
    return 0
```

- [ ] **Step 4: Run tests, syntax check, and document diagnostic invocation**

Run: `python3 -m unittest tools.riscv.tests.test_browser_system_time -v`

Expected: all sampler tests pass.

Run: `python3 -m compileall -q tools/riscv/debian/rootfs/browser_system_time.py`

Expected: exit 0.

Document that this file measures CPU time, not input-to-scanout latency; the separate browser-event/local-navigation and real-page/network experiments will align with its `guest-monotonic` intervals without subtracting uncalibrated JS timestamps.

### Task 4: Qualify the ledger before broader probes

**Files:**
- Test: `tools/riscv/tests/test_browser_system_time.py`
- Test: `tools/riscv/tests/test_debian_browser_web.py`

- [ ] **Step 1: Run the independent host suite**

Run: `python3 -m unittest tools.riscv.tests.test_browser_system_time tools.riscv.tests.test_browser_interaction_perf -v`

Expected: all cases pass.

- [ ] **Step 2: Run the Debian browser smoke suite**

Run: `python3 -m unittest tools.riscv.tests.test_debian_browser_web -v`

Expected: exit 0 with no regressions.

- [ ] **Step 3: Inspect and record the exact diff**

Run: `git diff --check`

Expected: exit 0. Existing unrelated worktree edits are retained and are not included in any sampler commit.

The next independent plans add keyboard/pointer/scroll-to-framebuffer evidence and local/public navigation waterfall evidence. Kernel runnable-wait or I/O accounting is added only if this ledger plus those probes shows a material unexplained wait; generic `perf_event_open` is not required for the first baseline.
