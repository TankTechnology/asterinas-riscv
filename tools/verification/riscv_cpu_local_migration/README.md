# RISC-V CPU-local migration model

This bounded TLA+ model checks the concurrency contract between RISC-V
CPU-local addressing, task switching, migration, local IRQ exclusion, and the
CPU-local heap cache. It was added after two independent Firefox/WebDriver
paths stopped in `RefCell<LocalCache>::borrow_mut`: one while constructing a
`/proc/*/mountinfo` path and one while allocating a page-fault waiter.

The model is **not a proof of the Rust or assembly implementation, the compiler,
RISC-V weak-memory behavior, or the complete scheduler**. It exhaustively checks
the finite abstraction described below and makes the remaining implementation
obligations explicit.

## Run

From the repository root:

```sh
bash tools/verification/riscv_cpu_local_migration/run.sh
```

The runner requires Bash, Java 11 or newer, ripgrep, GNU timeout, and the SHA-256
utilities. It never downloads dependencies. It reuses the TLC 1.7.4 JAR cached
by `tools/verification/signal_job_control/run.sh` at
`target/signal-job-control-model/tla2tools-1.7.4.jar` after verifying SHA-256
`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`.

Logs, state databases, counterexample traces, the Java version, and a tabular
result summary are written under
`target/riscv-cpu-local-migration-model/run.*`. Each invocation uses a fresh
directory. A missing or mismatched JAR fails closed and triggers no download.

The runner first requires two deliberately broken modes to fail on their exact
designated invariants. It then requires the corrected mode to exhaust its state
space without an invariant violation. Every accepted case must report nonzero
generated and distinct state counts; syntax failures and timeouts cannot pass
as counterexamples.

## State and operations

The state space has two CPUs and three tasks. Three tasks are necessary to let
one task own a CPU-local cache while another task resumes on the other CPU with
a stale CPU-local identity. The model records:

- current CPU and execution phase for every task;
- the CPU-local base selected by the task's logical `gp` value;
- the `gp` identity saved at deschedule time;
- the target CPU and pending switch/migration state;
- local IRQ state for every CPU; and
- the logical mutable borrowers of each CPU's local heap cache.

The model separates these actions so TLC explores their interleavings:

| Model action | Abstract contract |
| --- | --- |
| `EnterUser` | A kernel task can enter user mode only with IRQs enabled and no live local-cache borrow. |
| `EnterKernel` | A user trap selects the CPU-local identity of the CPU that took the trap. |
| `BeginLocalAlloc` | Disable IRQs on the executing CPU, then borrow the cache selected through `gp`. |
| `EndLocalAlloc` | Release that borrow before restoring local IRQ state. |
| `Deschedule` | Save task state and remove it from its CPU; the correct mode forbids this with a live borrow. |
| `Migrate` | Change the target CPU of a runnable, descheduled task. |
| `Resume` | Place a task on its target CPU; the correct mode establishes that CPU's CPU-local identity. |

## Checked properties

- `TypeOK` constrains every variable to its declared domain.
- `UniqueCpuOccupancy` prevents two tasks from running on one CPU.
- `GpMatchesRunningCpu` requires kernel execution to address the current CPU's
  CPU-local area.
- `BorrowHasLocalIrqsDisabled` requires every borrower to run with its actual
  CPU's local IRQs disabled.
- `BorrowOwnerRunsOnCacheCpu` requires the borrower and selected cache to belong
  to the same CPU.
- `NoTaskOwnsTwoCaches` excludes a task retaining CPU-local borrows across CPUs.
- `NoBorrowConflict` gives each cache at most one logical mutable owner.
- `NoSwitchWithLiveBorrow` forbids a descheduled task from retaining a cache
  borrow.

No fairness or liveness property is asserted. The safety properties constrain
every state reachable through the modeled operations.

## Required negative controls

`RestoreStaleGp` represents a resume path that treats `gp` as task-owned rather
than CPU-owned. Its shortest accepted trace keeps one task borrowing CPU0's
cache, migrates a runnable task to CPU1 while retaining `gp = cpu0`, and lets the
migrated task borrow CPU0's cache too. `NoBorrowConflict` then fails with two
logical mutable owners, matching the class of failure reported by `RefCell`.

