# Signal Job-Control M1 Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for the coupled
> kernel work and superpowers:subagent-driven-development for the independent
> model-checking artifact. Steps use checkbox syntax for tracking.

**Goal:** Repair pending/selected STOP cancellation with reproducible model
checking and actual syscall regressions, without claiming complete job control.

**Architecture:** Retain existing queue storage and add one per-process
job-control coordinator. Use revocable per-thread selected-stop state and
commit notifications outside coordination locks. Keep raw queue consumers
separate from action selection. Follow the approved signal-job-control design.

**Tech Stack:** Safe Rust, cached RISC-V GCC/OSDK/QEMU in the persistent
container, host Java plus a pinned TLC jar for mathematical model checking.

## 1. Formal protocol model and negative controls

Files: `tools/verification/signal_job_control/` (new).

- [x] Create finite TLA+ actions for generation, queue selection, CONT
  cancellation, conditional stop commit and exit. Use two threads and both
  process/thread pending routes. Preserve a separate ghost cancellation
  observation so the safety invariant is not merely the implementation guard.
- [x] Check that disabled selected-stop invalidation produces a stale-stop
  counterexample; disabling pending cancellation produces a pending-set
  counterexample. Check the repaired transition system exhaustively.
- [x] Add a separate register/check/park/wake model exercising an early wake,
  including a negative control without remembered wake state. State fairness
  assumptions and limits explicitly: bounded model checking, not Rust or
  RISC-V machine-code verification.
- [x] Provide a repeatable runner with jar version/hash, bounded memory/time,
  nonzero exit checking, saved counterexamples and state counts. No automatic
  large dependency downloads or jar tracked in git.

## 2. Real userspace RED cases

Files: `test/initramfs/src/regression/process/signal/stop_continue_pending.c`,
`test/initramfs/src/regression/process/run_test.sh`.

- [x] Promote the existing standalone blocked-pending probe into a bounded
  permanent regression. Fork each case, block TSTP/TTIN/TTOU and CONT, send
  both orders through kill/tgkill, then assert the old pending bit is cleared
  and the newer one retained. Preserve unrelated pending signals.
- [x] Compile/run natively and with the frozen old kernel using the same C
  source/archive; retain guest result markers, not just QEMU exit status.

The essential failing assertion is:

```c
sigpending(&pending);
assert(sigismember(&pending, first_signal) == 0);
assert(sigismember(&pending, second_signal) == 1);
```

## 3. Queue storage and coordinator

Files: `kernel/src/process/signal/{sig_queues,job_control,mod}.rs`,
`kernel/src/process/{process, posix_thread}/mod.rs` and constructors.

- [x] Add notification-free queue mutation and explicit later notification.
  Discard by mask under the queue mutex; update count by exactly the removed
  entries. Do not call observer callbacks under an outer coordinator guard.
- [x] Add a per-process coordinator and private per-thread selected-stop bit.
  For STOP/CONT generation, hold task membership before coordination, clear
  all affected queues, revoke selected stops for CONT, commit state, enqueue
  the signal, then unlock and notify/wake. Serialize SIGKILL enqueue without
  reacquiring the task-set lock already held by sibling termination. Reject a
  stop with current-thread/shared SIGKILL pending; mark terminal coordinator
  state at actual group-exit commitment, not every SIGKILL generation (exec
  also kills siblings). Full Linux eager fatal-generation behavior is outside
  M1. Do not acquire task/disposition locks inside coordination.
- [x] Keep the predicate and update in one critical section:

```rust
// Protocol contract; concrete types are private to signal/job_control.rs.
if !group_exiting && selected_stop_is_valid {
    selected_stop_is_valid = false;
    // Commit the real process stop state before releasing coordination.
}
```

- [x] Test masked discard/count and selected -> CONT -> commit through the
  real private implementation. Test a new selection after cancellation and
  exit precedence. No production test sleeps or forced yield hooks.

## 4. Delivery integration and regression

Files: `kernel/src/process/signal/{pending,mod}.rs`, ptrace integration only
where needed; `kernel/src/syscall/rt_sigaction.rs` if raw discard API changes.

- [x] Mark eligibility atomically with delivery selection and before a ptrace
  wait; generic signalfd/sigtimedwait consumption remains action-free.
- [x] Validate at stop commit, remove the now-redundant default CONT resume,
  retain Linux-compatible blocked ptrace requeue behavior and origin.
- [x] Run native/C/QEMU pending and existing stopped-CONT cases, existing kill,
  job_control, signal_test2, signalfd and timed-wait tests where available.
- [x] Build offline using cached OSDK, freeze kernel and artifacts, run
  diskless Basic/Probe. Review model-to-code correspondence and lock ordering.

Existing baseline build command (inside the named container):

```sh
cd /root/asterinas/kernel
OSDK_TARGET_ARCH=riscv64 OSDK_LOCAL_DEV=1 CARGO_NET_OFFLINE=true \
  CARGO_BUILD_JOBS=4 ../osdk/target/debug/cargo-osdk osdk build --release \
  --scheme riscv --features riscv_sv39_mode \
  --initramfs ../target/shutdown-latency/stage1/initramfs.cpio
```

## 5. Handoff gates

- [x] Ordinary spec review, then code-quality review; correct findings and
  rerun relevant tests. Keep model checker evidence distinct from integration.
- [x] Commit scoped changes and evidence locally; do not push or replace the
  installed networking kernel/default menu.
- [x] Only consider physical testing once M1 is green. Stopped-handler gating,
  group-stop participation/reporting (M2) and final three-run normal-console
  recovery qualification (M3) remain separate steps of the approved design.
  If M1 does not explain shutdown, continue diagnosis rather than claim success.
