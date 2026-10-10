# RISC-V Kernel-Trap `gp` Migration Fix Design

## Status

This design fixes a RISC-V CPU-local identity violation that can occur when a
kernel-mode trap handler sleeps and the current task resumes on another CPU.
It is intentionally scoped to the architectural state-transfer bug. It does
not change scheduler policy, heap allocation policy, or VM fault semantics.

The existing bounded model and implementation mapping remain in
`tools/verification/riscv_cpu_local_migration/`.
This design targets the model's open implementation obligation for returning
from a kernel trap after migration.
The implementation and deterministic test establish the return instruction's
behavior given a correct live destination base; they do not prove every
scheduler transition or the complete kernel's safety and liveness.

## Classification

The immediate defect is an implementation bug in RISC-V trap-return assembly:
the kernel restores a CPU-owned register from a task-owned stack frame.

The deeper defect is a protocol-design omission. The trap, scheduler, and
CPU-local subsystems do not state a shared ownership rule for architectural
registers across migration. This is not a faulty scheduling algorithm: runqueue
selection, affinity, and load balancing can all behave correctly while the
machine resumes with invalid CPU-local identity.

The useful classification is therefore:

- **implementation failure:** `_trap_return` unconditionally restores `gp`;
- **interface-contract failure:** no explicit contract says that kernel `gp`
  belongs to the executing CPU rather than the task or trap frame;
- **not a scheduler-algorithm failure:** migration is legal at the point where
  the VM fault handler may block.

## Evidence and Counterexample

On RISC-V, Asterinas uses `gp` as the static CPU-local storage base. Every
`cpu_local_cell!` access derives its address from that base.

The following execution is permitted by the current implementation:

1. A task executes kernel code on CPU A with `gp = cpu_local_base[A]`.
2. A kernel access to a user address faults.
3. `trap.S` saves all general registers, including CPU A's `gp`, in a trap
   frame on the task's kernel stack.
4. `trap_handler` re-enables local interrupts and invokes the injected user
   page-fault handler.
5. VM fault handling waits for a lock or I/O. The scheduler switches the task
   out; later it resumes on CPU B.
6. The RISC-V task switch deliberately does not transfer `gp`, so execution on
   CPU B initially has the correct `gp = cpu_local_base[B]`.
7. `_trap_return` loads `gp` from the old trap frame and changes it back to
   `cpu_local_base[A]`.
8. Code running on CPU B reads and mutates CPU A's IRQ, preemption, current-task,
   and heap-cache cells.

This mechanism can explain varying failures with a common CPU-local shape:
Firefox runs have failed in unrelated allocation paths at
`RefCell<LocalCache>::borrow_mut`, and diagnostic runs have observed impossible
IRQ/preemption state. The first corrupted CPU-local access determines the
surface symptom; it need not occur in the page-fault handler that created the
stale `gp`.
Those failure sites alone do not establish that this transition occurred in
each historical Firefox run; integration qualification remains separate.

## Architectural Ownership Model

Architectural state crossing a scheduling boundary is divided into three
classes:

| State class | Examples | Owner | Migration rule |
| --- | --- | --- | --- |
| CPU-bound | kernel `gp`, CPU-local base, current CPU's IRQ/preempt cells | executing CPU | preserve or re-establish the destination CPU's value |
| Task-bound | kernel `sp`, callee-saved registers, task continuation | task | save and restore with `TaskContext` |
| Trap-bound | `sepc`, saved `sstatus`, interrupted general registers | interrupted execution | restore only values whose meaning is valid in the destination privilege context |

RISC-V `gp` has two owners depending on privilege:

- in user mode, it is an ordinary user register and must be restored from the
  user context;
- in kernel mode, it selects CPU-local storage and must describe the CPU that
  is executing at the moment of return.

The saved `sstatus.SPP` bit determines which ownership rule applies.

## Required Invariants

The implementation must maintain these invariants:

1. **Kernel CPU-local identity:** before executing Rust kernel code,
   `gp == cpu_local_base[current_cpu]`.
2. **No task transfer of CPU identity:** a task context switch does not save or
   restore kernel `gp` as task state.
3. **Kernel-trap return rebinding:** returning to S-mode does not overwrite the
   destination CPU's current `gp` with the interrupted CPU's saved value.
4. **User-context fidelity:** returning to U-mode restores the user's `gp`
   exactly as saved.
5. **Borrow locality:** a CPU-local reference remains protected by an IRQ or
   preemption guard and cannot survive a legal task switch.

The existing TLA+ `GpMatchesRunningCpu` invariant expresses items 1 through 3.
`RestoreStaleGp.cfg` is the negative control for the current defect.

## Selected Fix

Change the shared `_trap_return` path in
`ostd/src/arch/riscv/trap/trap.S` to use the saved `sstatus.SPP` value:

- if `SPP == 0`, the return target is U-mode, so load `x3/gp` from the trap
  frame;
- if `SPP == 1`, the return target is S-mode, so leave the current `gp`
  unchanged.

The decision is made while the saved `sstatus` is already in a temporary
register. Another temporary register can hold the `SPP` test because all
temporary registers are restored later in the same path. The implementation
will name the `SPP` mask rather than introducing another literal bit number.

This adds one mask operation and one branch to trap return. Kernel returns
avoid the old `gp` load. It adds no allocation, lock, atomic operation, cache
line sharing, or scheduler work.

### Why the Current `gp` Is Correct