`SwitchWhileBorrowed` permits descheduling after `BeginLocalAlloc`. Its shortest
accepted trace leaves the task runnable with a live CPU-local cache borrow, so
`NoSwitchWithLiveBorrow` fails.

These are sensitivity controls, not conclusions about which defect exists in
the current kernel. A passing corrected model says that the stated abstract
rules exclude these failures; code review must still establish that the
implementation follows the rules.

## Code correspondence and open obligations

| Model concept or action | Implementation surface | Status and obligation |
| --- | --- | --- |
| CPU-local base selected by `gp` | `ostd/src/arch/riscv/cpu/local.rs`; `ostd/src/cpu/local/static_cpu_local.rs` | Direct mechanism: `get_base` reads `gp`, and static CPU-local addressing adds an object offset to that value. Open obligation: every kernel entry and resume must make `gp` identify the executing CPU before any CPU-local access. |
| BSP/AP initial CPU-local identity | `ostd/src/arch/riscv/boot/bsp_boot.S`; `ostd/src/arch/riscv/boot/ap_boot.S` | Directly initialized: BSP loads `__cpu_local_start`; each AP loads its supplied CPU-local pointer. This covers boot only, not later trap or migration paths. |
| `EnterUser` / `EnterKernel` | `ostd/src/arch/riscv/trap/trap.S` | Open proof obligation. `run_user` saves kernel `gp` on the kernel stack and user-trap return restores it. The correspondence requires the saved value to remain the executing CPU's base across every allowed scheduling path. |
| `Deschedule` / `Resume` | `ostd/src/arch/riscv/task/switch.S`; `ostd/src/arch/riscv/task/mod.rs` | Deliberate design: `TaskContext` saves `sp`, `ra`, and `s0`-`s11`, but not `gp`, consistent with CPU-owned rather than task-owned `gp`. Open obligation: no surrounding assembly path may later overwrite the CPU-owned value with stale task state. |
| Unique running ownership | `ostd/src/task/processor.rs` | Direct runtime enforcement: `switched_to_cpu.compare_exchange` prevents the same task context from being used concurrently, and release occurs after switching away. This does not by itself prove correct `gp` identity. |
| No switch with a live IRQ-disabled borrow | `ostd/src/task/processor.rs`; `ostd/src/task/atomic_mode.rs` | Direct runtime check: `prepare_task_switch` calls `might_sleep`, which panics if preemption is disabled or local IRQs are disabled. The model treats a successful switch as requiring that check to pass. |
| `Migrate` | `ostd/src/task/scheduler/mod.rs` | Partly enforced: `migrate_current` calls `prepare_task_switch` before moving the current task between run queues. Open obligation: enqueue, remote selection, and resume preserve CPU-local identity. The generic reschedule path also documents a non-atomic decision/switch window. |
| Local IRQ exclusion | `ostd/src/irq/guard.rs` | Direct mechanism: `DisabledLocalIrqGuard` disables local IRQs, is `!Send`, and restores the previous enabled state on drop. Its safety still depends on code not switching CPUs while the guard is live. |
| `BeginLocalAlloc` / `EndLocalAlloc` | `osdk/deps/heap-allocator/src/allocator.rs` | Direct mechanism: small alloc/dealloc disables local IRQs, selects `LOCAL_POOL` with that guard, and then mutably borrows its `RefCell<LocalCache>`. Open obligation: the CPU-local lookup must resolve to the executing CPU for the entire borrow. |
| CPU-local reference soundness | `ostd/src/cpu/local/mod.rs`; `ostd/src/cpu/local/static_cpu_local.rs` | Explicit implementation assumption: comments and unsafe dereference rely on IRQ/preemption exclusion and on `get_base()` naming the current CPU. The TLA+ invariants state this assumption but do not prove the unsafe implementation. |

## Limits

The model uses sequentially consistent atomic actions. It omits RISC-V memory
ordering, TLB behavior, nested interrupt levels, allocator refill internals,
page tables, task priorities, run-queue data structures, and CPU hotplug. It
does not establish that all real context-switch paths call `might_sleep`, nor
that assembly and Rust agree on every stack-frame layout. Those items remain
code-review and regression-test obligations.

The model also does not diagnose arbitrary memory corruption. If targeted
runtime evidence shows that `gp` matches the executing CPU and no switch occurs
with a live borrow, the `RefCell` failure must be investigated outside the two
protocol violations modeled here.
