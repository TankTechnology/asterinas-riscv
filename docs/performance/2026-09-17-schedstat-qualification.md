# Per-thread schedstat qualification, 2026-09-17

This record qualifies the first observability slice of Firefox performance
milestone M2a.  It adds Linux-shaped cumulative scheduler counters and a short,
repeatable test mode.  It does **not** claim a Firefox speedup or identify a
performance bottleneck by itself; the counters are the input to the next
attribution experiment.

## Delivered interface

Asterinas now exposes these read-only files:

```text
/proc/<pid>/schedstat
/proc/<pid>/task/<tid>/schedstat
```

Each read returns exactly:

```text
<cpu_runtime_ns> <completed_runqueue_wait_ns> <dispatch_count>\n
```

The process path describes the thread-group leader, while the task path
describes the selected TID.  CPU runtime reuses the thread profiling clock.
The scheduler ledger records successful queue entry, queued-to-running
dispatch, and runnable requeue transitions without adding division to the
scheduler hot path; counter ticks are converted to nanoseconds only when the
procfs entry is read.  Accumulation and conversion saturate instead of
wrapping.

The implementation was developed in these commits:

| Commit | Purpose |
|---|---|
| `cba7f76afb8239469f4d2347d3a9944b109f28e9` | Finite scheduler-accounting model and negative controls |
| `0b8e275198a2d6e72371e6fc5089ac4c98f04b4b` | Monotonic per-thread ledger |
| `6884e6304a3f8b80a37fa4b049f218fb1993eef2` | Actual runqueue transition accounting |
| `58c63457048a40815f2fcef9ed007b2c71db2d6f` | Procfs ABI and userspace regression |
| `58a3f8875e9688498a9d38d45414bcbf231a2ba7` | Focused QEMU regression mode |

The design and implementation plan are commits
`9b87115f39a038a6dc36a5dd0b1e9907dec7d879` and
`cc8bae7bf7e41ecb4db11249dfab16fdd9e9fe6a`, respectively.

## Formal state-machine check

The bounded TLA+ check was run with:

```bash
bash tools/verification/sched_info/run.sh
```

Artifacts are under `target/sched-info-model/run.HdBguQq1`.  The corrected
model completed with 4,203 generated states, 1,962 distinct states, no states
left on the queue, and no invariant violation.  The two deliberate defects
were both detected: resetting a timestamp on duplicate enqueue produced a
counterexample after 60 generated states, and counting blocked time produced a
counterexample after 81 generated states.  This validates the bounded
transition contract; it is not a proof of Rust weak-memory behavior.

## Kernel and userspace verification

The final RISC-V kernel-mode checks used the persistent development container
and an SMP=1 QEMU guest:

```bash
cd kernel
SMP=1 OSDK_TARGET_ARCH=riscv64 \
  cargo osdk test sched_info --scheme riscv --features riscv_sv39_mode
SMP=1 OSDK_TARGET_ARCH=riscv64 \
  cargo osdk test schedstat --scheme riscv --features riscv_sv39_mode
```

Both guests booted and each focused test passed 1/1.  Serial evidence is in:

- `target/schedstat-final-riscv64-sched-info-20260917/qemu-serial.log`
- `target/schedstat-final-riscv64-procfs-20260917/qemu-serial.log`

The focused userspace regression validates the exact three-field ABI,
leader/TID identity, monotonic counters, read-only enforcement, same-CPU
contention, and exclusion of sleeping time from runnable wait.  It passed all
21 checks on both architectures:

| Architecture | Result | Evidence |
|---|---:|---|
| x86-64 QEMU/TCG | 21 passed, 0 failed | `target/schedstat-auto-x86_64-20260917/runner.log` |
| RISC-V QEMU | 21 passed, 0 failed | `target/schedstat-auto-riscv64-20260917/runner.log` |

The stable entry point is:

```bash
make run_kernel TARGET_ARCH=riscv64 AUTO_TEST=schedstat SMP=1 \
  FEATURES=riscv_sv39_mode
```

It emits `SCHEDSTAT_TEST PASS` directly through `/dev/console`, avoiding a
dependency on interactive shell or pseudo-terminal capture.

A final normal four-hart RISC-V build also completed:

```bash
make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
```

Its artifacts were:

| Artifact | SHA-256 |
|---|---|
| `target/osdk/aster-kernel/aster-kernel-osdk-bin` | `561bdef4750ffe98689cdfb24851fe1b8d7696c5e7e2b56cce17e12e404cbd4d` |
| `target/osdk/aster-kernel/aster-kernel-osdk-bin.Image` | `3170e973393194d1228342b70a26de1198524ca62f124382c4ec9f73df96e30b` |

## Semantics and known limits

- Procfs reads are deliberately weak atomic snapshots.  Each individual field
  is monotonic, but a tuple may combine values from neighboring scheduler
  transitions.  Interval analysis must aggregate samples and tolerate one
  transition-boundary sample instead of assuming a transactional tuple.
- A queued thread's current, unfinished waiting interval is published only
  when that thread is dispatched.  This matches the documented contract used
  for the profiler and prevents read-side runqueue locking.
- Sleeping and blocked time is excluded from runnable wait.
- Counters saturate at `u64::MAX`; they never reset on read.
- The x86-64 regression used software emulation, so it validates ABI behavior,
  not KVM performance.

## Next attribution gate

The next step is to extend the browser time ledger to sample these counters for
Firefox and Xorg threads during fixed idle, interaction, and local-navigation
phases.  Only a repeatable classification—CPU execution, runnable delay, or
sleeping/blocking—will admit a kernel optimization.  The before/after speedup
claim must then use the same workload and a focused regression; this
qualification record supplies observability, not that claim.