The BSP and every AP initialize `gp` to their own CPU-local base. RISC-V task
context switching does not include `gp`; therefore switching tasks on one CPU
leaves that CPU's base installed. If a blocked task resumes on another CPU, it
inherits that destination CPU's already-installed kernel `gp`. The only broken
transition identified here is the later overwrite from the old kernel trap
frame.

### Nested Traps

For a nested kernel trap, `SPP == 1`; preserving the live kernel `gp` is correct
whether or not migration occurred. A user-origin trap first returns from the
assembly entry to the kernel's `run_user` caller, restoring the kernel-side
`gp` saved on that same CPU. User exception processing and any later migration
happen after that assembly frame has unwound. The eventual return to U-mode
has `SPP == 0` and restores the user's `gp`.

## Rejected Alternatives

### Rewrite the Trap Frame in Rust

Writing the current `gp` into `TrapFrame.general.gp` immediately before
returning could produce the same final register value. It is rejected because
the handler has interrupt early returns and exception branches, making the
invariant dependent on every Rust exit path. The assembly return path is the
single point that knows the return privilege and performs the restore.

### Disable Preemption Across Kernel Fault Handling

Pinning the task from trap entry through trap return would prevent migration.
It is rejected because user-address page faults may wait for locks or backing
I/O. Making that path atomic would turn a legal blocking operation into a
panic or deadlock and would harm latency.

### Repair CPU-local State After a Switch

Clearing IRQ levels, preemption counts, or allocator borrow flags after a
context switch treats downstream symptoms independently. It cannot recover
the correct ownership of every CPU-local cell and can erase evidence or violate
live guards. Such resets are not part of this fix.

## Verification Design

Verification follows a red/green sequence and avoids repeated Firefox A/B
experiments as a discovery mechanism.

### Deterministic Trap-Return Kernel Test

Add a RISC-V-only ktest helper that executes the real `_trap_return` path with:

- local IRQs disabled;
- an S-mode return (`SPP == 1`);
- a deliberately poisoned saved `gp`;
- a continuation that records the `gp` observed after `sret`, restores the
  original `gp` before returning to Rust, and leaves IRQs disabled for the
  existing guard to restore.

The test asserts that the observed value is the pre-call CPU-local base, not
the poison value. The helper is assembled only for `ktest`, so production
kernels do not contain test instructions or exported test symbols.

The unmodified kernel must fail this assertion for the intended reason. The
fixed kernel must pass through the same real return path.

Add a companion user-return test only if the existing user-context tests do not
exercise `gp`; it must prove that `SPP == 0` still restores the saved user value.

### Model and Static Contract

Update the implementation mapping in
`tools/verification/riscv_cpu_local_migration/README.md` to mark kernel-trap
return rebinding as enforced and tested. Re-run all three configurations:

- `RestoreStaleGp` must still produce its designated counterexample;
- `SwitchWhileBorrowed` must still produce its designated counterexample;
- `Correct` must exhaust the bounded state space without an invariant failure.

The model remains an abstraction. The ktest connects its key `gp` transition
to the actual assembly implementation.

### Integration Gates

After the deterministic test passes:

1. build the RISC-V SV39 SMP=4 kernel in the persistent development container;
2. run the relevant RISC-V kernel tests under QEMU;
3. run one bounded QEMU Firefox/WebDriver reproduction and reject any
   CPU-local, IRQ-level, preemption, or allocator-borrow failure;
4. prepare the normal controlled Megrez boot artifact;
5. when the board is available, boot without requesting a physical reset,
   verify Firefox startup, then run the Bilibili muted-playback gate.

The browser and board runs are final integration gates, not tools for selecting
between speculative fixes.

## Systematic Prevention of Similar Bugs

This bug class should be audited by ownership and transitions rather than by
workload symptoms.

### Register-Ownership Audit

For each architecture, maintain a review table for registers and control state
that have kernel meaning across traps and context switches. At minimum, audit:

- RISC-V `gp`, `tp`, `sscratch`, `sstatus`, and `satp`;
- LoongArch's CPU-local base register and trap return state;
- x86-64 per-CPU base state, user FS/GS state, control registers, and return
  privilege transitions.

For every item, reviewers must answer:

1. Is it CPU-bound, task-bound, user-bound, or trap-bound?
2. Which transition establishes it?
3. Which transition is allowed to restore it?
4. Can code between save and restore sleep or migrate?
5. What independent test fails if the ownership rule is violated?

### Transition-Centered Tests

Tests should target the small state boundary directly:

- trap return after a simulated destination-CPU rebinding;
- task switch while CPU-local guards are live must be rejected;
- user return must preserve all user-owned registers;
- architecture-specific CPU-local bases must remain stable across ordinary
  switches and change only with the executing CPU.

Large workloads remain useful as integration coverage but are not substitutes
for these tests.

### Review Rule

Any code that saves and later restores architectural state must document
whether scheduling or migration can occur in between. If migration is possible,
restoring CPU-owned state from the saved frame is forbidden. If migration is
impossible, the mechanism that prevents it must be explicit and testable.

## Acceptance Criteria

- The deterministic S-mode trap-return ktest fails before the fix and passes
  after it.
- U-mode return continues to restore user `gp`.
- The RISC-V SV39 SMP=4 kernel and relevant ktests build and pass.
- The TLA+ negative controls fail as prescribed and `Correct` passes.
- A bounded QEMU Firefox run reaches its success gate without CPU-local
  corruption or allocator-borrow panic.
- A controlled Megrez Firefox and muted-video run passes when board access is
  available.
- No scheduler policy, VM fault blocking rule, allocator policy, or unrelated
  architecture behavior changes as part of this fix.
