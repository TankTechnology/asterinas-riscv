# Logging and scheduling performance attribution

## Status and scope

The user approved the staged approach on 2026-09-13:
measure logging critical paths and CPU-pinned scheduling workloads first,
fix demonstrated bottlenecks with concurrency checks second,
then qualify Firefox startup, first paint, and interaction latency.
The user approved this written specification in the following turn.

This first subproject delivers measurements and reproducible probes.
It does not replace the scheduler, introduce an asynchronous console worker,
change regression deadlines, or claim a Firefox performance fix.
Those changes require a mechanism-specific follow-up after attribution.

## Why this approach

Three approaches were considered:

- Measure logging and scheduling separately, then select a targeted fix.
  This is the selected approach because it distinguishes observation overhead
  from runnable-thread delay without first changing either mechanism.
- Rewrite console output as asynchronous immediately.
  This could shorten critical sections but introduces queue ownership,
  overflow, wakeup, early-boot, and panic-path questions before quantifying the cost.
- Begin with full Firefox runs.
  These measure user-visible behavior but combine scheduling, rendering,
  filesystem activity, and profiling overhead in one result.

## Confirmed code paths and open hypotheses

`kernel/comps/logger/src/aster_logger.rs` records to the bounded memory log,
then synchronously prints eligible records.
`kernel/comps/logger/src/console.rs` holds the console registry's
`LocalIrqDisabled` spinlock across formatting and device sends.
The RISC-V DW APB UART polls with a finite budget;
its diagnostic path treats busy/timeout as best-effort success.
Therefore retained log records and delivered serial bytes are different quantities.
The IRQ-disabled serial path is confirmed; its contribution to stalls is not measured yet.

`kernel/comps/uart/src/console.rs` can also call output-readiness callbacks
from the diagnostic path.
The installed serial TTY callback reaches `Pollee::notify`.
Audit the complete callback and lock chain before changing this path;
this observation alone is not evidence of a deadlock.

The fair scheduler groups `Yield` with tick-based accounting and does not
unconditionally select another fair thread on every yield.
CPU selection prefers a thread's previous eligible CPU.
These are attribution hypotheses, not established ABI defects.
[Linux sched_yield(2)](https://man7.org/linux/man-pages/man2/sched_yield.2.html)
explicitly cautions that normal-policy yield semantics are unspecified.
[Linux v6.12 fair.c](https://github.com/torvalds/linux/blob/v6.12/kernel/sched/fair.c)
is a reference for accounting and policy reasoning, not a drop-in implementation.

## Measurement contract

### Logging

Use opt-in, allocation-free diagnostic instrumentation around existing stages:
memory-record preparation/publication, console lock acquisition,
and the locked formatting/send interval.
Report the latter as a combined interval unless a separate device-send timer exists.
Do not label the whole call as interrupt-disabled time:
only the lock-related interval establishes that property.

Collect count, total duration, and maximum duration per stage.
Use the architecture counter with its frequency and clock-validity checks;
jiffy-based log timestamps are not adequate for microsecond attribution.
Document counter serialization, migration, wraparound, and initialization assumptions.
An unsupported clock must yield an explicit unavailable result, not zero latency.
Measure empty instrumentation overhead and compare instrumented/uninstrumented builds.

Never log, allocate, sleep, or wake readers from the measurement update path.
Use bounded storage and read summaries after the measured workload stops.
Do not add a public syscall or invent a Linux procfs ABI solely for this probe.
Record attempted output bytes separately from actual delivered bytes;
do not claim a delivery count that the driver interface cannot provide.

### Scheduling

Use a standalone user-space microbenchmark with explicit CPU affinity,
confirmed through affinity readback before timing.
Measure two-thread handoffs with yield polling and blocking synchronization separately.
Use same-CPU and distinct-CPU placements, and record CPU IDs and policy.
An unsupported affinity request fails or skips explicitly, never silently runs unpinned.

Each mode performs the same fixed number of successful handoffs.
Keep progress printing outside the timed hot loop and maintain a bounded watchdog.
Report completed operations, errors, elapsed time, and latency distribution
from bounded sample storage, including sample count and percentile definition.
Do not relax the existing signal regression's iteration count or deadlines.
The new performance probe supplements, rather than replaces, that regression.

### Experiment matrix and evidence

Begin with short smoke runs, then at least five repetitions per selected condition.
Record all samples, failures, and timeouts; do not retain only the fastest run.
For logging comparisons, alternate console `info` and `error` with identical
memory-capture level, kernel, archive, workload, CPU placement, and input activity.
Record capture level independently of console level.
Include a bounded fixed-volume log workload for repeatability;
interactive USB log bursts are a separate stress condition.

Use QEMU for mechanism and regression checks, not a hardware speed ratio.
Use RockOS and Asterinas on the same board for hardware comparisons,
recording CPU-frequency policy, binaries, firmware, boot IDs, and background conditions
where observable; explicitly list remaining unmatched conditions.
Keep raw logs, SHA-256 identities, commands, ordered completion markers,
and process/runner exit status beside the summary.

## Validation and concurrency boundaries

Test measurement aggregation and result validation before implementation:
empty samples, bad timestamps, counter overflow, incomplete runs,
affinity failure, and nonzero exits must not become passing measurements.
Run native and QEMU smoke tests before deploying the small optional board probe.
Retain the existing signal qualification suite as a correctness guard.

Measurement counters must not participate in scheduler or console synchronization.
If a follow-up changes notification or queue ownership,
first model ownership exclusion, no lost notification, and bounded overflow behavior
with the cached model-checking tools and a deliberately broken negative control.
Document fairness assumptions and the model-to-code correspondence.
A finite protocol model is not proof of Rust implementation or weak-memory correctness.

## Firefox follow-up and operational limits

Reuse the existing browser evidence scripts and optional Gecko profile capture.
Use a deterministic local page before network browsing measurements.
Separate exec-to-window, first-paint/local-page readiness, and interaction latency;
distinguish cold and warm cache runs and profiling-on versus profiling-off overhead.
Profile evidence must belong to the current run and be complete before use.
Do not infer current-run success solely from an old nonempty profile file.

Reuse the persistent development container and existing Debian/Firefox artifacts.
Keep RockOS and the normal boot defaults unchanged during probe development.
Check board health and exact boot-partition space before deployment;
the current boot partition has only about 5 MB free.
Do not automatically reboot, delete images, expand networking scope, or push changes.

## Completion criteria for this subproject

- A reproducible CPU-placement benchmark and validated result summaries exist.
- Logging stage durations and instrumentation overhead are measured or explicitly unavailable.
- Controlled console-level comparisons preserve memory capture and correctness checks.
- Evidence identifies a supported bottleneck or narrows the unresolved hypotheses.
- No claim of scheduler or Firefox improvement is made without corresponding measurements.

Initial observations and exact baseline identities are recorded in
`docs/porting/evidence/2026-09-13-performance-baseline.md`.
