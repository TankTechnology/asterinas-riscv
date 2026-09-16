# Linux-Compatible Per-Thread Schedstat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add monotonic per-thread scheduler runtime, runnable-wait, and dispatch counters exposed through Linux-shaped `/proc/<pid>/schedstat` files.

**Architecture:** A small `SchedInfo` object owned by `SchedAttr` records queue transitions in architecture counter ticks using relaxed atomics; runqueue ownership serializes writers. Procfs combines the converted runnable-wait value with the existing POSIX thread CPU clock and formats the three-field Linux ABI. A bounded TLA+ model and focused kernel/userspace tests cover duplicate enqueue, preemption, blocking, wakeup, and migration before browser tooling consumes the interface.

**Tech Stack:** Rust `core::sync::atomic`, Asterinas CFS/runqueue and procfs templates, kernel-mode `ktest`, C regression tests, TLA+ TLC 1.7.4, persistent Docker launcher.

---

## File map

- Create `kernel/src/sched/sched_class/sched_info.rs`: pure timestamp/counter state and tick-to-nanosecond conversion.
- Modify `kernel/src/sched/sched_class/mod.rs`: own `SchedInfo` in `SchedAttr` and call it at successful enqueue/dispatch/requeue transitions.
- Create `kernel/src/fs/fs_impls/procfs/pid/task/schedstat.rs`: exact three-field procfs rendering.
- Modify `kernel/src/fs/fs_impls/procfs/pid/task/mod.rs`: publish the inherited `schedstat` entry.
- Create `test/initramfs/src/regression/fs/procfs/schedstat.c`: Linux ABI and behavioral regression.
- Create `tools/verification/sched_info/*`: finite state-machine model, positive configuration, two negative controls, runner, and code-correspondence note.
- Update `book/src/kernel/` only if an existing procfs compatibility page lists individual task files; otherwise document the interface in the later performance evidence record.

### Task 1: Model the accounting state machine

**Files:**
- Create: `tools/verification/sched_info/SchedInfo.tla`
- Create: `tools/verification/sched_info/Correct.cfg`
- Create: `tools/verification/sched_info/ResetDuplicate.cfg`
- Create: `tools/verification/sched_info/CountBlocked.cfg`
- Create: `tools/verification/sched_info/run.sh`
- Create: `tools/verification/sched_info/README.md`

- [ ] **Step 1: Write the finite model and deliberate regressions**

Model two tasks, two CPUs, and time `0..4`.  Each task has one owner in
`{"Blocked", "Q0", "Q1", "R0", "R1"}` plus implementation and specification
copies of `queuedAt`, `wait`, and `dispatches`.  The complete transition set is:

```tla
Wake(t, cpu)       == owner[t] = "Blocked" /\ owner' = [owner EXCEPT ![t] = Q(cpu)]
Duplicate(t)       == IsQueued(owner[t])
Dispatch(t, cpu)   == owner[t] = Q(cpu) /\ CpuIdle(cpu)
Preempt(t, cpu)    == owner[t] = R(cpu)
Block(t, cpu)      == owner[t] = R(cpu)
Migrate(t, a, b)   == owner[t] = R(a) /\ CpuIdle(b)
Tick               == now < MaxTime /\ now' = now + 1
```

`Correct` changes neither timestamp on `Duplicate` and starts no queued interval on
`Block`. `ResetDuplicate` resets only the implementation timestamp. `CountBlocked`
sets only the implementation timestamp while blocked. Invariants compare the complete
implementation and specification ledgers and require one owner per task.

- [ ] **Step 2: Add a fail-closed runner**

Use the cached TLC artifact and checksum already used by `fair_yield`:

```bash
jar="$repo_dir/target/signal-job-control-model/tla2tools-1.7.4.jar"
sha256=936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88
```

Run each case with one worker, 256 MiB heap, and a 60-second timeout. Require exit 12
plus `Invariant LedgerMatches is violated.` for both negative controls, and exit 0 plus
`Model checking completed. No error has been found.` for `Correct`. Save all logs and
negative traces under `target/sched-info-model/run.*` without downloading anything.

- [ ] **Step 3: Run the model and inspect the counterexamples**

Run: `bash tools/verification/sched_info/run.sh`

Expected: both negative controls report nonempty counterexample traces, the corrected
model exhausts a nonzero state space, and the runner prints one final `PASS` line.

- [ ] **Step 4: Commit the model**

```bash
git add tools/verification/sched_info
git commit -m "Model per-thread scheduler accounting"
```

### Task 2: Implement and unit-test `SchedInfo`

**Files:**
- Create: `kernel/src/sched/sched_class/sched_info.rs`
- Modify: `kernel/src/sched/sched_class/mod.rs`

- [ ] **Step 1: Add failing kernel tests for the desired transition API**

Expose `SchedInfo::new`, `enqueue_at`, `dispatch_at`, and `snapshot_at_frequency` to the
parent scheduler module. Add ktests using exact synthetic ticks:

