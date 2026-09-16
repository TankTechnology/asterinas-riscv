# Firefox schedstat and sampled-PC attribution design

**Date:** 2026-09-16

**Status:** Awaiting written-spec review

**Milestone:** Firefox performance M2a (attribution before optimization)

## Goal

Identify whether Firefox's remaining keyboard, pointer, and navigation latency is
caused by CPU execution, runnable-queue delay, sleeping/blocking, or a repeatable
kernel/userspace instruction hotspot.  The profiler must be cheap enough for the
physical Megrez workload and must not reuse the detailed syscall/fault probes that
increased an earlier QEMU launch by about 36%.

This subproject delivers the missing measurements and one reproducible physical
evidence set.  It does not choose or implement the eventual kernel optimization.
That change is admitted only after the measurements identify a specific mechanism
and a focused regression can be written for it.

## Existing evidence and gap

The current browser ledger records per-thread user/kernel CPU ticks, task state,
last CPU, affinity, system CPU utilization, and context switches.  Its schema
correctly lists per-thread runnable wait as unsupported.  The latest physical run
found the Firefox main thread dominant, Xorg below 0.04 average cores, and the two
hottest Firefox threads already running on different CPUs.  That evidence rejects
blind affinity changes but cannot distinguish execution from runqueue contention.

The existing `qemu_live_pc_sampler.sh` attaches GDB to the system-emulation stub,
briefly pauses all harts, and has no physical-board equivalent.  Detailed syscall,
futex, VM, and page-cache logging is useful for a short second-stage experiment but
is too perturbing to select the first target.

Linux documents `/proc/<pid>/schedstat` as three cumulative fields: CPU runtime in
nanoseconds, runnable-queue wait in nanoseconds, and timeslices dispatched.  The
same entry is present in a thread directory.  These counters are intended to be
sampled by taking deltas rather than reset between observations:

- <https://docs.kernel.org/scheduler/sched-stats.html>
- <https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/tree/fs/proc/base.c?h=v6.12#n508>
- <https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/tree/kernel/sched/stats.h?h=v6.12#n221>

## Considered approaches

### Implement `perf_event_open` first

This would provide the most familiar profiler interface, but Asterinas currently has
no perf-event subsystem.  Correct implementation requires event lifetime, overflow,
inheritance, mmap/ring-buffer, permission, and architecture-PMU contracts.  It is too
large to make the next Firefox diagnosis cheaper and is deferred.

### Poll registers with ptrace or QEMU GDB

Ptrace can expose stopped user registers and QEMU GDB can expose all hart PCs, but
both stop execution and distort the scheduling latency being measured.  QEMU GDB
remains an optional corroborating mechanism, not the physical acceptance profiler.

### Add Linux schedstat plus a bounded opt-in PC sampler

This is the selected approach.  Linux-compatible cumulative scheduling counters are
always available with small per-dispatch accounting.  A separate experimental procfs
entry exposes a bounded ring of PCs sampled from timer interrupts only when an
explicit boot option is present.  The standard ABI and the experimental diagnostic
surface are kept separate.

## Linux-compatible schedstat contract

Add read-only files at both:

```text
/proc/<pid>/schedstat
/proc/<pid>/task/<tid>/schedstat
```

Each read returns exactly three base-10 unsigned integers and a newline:

```text
<cpu_runtime_ns> <runqueue_wait_ns> <dispatch_count>\n
```

`/proc/<pid>/schedstat` describes the thread-group leader, matching Linux; it is not
an aggregate of all threads.  The task-directory entry describes that TID.  The file
uses the same world-readable mode and owner-thread behavior as the existing Linux
proc task statistics entries.  A vanished thread returns the same `ESRCH` behavior as
the neighboring task files.

The first field reuses the POSIX thread profiling clock and therefore reports user
plus kernel CPU time in nanoseconds.  It remains tick-accounting granularity even
though the output unit is nanoseconds.  The second field includes completed intervals
from becoming runnable/enqueued until being selected to run.  As in Linux, a task
that is still queued at the exact read instant does not publish its unfinished wait
until it is dispatched.  Sleeping time and the gap before a migrating
task is enqueued on its destination are not runnable wait.  The third field increases
once whenever an enqueued task is selected as the runqueue current task.

