# Performance attribution implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attribute verbose-log timing instability and scheduling handoff cost before changing kernel policy, then use those findings to guide Firefox optimization.

**Architecture:** Keep probes outside the signal correctness suite. Use fixed-affinity user-space handoffs and opt-in bounded logging measurements; retain raw failures and compare identical workloads. No new syscall, scheduler replacement, timeout relaxation, or default boot change.

**Tech Stack:** Static C/pthreads, Python standard-library tests, existing Rust/OSDK toolchain, cached RISC-V QEMU and model checker.

## Task 1: CPU-pinned handoff probe

Files: create `tools/benchmarks/sched_handoff/{sched_handoff.c,test_probe.py,README.md}`.

- [x] Write integration tests first, invoking the real executable and checking nonzero exit on missing arguments, invalid modes, out-of-range CPUs, zero/excessive iterations, and unsupported affinity.
  Test successful same-CPU yield/blocking and distinct-CPU blocking when two CPUs are available.
  Validate output schema, requested/confirmed CPU IDs, completion count, positive elapsed time, and `min <= p50 <= p95 <= p99 <= max`.
- [x] Run `python3 tools/benchmarks/sched_handoff/test_probe.py` in the persistent container and record the missing-probe failure.
- [x] Implement CLI `sched_handoff MODE CPU_A CPU_B ITERATIONS`, where MODE is `yield` or `blocking`, and ITERATIONS is 1 through 100000.
  Select fixed affinity separately in each thread and verify readback before timed work; report SCHED_OTHER policy or explicitly reject other policies.
  Use a release/acquire atomic turn for yield mode, two semaphores for blocking mode, and a startup barrier outside timing.
  Record one round-trip duration per completed iteration in a preallocated bounded array; print only after both threads finish.
  Use a 20-second process alarm with `_exit(124)` only;
  even a fixed diagnostic write can block when stdout and stderr share a full pipe.
  Reject failures without printing a success record; handle EINTR on semaphore waits.
  Produce one JSON object with mode, CPU IDs, iterations, completed, elapsed_ns, sample count, and nearest-rank min/p50/p95/p99/max in ns.
- [x] Run the same integration command, compiling with `cc -O2 -pthread -Wall -Wextra -Werror`; retain red and green outputs.
- [x] Cross-compile with cached RISC-V GCC, run QEMU smoke before RockOS comparison, then five samples per supported placement/mode.
  Check raw exit and JSON, not only shell-loop status.
- [x] Obtain spec review, then normal code review; fix findings before local commit.

## Task 2: Logging attribution and lock-chain audit

Files: inspect `kernel/comps/logger/src/{aster_logger.rs,console.rs,klog.rs}`, `kernel/comps/uart/src/console.rs`, and `kernel/src/process/signal/poll.rs`.
Implementation uses `diagnostics.rs`, independent `diagnostics_stats.rs` and host `diagnostics_tests.rs`;
`tools/riscv/diagnostics/log_profile.py` and its tests validate complete boot evidence.
The probe uses stack-owned observers instead of global instrumentation counters.
It is explicitly a user-record path probe, not a full kernel-formatting profile.

- [x] Trace diagnostic output through TTY readiness to observer wakeup and identify all locks held, distinguishing executable edges from hypothetical deadlocks.
- [x] Test a bounded duration accumulator before implementation: empty snapshot, valid count/total/max, reversed clock, overflow, and concurrent observations.
- [x] Add opt-in measurements for memory preparation/publication, console-lock wait, and locked formatting/send; no I/O or callbacks in the accumulator.
  Use supported architecture clocks only and return unavailable for unsupported/uninitialized timing; include instrumentation overhead.
- [x] Verify formatting and compilation with the persistent container, run focused tests, and measure short QEMU logging workloads before board deployment.
- [x] Preserve raw measurements and state whether callback time is included; do not report attempted bytes as delivered bytes.

## Task 3: Evidence and fix selection

Files: append a dated evidence document under `docs/porting/evidence/`.

- [x] Record exact binaries, kernels, affinity readback, capture/console levels, repetitions, commands, exits, and unmatched conditions.
- [x] Run existing signal regressions against any changed kernel; retain failure samples and unchanged deadlines.
- [ ] Select a follow-up only when a measured mechanism explains a difference.
  For changed wakeup/queue protocols, model exclusion and no-lost-wakeup with a failing negative control before implementation.
- [ ] Reuse Firefox local-page and Gecko-profile evidence after the kernel comparison; distinguish startup, paint, interaction, and profiler overhead.

## Execution notes

Reuse `/home/ubuntu/.config/superpowers/worktrees/asterinas/megrez-boot-main`
and container `asterinas-dev-v1-4f054ba7e4d3-b058e7f2c917`.
The source baseline is `2e812a3f8`; the worktree was clean before this plan.
Initial existing-binary tests are recorded in `docs/porting/evidence/2026-09-13-performance-baseline.md`.
Do not create a fresh container, install dependencies, push, or modify board boot defaults.

Results and remaining limits: `docs/porting/evidence/2026-09-13-performance-attribution.md`.
The pure same-CPU yield probe now reproduces a roughly 12 ms round trip,
matching the current fair scheduling period calculation.
The concrete minimal yield-change design was presented to the user;
implementation is pending that response.
Physical Asterinas logging measurement and Firefox qualification remain outstanding.