```rust
#[ktest]
fn duplicate_enqueue_preserves_first_wait_timestamp() {
    let info = SchedInfo::new();
    info.enqueue_at(10);
    info.enqueue_at(30);
    info.dispatch_at(50);
    assert_eq!(info.snapshot_at_frequency(1_000_000_000), Snapshot {
        run_delay_ns: 40,
        dispatches: 1,
    });
}

#[ktest]
fn block_and_wake_excludes_sleeping_time() {
    let info = SchedInfo::new();
    info.enqueue_at(10);
    info.dispatch_at(20);
    info.enqueue_at(100);
    info.dispatch_at(130);
    assert_eq!(info.snapshot_at_frequency(1_000_000_000).run_delay_ns, 40);
}

#[ktest]
fn tick_conversion_saturates_without_per_dispatch_division() {
    let info = SchedInfo::new();
    info.enqueue_at(0);
    info.dispatch_at(u64::MAX);
    assert_eq!(info.snapshot_at_frequency(1).run_delay_ns, u64::MAX);
}
```

Also cover dispatch-without-enqueue as a no-op and a zero frequency as an explicit
`None` result.

- [ ] **Step 2: Run the focused ktest and verify RED**

Run:

```bash
tools/docker/run_dev_container.sh -- \
  tools/riscv/kernel_ktest.sh sched_info
```

Expected: compilation fails because `sched_info` and its API do not exist yet.

- [ ] **Step 3: Implement the minimal atomic ledger**

Use an `AtomicBool` for queued state, `AtomicU64` for the queued timestamp, completed
tick total, and dispatch count. Writers are serialized by task/runqueue ownership.
`enqueue_at` stores a timestamp only on the false-to-true transition. `dispatch_at`
uses `wrapping_sub`, saturating atomic updates, and increments only if the task was
queued. Convert the final accumulated value through `u128` in the procfs read path:

```rust
pub(super) fn snapshot_at_frequency(&self, frequency: u64) -> Option<Snapshot> {
    let ticks = self.run_delay_ticks.load(Ordering::Relaxed);
    let nanos = u128::from(ticks)
        .saturating_mul(1_000_000_000)
        .checked_div(u128::from(frequency))?;
    Some(Snapshot {
        run_delay_ns: u64::try_from(nanos).unwrap_or(u64::MAX),
        dispatches: self.dispatches.load(Ordering::Relaxed),
    })
}
```

The saturating update must use `fetch_update(Ordering::Relaxed, Ordering::Relaxed,
|old| Some(old.saturating_add(delta)))`; plain `fetch_add` is not acceptable.

- [ ] **Step 4: Run the focused ktest and verify GREEN**

Run the same `kernel_ktest.sh sched_info` command.

Expected: every `sched_info` ktest passes and the QEMU process exits 0.

- [ ] **Step 5: Commit the pure ledger**

```bash
git add kernel/src/sched/sched_class/sched_info.rs kernel/src/sched/sched_class/mod.rs
git commit -m "Add monotonic per-thread scheduler ledger"
```

### Task 3: Wire actual runqueue transitions

**Files:**
- Modify: `kernel/src/sched/sched_class/mod.rs`

- [ ] **Step 1: Add failing runqueue ktests**

Extend the existing `sched_class::tests` with one test that enqueues two built threads,
dispatches the first, forces a yield to the second, and verifies these facts through a
test-only tick-injected helper:

```rust
assert_eq!(first.as_thread().unwrap().sched_attr().sched_info().dispatches(), 1);
assert!(first.as_thread().unwrap().sched_attr().sched_info().is_queued());
assert_eq!(peer.as_thread().unwrap().sched_attr().sched_info().dispatches(), 1);
```

Add a second test where an already-queued task receives another `Wake` enqueue attempt;
its queued timestamp must remain unchanged.

- [ ] **Step 2: Run the tests and verify RED**

Run: `tools/docker/run_dev_container.sh -- tools/riscv/kernel_ktest.sh sched_class`

Expected: the assertions fail because actual scheduler transitions do not update the
ledger.

- [ ] **Step 3: Add accounting at the three ownership boundaries**

- On successful `ClassScheduler::enqueue`, call `enqueue_at(sched_clock())` immediately
  before inserting the entity. The duplicate/racing early return must not change it.
- In `try_pick_next`, read one `now`, call `dispatch_at(now)` on the selected entity,
  and create `CurrentRuntime::new_at(now)` so accounting and slice start share a clock.
- When replacing a still-runnable old current, call its `enqueue_at(now)` before
  re-inserting it. `dequeue_current` for wait/exit does not start a queued interval.

- [ ] **Step 4: Run focused and full scheduler ktests**

Run:

```bash
tools/docker/run_dev_container.sh -- tools/riscv/kernel_ktest.sh sched_info
tools/docker/run_dev_container.sh -- tools/riscv/kernel_ktest.sh sched_class
```

Expected: both commands exit 0.