All fields are cumulative and saturate rather than wrap.  Reads never reset state.
Clock ticks are accumulated on the scheduler hot path and converted with the stable
architecture counter frequency only when procfs is read; a division is not added to
each context switch.  Zero counter frequency is an initialization error, not a valid
zero-duration result.

## Scheduling state transitions

Per-thread scheduling information stores a queued timestamp, completed queued ticks,
and dispatch count.  Scheduler transitions are serialized by the runqueue ownership
that already prevents one task from being runnable on two CPUs.  Procfs readers use
atomics and never acquire a runqueue lock.

The transition rules are:

| Event | Accounting action |
|---|---|
| Spawn or wake successfully enters a queue | Set the queued timestamp only when none is already present. |
| Duplicate/racing enqueue finds the task still queued | Preserve the original timestamp; do not double count. |
| A queued task is selected | Clear its queued timestamp, add the elapsed ticks, and increment dispatch count. |
| A running task is preempted or yields to a different task | Start a new queued interval before placing it back in its class queue. |
| A running task blocks or exits | Remove it without starting a queued interval. |
| A running task migrates | Removal does not count as wait; the successful destination enqueue starts the interval. |

Updates use relaxed per-task atomics because runqueue ownership supplies writer
serialization and readers need monotonic observations, not synchronization of other
task memory.  The public snapshot may observe adjacent fields at slightly different
transition instants, as Linux's task sched-info reader can; every individual field
must remain monotonic.  The browser analyzer treats a single interval crossing such a
transition as measurement uncertainty and aggregates multiple intervals rather than
inventing a transactional tuple.

## Opt-in sampled-PC diagnostic

### Collection

An OSTD timer hook receives a read-only architecture trap-frame view containing the
interrupted instruction pointer.  The existing interrupt-level state identifies
whether the timer interrupted user or kernel execution.  The kernel callback obtains
the current POSIX thread and records at most 100 samples per second per running CPU
when this boot option is present:

```text
asterinas.pc_profile=1
```

With the option absent, no sampling callback is registered and per-thread PC rings are
not allocated.  The callback never formats, logs, allocates, takes a sleeping lock,
resolves symbols, or wakes a reader.

Each POSIX thread owns a fixed 32-slot atomic ring.  One record contains sample ID,
counter timestamp, CPU, privilege (`user` or `kernel`), and raw PC.  A per-slot odd/even
sequence protects readers from a partially overwritten record; interrupt writers never
retry or wait.  The ring intentionally retains only the latest roughly 320 ms of CPU
execution.  This is a statistical sample, not a complete trace or proof that an
unobserved function did not execute.

### Procfs surface and access control

The experimental read-only entry is:

```text
/proc/<pid>/task/<tid>/asterinas_pc_samples
```

It is owner-readable, bounded to one header plus at most 32 records, and uses the same
alien-access/exec-identity checks as `asterinas_syscall`.  An open handle is tied to the
current VMAR identity; after exec it reaches EOF rather than exposing addresses from a
new address space through an old descriptor.  Samples are reset at exec.  Reads skip an
unstable slot after a bounded retry and state the number skipped; they never spin until
a writer cooperates.

Raw addresses are not treated as symbols.  The capture binds user samples to bounded
`/proc/<pid>/maps` snapshots and binds kernel samples to the exact unstripped kernel
ELF hash used for that boot.  Anonymous/JIT addresses remain explicitly unresolved.
This custom file is not presented as Linux ABI and may later be replaced by a real
perf-event implementation.

## Guest capture and workload

Extend `browser_system_time.py` to parse schedstat strictly and publish schema version
2.  Every sampled TID gains cumulative/delta CPU runtime, completed runqueue wait,
dispatch count, state, last CPU, and affinity.  Counter regression, malformed fields,
PID/TID reuse, or a changed process start time fails closed.  The old schema remains
readable by existing tools but is not silently relabelled as schema 2.

