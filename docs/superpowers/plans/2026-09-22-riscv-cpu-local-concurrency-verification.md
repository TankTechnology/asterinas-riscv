# RISC-V CPU-local Concurrency Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an offline TLA+ model that detects stale RISC-V CPU-local identity and live-borrow task switching, validates the intended protocol, and maps every modeled obligation to Asterinas Rust or assembly.

**Architecture:** A finite two-CPU, three-task state machine represents placement, execution mode, `gp` ownership, local IRQ state, migration, switching, and per-CPU heap-cache borrowers. Two deliberately broken modes must produce prescribed TLC counterexamples before the corrected mode can pass. A README supplies the refinement/code-correspondence layer and states all unproved assumptions.

**Tech Stack:** TLA+, TLC 1.7.4, Bash, existing repository verification conventions.

---

### Task 1: Fail-closed runner and configurations

**Files:**
- Create: `tools/verification/riscv_cpu_local_migration/run.sh`
- Create: `tools/verification/riscv_cpu_local_migration/Correct.cfg`
- Create: `tools/verification/riscv_cpu_local_migration/RestoreStaleGp.cfg`
- Create: `tools/verification/riscv_cpu_local_migration/SwitchWhileBorrowed.cfg`

- [ ] **Step 1: Add the runner before the model exists**

The runner must reuse `target/signal-job-control-model/tla2tools-1.7.4.jar`, verify SHA-256
`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`, use one TLC worker with a 60-second timeout, and write results under `target/riscv-cpu-local-migration-model/run.*`.

Its cases are exactly:

```bash
run_case RestoreStaleGp 12 'Invariant NoBorrowConflict is violated.'
run_case SwitchWhileBorrowed 12 'Invariant NoSwitchWithLiveBorrow is violated.'
run_case Correct 0 'Model checking completed. No error has been found.'
```

For every case, require nonzero generated and distinct state counts. Save negative traces beginning at `State 1:` and require them to be nonempty.

- [ ] **Step 2: Add configurations**

`Correct.cfg` checks all safety properties:

```tla
CONSTANT Mode = "Correct"
SPECIFICATION Spec
INVARIANTS TypeOK UniqueCpuOccupancy GpMatchesRunningCpu
           BorrowHasLocalIrqsDisabled BorrowOwnerRunsOnCacheCpu
           NoTaskOwnsTwoCaches NoBorrowConflict NoSwitchWithLiveBorrow
CHECK_DEADLOCK FALSE
```

`RestoreStaleGp.cfg` checks `TypeOK` and `NoBorrowConflict`; `SwitchWhileBorrowed.cfg` checks `TypeOK` and `NoSwitchWithLiveBorrow`. Limiting negative configurations to their designated property makes the expected counterexample stable.

- [ ] **Step 3: Run the runner and verify RED**

Run:

```bash
bash tools/verification/riscv_cpu_local_migration/run.sh
```

Expected: nonzero exit because `RiscvCpuLocalMigration.tla` does not exist. This proves the gate cannot pass without the model.

### Task 2: State machine and negative controls

**Files:**
- Create: `tools/verification/riscv_cpu_local_migration/RiscvCpuLocalMigration.tla`

- [ ] **Step 1: Define the finite state**

Use two CPUs and three tasks, plus a `NoCpu` value. Define functions for task placement, phase, current and saved `gp` owner, migration target, IRQ state, cache-borrower sets, switch-pending state, and migration-pending state.

The allowed modes are:

```tla
ASSUME Mode \in {"Correct", "RestoreStaleGp", "SwitchWhileBorrowed"}
```

- [ ] **Step 2: Add small protocol actions**

Implement `EnterUser`, `EnterKernel`, `BeginLocalAlloc`, `EndLocalAlloc`, `Deschedule`, `Migrate`, and `Resume`. The corrected `Resume` sets `gpOwner[t]` to the resume CPU. `RestoreStaleGp` restores `savedGp[t]`. Correct descheduling requires the task to own no cache; `SwitchWhileBorrowed` removes this precondition.

`BeginLocalAlloc` adds the task to the borrower set selected by `gpOwner[t]`, while disabling IRQs on the CPU on which the task actually runs. This distinction makes a stale `gp` observable as both an identity error and, after another task borrows the cache, a mutable-borrow conflict.

- [ ] **Step 3: Add named safety invariants**

Define the properties listed in `Correct.cfg`. In particular:

```tla
NoBorrowConflict ==
    \A cpu \in Cpus : Cardinality(cacheBorrowers[cpu]) <= 1

NoSwitchWithLiveBorrow ==
    \A task \in Tasks :
        taskCpu[task] = NoCpu => BorrowedCaches(task) = {}
```

- [ ] **Step 4: Run TLC and verify GREEN**

Run:

```bash
bash tools/verification/riscv_cpu_local_migration/run.sh
```

Expected:

- `RestoreStaleGp`: exit 12, `NoBorrowConflict` counterexample;
- `SwitchWhileBorrowed`: exit 12, `NoSwitchWithLiveBorrow` counterexample;
- `Correct`: exit 0 and no invariant violation;
- final line reports two expected negative controls and the corrected finite model.

- [ ] **Step 5: Commit the executable model**

```bash
git add tools/verification/riscv_cpu_local_migration
git commit -m "test: model RISC-V CPU-local migration protocol"
```

### Task 3: Code correspondence and verification

**Files:**
- Create: `tools/verification/riscv_cpu_local_migration/README.md`

- [ ] **Step 1: Document properties and evidence**

Explain the observed `RefCell<LocalCache>` panic, how both negative traces represent relevant protocol failures, how to run the model, and where artifacts are written.

- [ ] **Step 2: Map model actions to implementation**

Include exact mappings for:

- RISC-V CPU-local base: `ostd/src/arch/riscv/cpu/local.rs` and BSP/AP boot assembly;
- user/kernel crossings: `ostd/src/arch/riscv/trap/trap.S`;
- task switching: `ostd/src/arch/riscv/task/switch.S` and `ostd/src/task/processor.rs`;
- migration: `ostd/src/task/scheduler/mod.rs`;
- IRQ exclusion: `ostd/src/irq/guard.rs`;
- cache lookup/borrow: `osdk/deps/heap-allocator/src/allocator.rs`.

Label each mapping as directly enforced, assumed, or an open implementation obligation. State explicitly that finite model checking does not prove the Rust/assembly implementation or RISC-V weak-memory behavior.

- [ ] **Step 3: Run fresh verification**

Run:

```bash
bash tools/verification/riscv_cpu_local_migration/run.sh
git diff --check HEAD~1 -- tools/verification/riscv_cpu_local_migration \
  docs/superpowers/specs/2026-09-22-riscv-cpu-local-concurrency-verification-design.md \
  docs/superpowers/plans/2026-09-22-riscv-cpu-local-concurrency-verification.md
rg -n 'TBD|TODO|axiom|proved Rust|proves the Rust' \
  tools/verification/riscv_cpu_local_migration \
  docs/superpowers/specs/2026-09-22-riscv-cpu-local-concurrency-verification-design.md
```

Expected: TLC runner passes; diff check is clean; terminology scan contains no placeholders or claims that the model proves the implementation.

- [ ] **Step 4: Commit documentation**

```bash
git add tools/verification/riscv_cpu_local_migration/README.md \
  docs/superpowers/plans/2026-09-22-riscv-cpu-local-concurrency-verification.md
git commit -m "docs: map CPU-local model to kernel code"
```
