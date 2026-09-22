# RISC-V CPU-local Concurrency Verification Design

## Objective

Build a bounded TLA+ model of the RISC-V CPU-local execution protocol and a
reviewable mapping from every model action and invariant to the relevant Rust
or assembly implementation. The work targets the observed failure where
`HeapAllocator::alloc` finds a CPU-local `RefCell<LocalCache>` already mutably
borrowed while handling unrelated user work.

This scope deliberately excludes Lean and does not claim to prove the Rust
implementation, compiler, hardware, or complete scheduler. It provides:

1. exhaustive checking of the stated finite concurrency abstraction;
2. required counterexamples for deliberately broken protocol variants; and
3. explicit implementation obligations that can be reviewed against code.

## Evidence Being Explained

Two Firefox/WebDriver runs reached different kernel paths before failing:

- `/proc/*/mountinfo` path construction allocated from the local heap cache;
- user-page-fault handling allocated a `Waiter` from the same cache.

Both stopped in `RefCell<LocalCache>::borrow_mut`. The allocator disables local
IRQs before obtaining the CPU-local cache, so an ordinary same-CPU interrupt
cannot explain a second mutable borrow. Earlier diagnostics also observed
inconsistent IRQ/preemption state across CPUs. The model therefore treats
CPU-local identity, migration, task switching, and local-cache ownership as one
protocol rather than modeling Firefox or VFS behavior.

## Deliverables

Create `tools/verification/riscv_cpu_local_migration/` containing:

- `RiscvCpuLocalMigration.tla`: the state machine and safety invariants;
- `Correct.cfg`: the intended protocol;
- `RestoreStaleGp.cfg`: a negative control that restores a task-associated,
  stale CPU-local base after migration;
- `SwitchWhileBorrowed.cfg`: a negative control that permits a task switch
  while a CPU-local cache borrow is live;
- `run.sh`: an offline, fail-closed TLC runner following existing repository
  verification conventions;
- `README.md`: properties, counterexample interpretation, exact code mapping,
  assumptions, and limits.

Generated logs and traces live below
`target/riscv-cpu-local-migration-model/` and are not committed.

## Abstract State

The bounded state space contains two CPUs and two tasks. This is sufficient to
represent one task migrating while another execution context owns a per-CPU
cache. State variables record:

- the current task on each CPU;
- each task's execution mode: runnable, kernel, or user;
- the CPU on which each task is placed;
- the CPU-local base selected by `gp` for each running kernel context;
- local IRQ enabled/disabled state for each CPU;
- the owner, if any, of each CPU's `LocalCache` mutable borrow;
- whether a task has a saved CPU-local identity that may be restored;
- whether migration or a context switch is pending.

The model does not represent heap contents. Cache borrowing is the smallest
state needed to express the observed exclusivity failure.

## Transitions

The model exposes small atomic actions rather than one monolithic scheduler
step:

1. `EnterKernel` and `LeaveKernel` model user/kernel crossings.
2. `BeginLocalAlloc` disables local IRQs and borrows the cache selected by
   `gp`.
3. `EndLocalAlloc` releases that cache and restores the prior IRQ state.
4. `Deschedule` removes a kernel task from a CPU when the switching preconditions
   hold.
5. `Resume` places a runnable task on a CPU and establishes the CPU-local base.
6. `Migrate` changes the eligible CPU for a descheduled task.

`Mode = "Correct"` derives `gp` from the CPU on every kernel resume and forbids
descheduling while a local-cache borrow is live.

`Mode = "RestoreStaleGp"` restores the task's saved CPU-local identity after
migration. This must produce a counterexample in which two contexts address the
same per-CPU cache.

`Mode = "SwitchWhileBorrowed"` allows a task to be descheduled while retaining
a live cache borrow. This must also produce a cache-ownership counterexample.

These negative controls demonstrate sensitivity to both suspected classes of
implementation defect; they do not assert which defect exists in the current
kernel.

## Safety Properties

The runner checks these invariants:

- `TypeOK`: every state value remains in its declared domain.
- `GpMatchesRunningCpu`: every task executing kernel code addresses the
  CPU-local area of the CPU on which it runs.
- `BorrowHasLocalIrqsDisabled`: a borrowed CPU cache belongs to a CPU whose
  local IRQs are disabled.
- `BorrowOwnerRunsOnCacheCpu`: the borrow owner is running on the CPU whose
  cache it owns.
- `NoTaskOwnsTwoCaches`: a task cannot retain CPU-local borrows across CPUs.
- `NoBorrowConflict`: a cache never has two logical mutable owners.
- `NoSwitchWithLiveBorrow`: descheduled or migrating tasks own no CPU-local
  cache.

The corrected configuration must exhaust its finite state space with no
invariant violation. Each negative configuration must fail on its designated
invariant, with nonzero generated/distinct state counts and a saved
counterexample trace. Syntax errors, Java failures, timeouts, missing state
counts, or a negative control that unexpectedly passes all cause `run.sh` to
fail.

## Code Correspondence

The README will map model concepts to these implementation surfaces:

| Model concept | Implementation surface |
| --- | --- |
| CPU-local base selected by `gp` | `ostd/src/arch/riscv/cpu/local.rs`, BSP/AP boot assembly |
| User/kernel crossing and `gp` save/restore | `ostd/src/arch/riscv/trap/trap.S` |
| Task context switch | `ostd/src/arch/riscv/task/switch.S`, `ostd/src/task/processor.rs` |
| Reschedule and migration | `ostd/src/task/scheduler/mod.rs` |
| IRQ-disabled guard | `ostd/src/irq/guard.rs` |
| CPU-local cache lookup and borrow | `osdk/deps/heap-allocator/src/allocator.rs` |

For each row, the documentation will state whether the implementation directly
enforces the modeled precondition or whether it remains a review obligation.
Unproven obligations will be labeled explicitly; model success cannot discharge
them automatically.

## Verification Workflow

The runner reuses the repository's pinned TLC 1.7.4 JAR after checking its
SHA-256 and never downloads dependencies. It executes negative controls first,
checks their exact expected failures, and then checks the corrected model.

Implementation follows a red/green sequence:

1. add the negative-control configurations and runner expectations;
2. demonstrate that the incomplete/correct model cannot yet satisfy the runner;
3. implement the protocol and invariants;
4. require both negative controls to emit the intended counterexamples;
5. require the corrected configuration to complete without errors;
6. run repository formatting and diff checks for the new verification files.

The full Firefox workload remains a later integration test. It is not part of
the formal model and will not be repeatedly used to discover the protocol.

## Acceptance Criteria

- One command, `bash tools/verification/riscv_cpu_local_migration/run.sh`,
  performs all checks offline.
- Both broken modes fail for their prescribed semantic reason.
- The corrected mode exhaustively checks with no invariant violation.
- Every invariant and transition has an implementation correspondence entry.
- The README distinguishes finite model checking from proof of implementation.
- No Lean dependency or proof project is introduced.