PC rings are read every 250 ms for at most eight explicitly selected TIDs, and records
are deduplicated by sample ID.  The sampler does not read the custom file for every
Firefox thread.  A CPU/schedstat pass first selects the hottest TIDs, and a repeated
same-boot workload then captures their PCs.  A 32-slot ring can retain the worst-case
25 samples produced for one continuously running TID between reads.  This keeps
collection cost independent of Firefox's total thread count without retaining only
the final fraction of a long phase.

One physical boot runs the following bounded phases with the same kernel, rootfs,
display mode, Firefox process, and local fixture:

1. idle desktop control;
2. three repetitions of the deterministic local keyboard/pointer/scroll browser workload;
3. three repetitions of local 16-resource navigation;
4. one public Baidu navigation, recorded separately because network and site variance
   make it corroborating evidence rather than the optimization gate.

Each phase records start/end guest-monotonic time, schedstat deltas, task states, PCs,
browser timing, system CPU/context switches, process identity, kernel/rootfs hashes,
and terminal success or failure.  Multiple phases occur in one boot; no partition
rewrite or board reset is part of the measurement loop.

## Analysis and optimization admission gate

The analyzer classifies each hot thread interval as:

- executing: CPU time dominates wall time;
- runnable-delayed: completed runqueue wait is material relative to CPU time;
- sleeping/blocking: wall time grows while CPU and runnable wait do not;
- mixed or unresolved: no single boundary dominates or samples are incomplete.

PC frequencies are reported separately for user and kernel mode as raw address,
module/offset where resolvable, sample count, and fraction of accepted samples.  A
kernel function becomes an optimization candidate only when it is repeated across at
least three local-workload phases or runs and agrees with the time classification.
Public-page-only hotspots do not justify a kernel change.

After attribution, the next subproject must name one mechanism, add a failing
microbenchmark/regression for it, and compare the same workload before and after.
Examples such as futex wake classification, page-cache behavior, or scheduler policy
remain hypotheses until they pass this gate.  DRM and framebuffer ownership remain
with the separate DRM team.

## Formal and test strategy

A finite TLA+ model covers `Blocked`, `Queued`, and `Running` states for two tasks and
two CPUs, including duplicate wake, preemption, block/wake, yield, and migration.  It
checks:

- a task is in exactly one scheduling state;
- queued timestamps are present only while queued;
- dispatch count equals completed queued-to-running transitions;
- accumulated wait equals the sum of completed queued intervals;
- counters never decrease;
- duplicate enqueue cannot reset the beginning of a wait.

Negative configurations deliberately reset the timestamp on duplicate enqueue and
start wait accounting when a task blocks; both must produce counterexamples.  The
model establishes the finite state-machine contract, not Rust weak-memory correctness.

Kernel unit tests use injected timestamps to cover spawn/dispatch, preempt/requeue,
sleep/wake, duplicate enqueue, migration, saturation, and conversion.
Procfs tests verify exact formatting and leader/thread selection.  A userspace
regression pins two threads to one CPU and establishes that a busy waiter accumulates
wait/dispatch deltas while a sleeping control does not.

PC-ring tests force reads during odd and overwritten slot generations, require bounded
skip behavior, test exec invalidation and alien-access denial, and verify that profiling
disabled leaves the ring unchanged.  RISC-V and x86-64 QEMU tests cover the standard
ABI; the physical board is used only after host and QEMU gates pass.

## Completion criteria

- Linux-shaped leader and per-thread schedstat files pass kernel and userspace tests.
- The accounting state-machine model passes and both negative controls fail as expected.
- Opt-in PC sampling is bounded, permission-checked, and measurably nonblocking.
- Browser schema 2 preserves raw counters, limitations, and exact run provenance.
- One safe physical boot produces all four phase records or a precise terminal failure.
- Evidence identifies one qualified optimization target or honestly concludes that the
  current sample is insufficient; no speculative kernel change is bundled with the
  profiler.
