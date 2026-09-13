# Fair voluntary yield implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the measured same-CPU fair-yield time-slice delay without changing tick scheduling or synchronization.

**Architecture:** Keep current runtime/weight/min-vruntime accounting, the empty-peer check, and the existing locked pick-before-reenqueue sequence. An explicit `Yield` with a fair peer becomes a reason to select another task immediately. Higher-priority queues still win through the existing class selection.

**Tech Stack:** Rust kernel tests, cached TLC finite-state model checker, existing static C handoff benchmark, diskless four-hart QEMU.

The user approved the concrete design in the preceding evidence document
`docs/porting/evidence/2026-09-13-performance-attribution.md` and the subsequent confirmation.
Source baseline: `7c0c5d687`; prior frozen kernel: `03c9af8443227dce76d22820b0f2db0c994271c1b7f1e24288e2af47707bd0c7`.

## Task 1: Deterministic kernel regression

Files: `kernel/src/sched/sched_class/fair.rs`, `kernel/src/sched/sched_class/mod.rs`.

- [x] Add `fair_yield_handoff`, using a real isolated fair queue and unspawned `ThreadOptions::build()` peer.
  With a small, unexpired period time and accounted delta, assert `Yield` requests selection,
  the peer is selected, and virtual runtime still advances by the weighted delta.
  Separately assert an empty fair queue does not request selection,
  an unexpired `Tick` stays put, and an expired `Tick` still requests selection.
- [x] Add `fair_yield_preserves_class_priority`, using an isolated `ClassScheduler` queue,
  to check selected/current/queued identities over peer switches and a higher-priority arrival.
- [x] Run the exact named kernel test against the unchanged decision and retain the assertion failure.

Core intended assertion:

```rust
assert!(rq.update_current(&runtime, &attr, UpdateFlags::Yield));
assert!(Arc::ptr_eq(&rq.pick_next().unwrap(), &peer));
```

## Task 2: Selection model and minimal fix

Files: `tools/verification/fair_yield/` and the single decision in `fair.rs`.

- [x] Model the serialized local select-before-reenqueue transition,
  an externally queued task, empty/nonempty peers, and higher-priority selection.
  Check ownership exclusion, preservation of runnable tasks, voluntary peer selection,
  and class priority within a finite population.
  Include negative controls for ignored yield and an incorrect reenqueue/selection protocol.
- [x] Run cached TLC with a finite timeout and fail closed on syntax errors,
  zero exploration, unexpected exit codes, or missing invariant counterexamples.
  Document model-to-code mapping and unmodeled runtime/weak-memory/migration properties.
- [x] Change only the existing eligibility predicate after successful accounting and the empty check:

```rust
matches!(flags, UpdateFlags::Yield | UpdateFlags::Wait)
```

- [x] Run both exact kernel tests and confirm the old failure now passes.

## Task 3: Differential qualification and review

- [x] Reuse the persistent container and freeze the new RISC-V release kernel.
- [x] Run the identical 20-invocation handoff archive against old/new kernels,
  keeping CPU, affinity, capture/console settings, binary and iteration count fixed.
  Retain all exits and per-run quantile summaries; do not interpret QEMU timing as board timing.
- [x] Repeat the existing 17-program signal suite and logging probe on the changed kernel.
- [x] Compile x86-64 and run relevant exact kernel tests where supported.
- [x] Obtain independent specification and ordinary code review and resolve findings.
- [x] Commit scoped source, models, and evidence locally.

Results and limits are recorded in `docs/porting/evidence/2026-09-13-fair-yield.md`.

No board reboot, boot-default promotion, network integration, or remote push is included.
Firefox whole-application performance and verbose physical logging remain separate qualification steps.