- [ ] **Step 5: Commit the runqueue wiring**

```bash
git add kernel/src/sched/sched_class/mod.rs
git commit -m "Account scheduler queue transitions"
```

### Task 4: Expose the Linux procfs ABI

**Files:**
- Create: `kernel/src/fs/fs_impls/procfs/pid/task/schedstat.rs`
- Modify: `kernel/src/fs/fs_impls/procfs/pid/task/mod.rs`
- Create: `test/initramfs/src/regression/fs/procfs/schedstat.c`

- [ ] **Step 1: Add the failing userspace ABI regression**

The regression parses exactly three `uint64_t` fields plus newline, compares
`/proc/self/schedstat` with `/proc/self/task/<gettid>/schedstat`, rejects writes, and
then pins two pthreads to the same available CPU. A CPU-bound worker and repeatedly
yielding observer take before/after snapshots; require nondecreasing runtime/wait/count,
positive dispatch delta, and positive wait delta after a bounded 250 ms workload. A
separate nanosleep interval must not add a wall-time-sized runnable-wait delta.

Build it with the directory's existing static pthread flags:

```bash
tools/docker/run_dev_container.sh -- \
  make -C test/initramfs/src/regression/fs/procfs \
  HOST_PLATFORM=riscv64-linux
```

- [ ] **Step 2: Boot the still-missing interface and verify RED**

Rebuild the regression initramfs without adding the procfs implementation, boot the
current RISC-V kernel, and run `/test/fs/procfs/schedstat`.

Expected: the test reports `ENOENT` for `/proc/self/schedstat` and exits nonzero. Save
the serial transcript under `target/firefox-schedstat/red/`; a compile failure or an
unrelated boot failure is not an acceptable RED result.

- [ ] **Step 3: Add failing formatting and directory-table ktests**

Factor rendering into a pure helper and assert exact bytes:

```rust
#[ktest]
fn renders_linux_three_field_schedstat() {
    assert_eq!(render(12, Snapshot { run_delay_ns: 34, dispatches: 5 }), "12 34 5\n");
}
```

Add a task-directory table test that requires `schedstat` to appear once. The existing
`PidDirOps` inheritance supplies `/proc/<pid>/schedstat`; do not add a process override.

- [ ] **Step 4: Run the focused ktest and verify RED**

Run: `tools/docker/run_dev_container.sh -- tools/riscv/kernel_ktest.sh schedstat`

Expected: compilation or assertion failure because the proc entry is absent.

- [ ] **Step 5: Implement `SchedstatFileOps`**

Use `ProcFile`, `mkmod!(a+r)`, `TidDirOps::thread_and_process`, and `VmWriter`. Read the
thread's `ProfClock`, convert its `Duration::as_nanos()` with saturation, snapshot the
scheduler ledger using `ostd::arch::tsc_freq()`, and emit one exact line. Return `ESRCH`
for a dead identity and `EIO` if the counter frequency is zero.

- [ ] **Step 6: Run focused ktests, the userspace test, and a RISC-V build**

```bash
tools/docker/run_dev_container.sh -- tools/riscv/kernel_ktest.sh schedstat
tools/docker/run_dev_container.sh -- \
  make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
```

Expected: tests pass and the build exits 0 without new warnings in modified files.

- [ ] **Step 7: Commit procfs support and its regression**

```bash
git add kernel/src/fs/fs_impls/procfs/pid/task/schedstat.rs \
        kernel/src/fs/fs_impls/procfs/pid/task/mod.rs \
        test/initramfs/src/regression/fs/procfs/schedstat.c
git commit -m "Expose Linux-compatible task schedstat"
```

### Task 5: Run the dual-architecture gate and record evidence

**Files:**
- Create: `docs/performance/2026-09-17-schedstat-qualification.md`

- [ ] **Step 1: Run the regression on both implementations**

Run the same executable under the new RISC-V QEMU kernel and then under x86-64 QEMU.

Expected terminal marker:

```text
SCHEDSTAT_TEST PASS
```

Both QEMU processes must exit 0; retain serial logs under
`target/firefox-schedstat/{riscv64,x86_64}/`.

- [ ] **Step 2: Run final focused verification**

```bash
bash tools/verification/sched_info/run.sh
tools/docker/run_dev_container.sh -- tools/riscv/kernel_ktest.sh sched_info
tools/docker/run_dev_container.sh -- tools/riscv/kernel_ktest.sh schedstat
tools/docker/run_dev_container.sh -- \
  make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
```

Expected: every command exits 0 and the TLA+ runner confirms both negative controls.

- [ ] **Step 3: Record evidence and commit**

```bash
git add docs/performance/2026-09-17-schedstat-qualification.md
git commit -m "Qualify per-thread schedstat accounting"
```

The evidence record must list commit, kernel hashes, commands, model state counts,
QEMU terminal markers, and known weak-snapshot semantics. It must not claim a Firefox
speedup; the next plan consumes these counters for attribution.
