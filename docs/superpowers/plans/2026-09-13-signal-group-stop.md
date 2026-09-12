# Signal Group Stop and Physical Qualification Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for coupled kernel
> integration and superpowers:subagent-driven-development for the independent
> formal models and ordinary reviews. Track completion with checkboxes.

**Goal:** Implement stopped-thread signal delivery and complete group-stop
participation/reporting, then verify the result on the Megrez board.

**Final status:** This scoped signal-correctness milestone is qualified and
saved on the local branch. Final RISC-V software suite: 17/17; two physical
error-console/info-klog suites: 17/17 each. Eight finite models and 21 negative
controls have expected outcomes. Verbose-console timing failures and desktop/
scheduler performance remain explicit follow-up work, not claimed fixed.

**Architecture:** Extend the M1 coordinator with explicit Running, Stopping,
Stopped and Exiting phases. Per-thread participation is enrolled under stable
membership, acknowledged at a stop checkpoint, and cancelled by CONT or exit.
Keep action selection, parent notifications, ptrace stops and user ABI distinct.

**Tech Stack:** Safe Rust, C syscall tests, cached Docker/OSDK/GCC/QEMU, pinned
TLA+/TLC, existing serial/boot/recovery tools. Baseline: `a98487a79`.

The preceding [Linux/OSTEP design](../specs/2026-09-12-signal-job-control-design.md)
separates Linux ABI requirements from OSTEP's predicate/wakeup/atomicity guidance.
Its historical "proposed" status is not a current implementation report; the
checkpoints in this plan record the implementation and experimental evidence.

## 1. Reproduce stopped delivery and premature group reporting

Files: `test/initramfs/src/regression/process/signal/group_stop.c`, signal
Makefile, process runner; ignored artifacts under `target/signal-group-stop/`.

- [x] Add bounded subprocess cases: STOP then TERM stays pending and does not
  terminate until CONT; STOP then caught USR1 retains its pending bit until CONT;
  SIGKILL terminates without CONT. Use wait-confirmed STOP before sending.
- [x] Add multithreaded busy-user and interruptible-sleep participants. After
  WSTOPPED, shared userspace counters must be stable and every live sibling
  must be in a stop state. Continue and terminate through both signal routes.
- [x] Compare native Linux with the frozen M1 QEMU kernel using guest result
  markers, preserving exact images and failing assertions. Test child waits
  have deadlines and SIGKILL/reap cleanup; outer QEMU bounds kernel hangs.

Observable contracts include:

```c
kill(child, SIGSTOP);
waitpid(child, &status, WUNTRACED); /* require WIFSTOPPED */
kill(child, SIGTERM);
/* After a bounded observation window: waitpid(..., WNOHANG) == 0. */
kill(child, SIGCONT);
waitpid(child, &status, 0); /* require SIGTERM termination */
```

## 2. Stopped-thread delivery and group participation

Files: `kernel/src/process/signal/{job_control,mod,pause,pending}.rs`,
`kernel/src/process/{status,clone}.rs`, process/posix_thread modules and exit,
`kernel/src/thread/task.rs`.

- [x] Make stopped waits interrupt only for SIGKILL (CONT changes the predicate).
  Gate ordinary delivery under the signal coordinator, retaining ordinary
  pending payloads/masks. Check before first userspace entry as well as returns.
- [x] Replace immediate stop-report commitment with phase and participation
  methods. Initiation takes TaskSet -> coordinator, validates the M1 selection,
  marks all eligible live members pending, and wakes them after unlocking.
- [x] A checkpoint takes coordinator -> thread state, consumes its current
  pending marker once, and reports STOPPED only at the last outstanding ACK.
  It never carries an old decrement across unlock or uses a wrapping epoch.
- [x] Clone publishes an enrolled member under TaskSet -> coordinator. Joining
  Stopped blocks first userspace entry but does not create a second report.
  Exit of a pending member consumes one count; acknowledged exit consumes none.
  Exec sibling termination does not permanently mark the new process exiting.
- [x] CONT cancels selected and participation markers/count before waking;
  actual group exit dominates stop work and wakes/terminates all siblings.
  Ordinary syscall waits and user-mode event polling notice pending stop work
  even after the original stop signal has been removed from the shared queue.
- [x] Exercise actual private Rust state: two ACKs, duplicate ACK, join during
  stopping/stopped, pending/acknowledged exit, CONT -> new STOP -> checkpoint,
  and group exit. Keep model-only tests separate from real implementation tests.

The counter transition must be a single lock-held operation:

```rust
// Contract: consume the current participant marker under the coordinator.
if participation.is_pending() {
    participation.acknowledge();
    group.acknowledge_member();
    // Only a transition completing Stopping publishes the stopped wait event.
}
```

## 3. Reporting, tracing and model checks

Files: process/status and wait paths, signal job-control module, ptrace modules,
`test/initramfs/src/regression/process/ptrace/ptrace.c`, and
`tools/verification/signal_job_control/`.

- [x] Implement Linux-compatible CLD_STOPPED/CLD_CONTINUED SIGCHLD payloads and
  SA_NOCLDSTOP behavior, distinct from wait status and coalesced pending signals.
  CONT interrupting Stopping reports the Linux interrupted-stop notification;
  WCONTINUED remains observable. WNOWAIT must not consume the wait event.
- [x] Preserve ptrace delivery-stop versus group-stop separation. CONT must not
  release a ptrace stop. Test stop completion involving traced siblings, tracer
  restart/detach, and injected or suppressed stop signals, with bounded cleanup.
- [x] Extend TLC with three bounded members and repeated stop episodes. Verify
  premature reporting, missing join enrollment, double exit decrement and stale
  saved-decrement counterexamples; verify the corrected graph exhaustively.
  Document that ACK reads current participation at the checkpoint, not a stale
  externally captured token. Keep fairness/memory-model limitations explicit.
- [x] Perform ordinary specification then quality review of the model and
  actual lock graph/lifecycle integration; correct findings and rerun tests.

## 4. Software verification and frozen candidate

- [x] Reuse `asterinas-dev-v1-4f054ba7e4d3-b058e7f2c917`, mounted at
  `/root/asterinas`, without replacing its image or downloading a toolchain.
- [x] Run exact-name kernel tests (not a zero-matching module filter), native
  and four-hart QEMU M1/M2 regressions, signalfd/sigtimedwait, kill, job control,
  ptrace, clone/exec tests, Basic/Probe, formatting and cached clippy checks.
- [x] Run bounded multi-process/thread STOP -> TERM -> CONT workloads using
  real handlers, retaining guest completion markers and shutdown deadlines.
- [x] Freeze kernel/initramfs/hash/log artifacts; keep installed networking
  and default menus unchanged.
- [x] Commit reviewed scoped changes locally after final integration gates.

Cached build command inside the container:

```sh
cd /root/asterinas/kernel
OSDK_TARGET_ARCH=riscv64 OSDK_LOCAL_DEV=1 CARGO_NET_OFFLINE=true \
CARGO_BUILD_JOBS=4 ../osdk/target/debug/cargo-osdk osdk build --release \
  --scheme riscv --features riscv_sv39_mode \
  --initramfs ../target/shutdown-latency/stage1/initramfs.cpio
```

## 5. Real-board qualification (required, not replaced by QEMU)

- [x] Read-only confirm current RockOS/firmware state, serial ownership, storage
  mounts and current default artifact identities; use existing recovery policy.
- [x] Transfer only frozen changed artifacts using existing RockOS/network
  staging. Reuse the existing optional candidate menu and normal boot flow.
- [x] Run lightweight real-board signal/stop probes first; recover safely.
- [x] Run the normal tty0 desktop configuration with unchanged Debian, timeout,
  boot policy and logging baseline. Verify local Firefox page and capture via
  the existing framebuffer mechanism rather than requiring user HDMI photos.
- [x] Obtain at least three normal-configuration shutdown/recovery results.
  If escalation remains, inspect existing provenance and waiting states and
  continue diagnosis; do not call it a latency fix or shorten sync/timeouts.
- [x] Finish in verified RockOS with unmounted partition integrity checked,
  defaults unchanged, and record evidence/remaining limitations. A temporarily
  unavailable board does not turn simulated success into goal completion.
- [x] Qualify the latest logical-stop observer candidate (`dc25f4...`) on the
  board after a recoverable `/boot` capacity preflight. The checked hardware
  items above record earlier candidates; the final section qualifies this
  new image in the explicit error-console/info-klog condition.

## 2026-09-13 software checkpoint (not M2 completion)

All work remains local and uncommitted; no board artifacts or defaults have
been replaced. Frozen images and logs are under `target/signal-group-stop/`.

| Contract | Earlier failure | Current measured result |
| --- | --- | --- |
| Group stop and ordinary pending signals | M1 regression 2/7; missing proc pending fields confound the initial USR1 observation | `timens-green`: 7/7, including 32 consecutive busy-thread STOP/CONT rounds |
| Interrupted blocking read | Participation candidate 0/4, spurious EINTR without caught handlers | `timens-green`: 6/6, including caught handlers with and without SA_RESTART |
| Parent stop/continue events | Missing SIGCHLD; later exit event incorrectly SI_KERNEL | `timens-green`: 2/2, including WNOWAIT and user-handler SA_NOCLDSTOP |
| Sleep restart | Notification candidate 3/9; six no-handler cases returned EINTR | `timens-green`: 9/9, preserving original relative deadlines |
| Namespaced sleep | Sleep candidate verified offsets, then 50 ms nanosleep exceeded the 2 s parent bound | `timens-green`: 5/5 at 50 ms each, MONOTONIC +60 s and BOOTTIME +120 s |
| Restored user register versus internal restart | Notification candidate: direct restart_syscall ENOSYS; restored -512 misinterpreted | `timens-green`: direct restart_syscall EINTR and user-restored errno 512 preserved |
| pause/sigsuspend through STOP/CONT | `wait-red`: 0/4, no handler executed | `wait-green`: sigsuspend 4/4, including queued-while-stopped signals; libc pause still 0/2 because RISC-V glibc uses ppoll |

`timens-green` uses kernel SHA-256
`b9e94312e288d79274fe50c2871d9e68806590082d0364ac7c42ca80b8d88de4`.
The original Stage1 remains SHA-256
`9ef253dadad3f399e6152ad8681319725e99562081f237ef851a7ec11c3ba405`;
each run records the derived small regression archive separately.
`wait-green` is an explicitly **failing** candidate, SHA-256
`c23942187b8ad174249e250ba97472e0f4794f42517159214e169f0656ff4d7f`.

The eight existing M1 regression programs all exited zero in `m1-final-qemu`:
stop_continue, stop_continue_pending, signal_fd, signal_test2, kill, ptrace,
job_control and rseq. Basic/Probe simulation passed in `timens-basic-probe`.
These existing ptrace tests do not cover full traced group-stop semantics.

Five exact-name kernel tests actually ran and passed one test each, with logs
named `*-isolated-ktest.log`: participation, cancellation/rejoin, selected-stop
cancellation, pending-KILL/exit dominance, and signed deadline conversion.
The first deadline test attempt timed out while constructing namespace/pseudofs
objects in the minimal ktest environment; it is not counted as a pass.
The revised test exercises the production arithmetic helper independently;
namespace inheritance and clock selection are verified by the user-space test.

Main-agent rerun of the final model files completed at
`target/signal-job-control-model/run.JFrAx4gj/`: all nine fault-injection
configurations failed their intended properties, and all three corrected
configurations exhausted their finite graphs. GroupStop explored 35,805
distinct states. This is not a Rust refinement, weak-memory, notification-order,
or unbounded liveness proof. See the model README for operation mappings.

### Required follow-up before candidate qualification

- Relative FUTEX_WAIT and poll/select/pselect6 still need their own
  deadline/restart regressions. ppoll's STICKY_TIMEOUTS personality behavior
  remains unimplemented.
- Run the new x86-64 ptrace-cancelled sigsuspend regression on Asterinas, not
  only the native Linux oracle. The mask restoration implementation is fixed;
  RISC-V cannot exercise this test because ptrace register writes are absent.
- Preserve default/ignored-action flags for complete SA_NOCLDSTOP handling.
- Complete ptrace group-stop versus delivery-stop reporting and lifecycle tests.
- Run broader clone/exec lifecycle, cross-architecture and scoped lint checks;
  ordinary review and a frozen local commit still remain.
- Perform the required physical qualification, including normal-configuration
  desktop recovery. No Firefox latency or shutdown improvement is claimed yet.

The namespace test intentionally uses separately opened single-line writes:
the current timens_offsets proc implementation rejects a Linux-style multiline
write. That independent ABI gap is recorded, not silently counted as timer
coverage or expanded into this signal patch.

### Latest passing candidate: scan-exit (supersedes intermediate candidates)

Kernel: `target/signal-group-stop/scan-exit-kernel.Image`, SHA-256
`e2507a91cd776ff1eb21d7343974c2a3de7b1f7ded4420f6fb2fca2f68242f24`.

`scan-exit-green/result.json` records `passed=true`, QEMU exit 0, 7.143 s,
and derived initramfs SHA-256
`c8f149db45b5f6aae5ca1688e63c3f3308f2b7a709c4da3fe7c1194e0fa845b2`.
All 42 checks passed: group stop 7, parent events 2, read restart 6, sleep 9,
time namespaces 5, restored syscall context 1, pause/sigsuspend 6 and ppoll 6.
The group-stop check includes 32 repeated STOP/CONT rounds.

`scan-exit-m1-qemu/result.json` records all eight preexisting regression
programs passing, QEMU exit 0, 10.529 s. Basic and Probe results under
`scan-exit-basic-probe/{basic,probe-auto}/result.json` both passed.
The normal desktop and hardware recovery have not been exercised.

Additional defects isolated and fixed during this checkpoint:

- RISC-V glibc `pause()` issues syscall 73 (`ppoll`), confirmed by disassembly.
  Fixing only the architecture's `pause` implementation did not repair it.
- Raw ppoll lacked remaining-time copyback and non-handler restart; its
  initial regression passed only 2/5. Read-only timeout memory preserves a
  successful return and prevents restart after an interrupted failed copyback.
- A jiffies deadline could expire early: one 100 ms call returned at
  99.7385 ms. Poll syscalls now choose an explicit monotonic timer; expiry
  tests require both at least 100 ms elapsed and correct timeout copyback.
  The default constructor for unrelated Poller consumers is unchanged.
- Saved masks tied to an internal restart errno were restored during STOP
  when timeout copyback failed. The original mask now remains in ThreadLocal
  until a handler consumes it or the final userspace return restores it.
- Keeping the mask alone was insufficient: the queue prioritized default
  SIGCONT before USR1, and the one-signal scan returned too soon. Delivery now
  continues over default CONT and suppressed/ignored signals, stopping after
  preparing one user handler or requesting a stop checkpoint. Fatal delivery
  explicitly returns after exit cleanup instead of scanning cleared VM state.

The read-only temporary-mask regression initially observed zero handler calls
despite USR1 being queued while stopped. The final candidate observes exactly
one call and restores the original blocked mask. This is retained as a real
regression rather than inferred from the code review.

Native Linux also passed the separate x86-64 tracer-cancelled sigsuspend oracle:
the tracer replaces RAX -514 with zero and suppresses the signal, yet the old
mask must be restored. This test is registered for x86-64; it has not been run
on an Asterinas x86-64 kernel. Its non-x86 stub reports SKIP/77, not success.

`check-x86_64-scan-exit.log` completes successfully with 12 existing warnings.
`clippy-riscv-scan-exit.log` completes successfully with 20 warnings; this was
not a warning-free `make check`. The reported warning sites predate this patch.
LoongArch checking fails on three unchanged references to cfg-disabled `vdso`
in nsproxy, system-wide clocks and system_time. It is not reported as passing.
Boot-tool host tests passed 118 cases in `boot-host-tests-fixed-path.log`;
the initial invocation lacked `PYTHONPATH=tools/riscv:.` and is retained as a
failed invocation, not a product defect.

No local commit, remote push, board reboot, board filesystem write, or replacement
of the installed default kernel/menu has been performed at this checkpoint.

## 2026-09-13 futex restart checkpoint (not full group-stop completion)

The next bounded syscall integration step is implemented and reviewed. Linux
v6.12 `kernel/futex/{syscalls,waitwake}.c` is the reference: timed futex waits
use a restart block retaining the absolute host-clock deadline and arguments;
untimed waits retain `ERESTARTSYS`. A caught handler interrupts timed waits even
with SA_RESTART. Absolute MONOTONIC futex timestamps are converted from the
calling time namespace only at initial entry, never again during restart.

`FutexRestart` retains the address, expected value, matching bitset, private/shared
visibility, clock choice, and deadline in current-thread-only state. The existing
bucket-locked word check/enqueue and wake/cancellation decision are unchanged.
A successful futex dequeue/wake is not converted into restart work. The now-unused
plain-wait wrapper was removed after a build exposed its new dead-code warning.

Actual red/green evidence:

- `futex-red-20260913a`: the frozen `scan-exit-kernel.Image` passes 4/11 cases.
  Relative waits incorrectly return success when woken 100 ms after CONT instead
  of retaining their expired deadline. Timed caught-handler cases incorrectly
  return ETIMEDOUT after 1.84--2 seconds instead of EINTR. WAIT with the unsupported
  realtime flag and null timeout returns EAGAIN instead of ENOSYS.
- `timens-futex-red`: MONOTONIC +60 s is verified; relative futex waits take
  50 ms, but the 50 ms absolute futex wait exceeds the parent's 2 s bound.
- Native Linux passes all 11 futex cases and all 8 time-namespace cases. The
  latter include a live 400 ms absolute futex deadline across confirmed STOP,
  a 100 ms stopped interval, and CONT. Sources compile with strict warnings
  using the existing cached toolchain; no downloads were needed.
- `futex-clean-green`: four-hart QEMU passes 56 checks across nine programs,
  with exit 0 in 14.282 s. The futex suite is 11/11 and namespace suite 8/8.
  The resumed namespaced wait expires at its original 400 ms deadline.
- `futex-compat-green`: all 11 preexisting pthread/signal/job-control/ptrace/rseq
  programs return 0 in 16.737 s. This includes the signal/mutex regression and
  4096 condition-variable handoffs. This is not Firefox performance evidence.
- `futex-basic-probe`: Basic and Probe-auto both exit successfully using the
  unchanged Stage1 archive (0.417 s / 0.338 s in QEMU, not board boot times).

Frozen final candidate: `target/signal-group-stop/futex-clean-kernel.Image`,
SHA-256 `2dba62bef44c79fa2b09807833047d138ff6d1c717ec5befdf1b9f3da1a78330`.
The 56-check archive SHA-256 is
`0877d40e745b9303c5377be2a60433ad5d5783138ec40be5b966167e879d1001`;
the compatibility archive SHA-256 is
`fe6a18aed20c64f263c8496c4b4f7bea2fb0d54e99ffecaec04c86b98971dd2d`.
Each directory retains serial output and a machine-readable result record.
The earlier `futex-restart-kernel.Image` and its 55-check result are retained,
not overwritten or reported as the final source build.

The new independent `FutexRestart.tla` model covers wake-versus-cancellation,
spurious raw pause success normalized to interruption, and saved deadlines.
The main agent reran the full runner into
`target/signal-job-control-model/run.6kYKV1Mt/`: all 12 negative controls fail
the intended property, and all four corrected finite models exhaust their
graphs. The futex model explores 119,928 generated / 32,415 distinct states,
depth 29, with zero queued states. Its three negative controls catch deadline
reset, lost successful wake, and invented success from a spurious wake.
The model assumes atomic bucket operations and fixed correctly translated
arguments; it is not a Rust refinement, hardware-memory or liveness proof.

Ordinary reviews found no remaining blocking issue in this scoped change.
They caused two runtime-test improvements (a 500 ms post-CONT upper bound and
the live namespaced restart case) and the explicit spurious-pause model branch.
The 50 ms userspace-readiness-to-syscall-entry allowance is still a scheduling
assumption, not proof of enrollment. Severe scheduling delays can fail a test.
`futex-check-x86_64.log` succeeds with 12 existing warnings;
`futex-clippy-riscv.log` succeeds with 20 existing warnings. Scoped formatting,
shell syntax, and `git diff --check` succeed. LoongArch's previously recorded
unrelated vdso errors have not been repaired or reclassified as passing.

Still pending: poll/select/pselect6 restart integration, full ptrace group-stop
semantics, disposition flag retention, lifecycle integration coverage, and the
required physical qualification. No commit, push, board reboot/write, or default
artifact replacement occurred during this futex checkpoint.

## 2026-09-13 traditional ptrace contract and completed-stop latch checkpoint

This is a foundation change, not completed ptrace integration or physical
qualification. Linux v6.12 `kernel/signal.c` (`do_signal_stop`,
`task_participate_group_stop`, `ptrace_stop`, and `prepare_signal`) separates a
completed group-stop latch from outstanding participation in a later round.
A tracer may resume a member without SIGCONT; a subsequent stop preserves the
original completed group's signal and does not report another completion to
the group parent. SIGCONT during that later round reports continuation.

`GroupStopPhase::Stopping` now retains `completed_signum`. Enrollment skips
already acknowledged/parked siblings. Final acknowledgment preserves the
original signal and suppresses a duplicate group-parent report. The new exact
kernel test `repeated_stop_preserves_completed_notification` first printed
`accepted=false` against the old coordinator and failed its assertion (the
ktest wrapper eventually timed out; that timeout is not a passing test).
After the change it prints `accepted=true` and passes, including a parked
sibling, preservation of SIGSTOP after a SIGTSTP round, and SIGCONT during a
third round. It manually makes one member inactive to isolate the coordinator;
this is not evidence that a real tracer can yet resume a group-stopped thread.

Fresh exact-name kernel runs each report one passed test and zero failures:
`repeated_stop_preserves_completed_notification`, `group_stop_participation`,
and `group_stop_cancel_and_rejoin`. Logs use the
`target/signal-group-stop/ptrace-latch-final-*-ktest.log` prefix.

The new bounded `group_stop_ptrace.c` oracle passes all four cases on native
Linux: delivery-stop versus group-stop GETSIGINFO, tracer CONT without SIGCONT,
SIGCONT retaining a delivery-only ptrace stop, detach retaining group stop,
and repeated delivery/group-stop pairs without an intervening SIGCONT.
The four cases group these related assertions. On the frozen new candidate,
only the delivery-only SIGCONT case passes; the other three time out waiting
for the first group-stop report. Guest cleanup and QEMU shutdown succeed,
but the regression returns 1. The test is retained separately and is not yet
registered in the passing regression runner. Its repeated-round assertions
cannot execute until the missing first group-stop hook is implemented.

Frozen release image: `target/signal-group-stop/ptrace-latch-kernel.Image`,
SHA-256 `511d5503389490efa7be21f2146b60337c8d897ec5863bd6dd96aec1ff864c65`.
The cached, offline release build succeeds with 12 warnings. Actual runtime
artifacts under `target/signal-group-stop/` are:

- `ptrace-latch-regressions`: all 56 checks across nine programs pass;
  QEMU exits 0 in 14.054 s. Archive SHA-256
  `960be3c64f1734400e2fb5ec7b3b81ffd7e172e8c457c2728ade67b44a437a2f`.
- `ptrace-latch-compat`: all 11 preexisting compatibility programs return 0;
  QEMU exits 0 in 16.876 s. Archive SHA-256
  `fe6a18aed20c64f263c8496c4b4f7bea2fb0d54e99ffecaec04c86b98971dd2d`.
- `ptrace-latch-basic-probe`: both modes pass using unchanged Stage1,
  in 0.500 s and 0.527 s. These are QEMU results, not board boot measurements.
- `ptrace-latch-known-red`: the new ptrace oracle returns 1 in 15.602 s;
  QEMU itself exits 0. Archive SHA-256
  `fa3cf05a48f3034db1eaca8fb3a0e8a7ce66a07e929436db667701c7a0863205`.

The independent `PtraceGroupStop.tla` models the intended next integration,
not the current Rust implementation. It covers counted participation,
parked versus tracer-controlled members, delivery and group traps, delayed
old trap returns, SIGCONT, detach/tracer exit, and member/group exit. An
ordinary review caught an omitted interleaving: SIGCONT with no active group
stop during an initial delivery-only ptrace stop. Removing that guard makes
the corresponding negative trace directly exercise the native C scenario.
The corrected model has two threads, at most two stop initiations and one
attachment per thread; there is no fairness, weak-memory, Rust refinement,
or full-system liveness proof. Different stop signal numbers are abstracted
to one kind; preservation of the original number is a separate kernel test.

The main agent independently reran the corrected full runner into
`target/signal-job-control-model/run.O1iVZ8rf/`: all 15 negative controls fail
their intended property, and all five corrected finite models exhaust their
graphs. The new ptrace model explores 13,025 generated / 1,735 distinct states,
depth 14, with zero queued states. Scoped Rust/C formatting checks, shell syntax
and `git diff --check` also pass. Specification and ordinary quality review
found no remaining defect in the bounded completed-stop latch change; the
model coverage finding above was corrected before this final independent run.

### Next coupled integration: do not substitute a notification-only patch

- Separate `Pending { counted, signum }`, parked and tracer-controlled thread
  states. A pending obligation restored by attach/detach may be uncounted
  while a sibling still owns a counted obligation.
- Separate ptrace wait report/stop kind from an optional real delivery signal.
  A traditional group trap has no GETSIGINFO payload and does not inject a
  signal when resumed. Existing delivery/event/syscall stops retain their ABI.
- At a group checkpoint, acquire tracee state before the signal coordinator
  and participant state; consume the current obligation and publish the
  ptrace stop within that protected transition. Perform callbacks/wakeups
  only after releasing locks. Never query `is_traced()` under the coordinator:
  it acquires tracee state and would invert this order.
- Tracer CONT releases ptrace state without rewriting group participation on
  the old stop call's return. SIGCONT plus a new STOP can have installed a
  new pending obligation while that call was suspended.
- Attach to a parked member and explicit detach/tracer exit must serialize
  with the coordinator. Preserve existing counted Pending; restore uncounted
  Pending when tracing ends while a group stop remains active.
- Replace process-wide stop predicates with thread-effective predicates in
  user admission, signal dequeue, and interruptible-sleep checks. An explicitly
  tracer-resumed member must run and block normally while the completed group
  latch remains set. Other members must remain parked.
- Keep real-parent group reports separate from per-thread tracer reports,
  avoid duplicates when tracer and parent coincide, and test mixed traced
  siblings/lifecycle races. SEIZE/LISTEN remain unsupported and require a
  separate compatible extension; this traditional model does not cover them.

No commit, push, board reboot/write, or installed default replacement occurred
at this checkpoint. Full ptrace integration, the remaining syscall/lifecycle
gaps and the required board qualification remain open.

## 2026-09-13 traditional ptrace runtime integration checkpoint

The next coupled implementation now connects the coordinator to actual ptrace
stops, user admission, signal dequeue and interruptible waits. This supersedes
the preceding checkpoint's statement that only the latch is implemented.
It does not complete all ptrace/group-stop semantics or physical qualification.

`Pending { counted, signum }` distinguishes counted participation from the
uncounted obligation restored on attach/detach. `PtraceControlled` records
transfer to the tracer; `Acknowledged` still means an ordinary parked member.
The current checkpoint holds tracee state, then the process signal coordinator,
then participation across acknowledgment and ptrace-stop publication. Initial
checkpoints initialize tracee state as well, avoiding a race between the first
attach and a snapshot of an uninitialized `Once`. No returning ptrace-stop call
writes participation. Tracer continuation can therefore permit progress while
the process latch remains stopped without discarding a newer pending STOP.

Traditional group traps have a wait report but no signal-delivery payload:
GETSIGINFO returns EINVAL and a resume signal is not injected from that trap.
Attach to a parked member schedules an uncounted checkpoint. Detach and tracer
exit restore group parking but leave an existing counted Pending untouched.
The per-thread predicate is used in syscall waits and user-event polling as
well as the final task loop. The normal non-stopped fast path avoids taking
the coordinator mutex in that predicate.

Code/specification review exposed and corrected additional edges:

- A tracer-resumed member can start a new round before its sibling acknowledges
  the first round. Rejecting every Stopping phase lost that STOP. The exact
  `traced_member_restarts_incomplete_group_stop` ktest first prints
  `accepted=false`, then fails; the corrected test prints `accepted=true`
  and completes. It also exercises an uncounted detached member alongside a
  counted sibling, ensuring only that sibling completes the remaining count.
- The initiating member must not already be Pending/Acknowledged. A delivery
  ptrace stop can overlap a sibling's initiation: its injected STOP must not
  replace that existing obligation. This guard and selection cancellation are
  checked under task membership and the coordinator, matching the model's
  Inactive/PtraceControlled initiation guard. A dedicated mixed-thread runtime
  oracle for this precise interleaving remains to be added.
- Pending SIGKILL is checked under the coordinator before acknowledgment and
  publication. The code review verifies the ordering; no claim is made that
  the current C tests deterministically force that narrow race.
- A final traced acknowledgment claims its coalesced completion notification
  under the coordinator. Cross-process callbacks then omit a duplicate group
  report if the tracer process is the real parent. Tracer SIGCHLD uses the
  shared process-directed notifier with existing SIG_IGN/SA_NOCLDSTOP checks.
  Wait status retains the participant's current signal, while group SIGCHLD
  retains the original completed group signal. Default-action flag retention
  and non-leader ptrace TID namespace translation remain known limitations.

The C oracle now has seven cases and is registered in the normal process
regression runner. All seven pass on native Linux with strict compiler warnings.
The two lifecycle cases passed twenty consecutive native runs before adding
the seventh notification case. Fresh main-agent native execution also passes.
Frozen old `ptrace-latch-kernel.Image` passes only one of the six lifecycle cases
(`ptrace-lifecycle-red`); the first integrated candidate passes all six
(`ptrace-lifecycle-first`). The seventh case deliberately holds the tracee in
its group trap while checking blocked SIGCHLD: Linux reports one event, whereas
the first integrated image reports a second CLD_STOPPED
(`ptrace-notify-red`). The shared notifier/atomic claim fixes that real failure.

Latest frozen release: `target/signal-group-stop/ptrace-guard-kernel.Image`,
SHA-256 `0f6f19843a37587a49809ed5d2dd857468e16e4dfcaa599550fc3d0c590e424e`.
Fresh artifacts under `target/signal-group-stop/`:

- `ptrace-guard-green`: 63 checks across ten programs pass, QEMU exit 0,
  14.806 s. Derived archive SHA-256
  `1d1302f6f5339617ebb4ac14a4e789e588eb9c695bb57c87d5ebe3f1d3617241`.
- `ptrace-guard-compat`: all eleven preexisting programs return 0, QEMU exit 0,
  16.801 s. Archive SHA-256
  `fe6a18aed20c64f263c8496c4b4f7bea2fb0d54e99ffecaec04c86b98971dd2d`.
- `ptrace-guard-repeat-{1,2,3,4,5}`: five additional independent four-hart QEMU
  boots each pass all seven ptrace cases and exit 0, 1.158--1.231 s per run.
- `ptrace-guard-basic-probe`: both modes pass unchanged Stage1, 0.501/0.334 s.
  These remain QEMU measurements, not board or Firefox performance results.
- `ptrace-integrate-*-ktest.log`: incomplete-round restart, completed latch,
  group participation and cancellation/rejoin each execute one test and pass.
  The completed-latch test now uses actual traced-acknowledgment transitions,
  replacing the preceding checkpoint's manual inactive-marker simulation.
- `ptrace-integrate-check-x86_64.log` succeeds with twelve warnings;
  `ptrace-integrate-clippy-riscv.log` succeeds with twenty warnings. These are
  the preexisting warning totals, not a warning-free full `make check`.
  An intermediate build failed for a missing trait import; that invocation
  remains retained and is not counted as a passing build.

The full finite-model runner independently passes again in
`target/signal-job-control-model/run.6w7srjHL/`: fifteen intended negative
controls and five corrected models. The ptrace graph remains 13,025 generated /
1,735 distinct states, depth fourteen and zero queued. Its omission of queued
SIGKILL, parent/tracer notification ordering, signal numbers and kernel locking
is still explicit: the model is a protocol check, not a Rust refinement.
Specification and subsequent ordinary quality review found no remaining new
defect in this bounded integration. Scoped Rust/C formatting, shell syntax and
`git diff --check` pass.

Still required before full-goal acceptance: real-parent wait events while a
separate tracer owns a child (the current child-wait path skips every traced
main thread), modern SEIZE/LISTEN behavior, mixed-thread/lifecycle race oracles,
remaining poll/select/pselect6 restart and disposition work, full candidate
review/commit, and the planned physical qualification. No commit, push,
board reboot/write, or installed kernel/menu replacement occurred this turn.

## Parent/tracer wait-consumption checkpoint (not full-goal completion)

The next software slice fixes three independently observed wait-report issues.
The reference is Linux v6.12
[`wait_consider_task` / `wait_task_continued`](https://github.com/torvalds/linux/blob/v6.12/kernel/exit.c):
different real-parent and tracer STOP reports are independent, continuation is
one process-wide consumable state, and a tracer's result identifies the traced
task rather than necessarily the group leader.

`try_wait_children` no longer skips every live traced main thread. Its new
`wait_child_group_status` helper serializes first attachment, same-parent
eligibility and consumption under tracee state before the group wait lock.
Same-parent tracing suppresses the additional group STOP; zombie reaping remains
hidden until the tracer consumes exit. `try_wait_tracees` can consume only the
shared CONT slot, not the real parent's STOP. `TraceeContinue` preserves the
traced thread's identity in both waitid and wait4.

The registered `group_stop_ptrace_parent.c` regression uses a supervisor owning
both child and sibling tracer, explicit pipe handshakes, bounded waits and
tracer-before-child cleanup. Four cases cover both STOP-consumption orders,
repeated non-consuming WNOWAIT, shared CONT, tracer-before-parent exit reaping,
and nonleader TID identity in both waitid and waitpid. The Makefile supplies
`-pthread` for the last case. Native Linux and four-hart QEMU pass all four.

Retained red/green evidence under `target/signal-group-stop/`:

- `ptrace-parent-red`: both first cases fail at `parent-group-peek` on the
  previous `0f6f1984...` frozen image. `ptrace-parent-first-green` passes the
  same archive on the first parent-wait correction (`8e74815d...`).
- `ptrace-parent-cont-red`: a third native-passing case fails at
  `tracer-continue-peek`; `ptrace-parent-cont-green` passes after adding the
  shared CONT path (`04779776...`).
- `ptrace-parent-tid-red`: a fourth native-passing case observes PID 13 where
  TID 14 is required. `ptrace-parent-tid-green` passes the identical archive
  after introducing the thread-sourced continuation result. Both runs exit
  QEMU normally; regression failure, not a timeout, supplies the red result.
  Shared archive SHA-256:
  `e41157ad5e6cbf42df87b44c8fba72d18448b14cb406bdb85979dcdee24efafb`.

Latest frozen release: `target/signal-group-stop/ptrace-parent-tid-kernel.Image`,
SHA-256 `490239ec597697a1e3b2b75c6bab23c719cf0395394d00f555e7da916aa0d2ae`.
Fresh verification of this exact image:

- `ptrace-parent-all-green`: 67 checks across eleven signal programs pass,
  QEMU exit 0, 14.996 s. Archive SHA-256
  `9ceb450bbffb5eaa353f56523a8fd2080f9e87599e3c4ef917fb34f10dc3ed41`.
- `ptrace-parent-compat`: eleven preexisting programs pass, QEMU exit 0,
  16.502 s. Archive SHA-256
  `fe6a18aed20c64f263c8496c4b4f7bea2fb0d54e99ffecaec04c86b98971dd2d`.
- `ptrace-parent-repeat-{1,2,3,4,5}`: five further independent four-hart boots
  each pass all four new cases, 0.613--0.970 s, QEMU exit 0.
- `ptrace-parent-basic-probe`: unchanged Stage1 passes both basic/probe-auto,
  0.420/0.338 s. These are QEMU timings, not physical or Firefox measurements.
- `ptrace-parent-check-x86_64.log` and `ptrace-parent-clippy-riscv.log` succeed
  with the existing twelve/twenty warnings; no warning-free full lint claim.

The new `WaitReports.tla` checks independent STOP slots, non-consuming peeks,
and atomic shared CONT consumption. All eighteen expected negative controls
and six corrected models pass in `target/signal-job-control-model/run.bIL8pSSG/`.
Its corrected graph is 38 generated / 17 distinct states, zero queued. Three
deliberate faults each produce the named invariant violation. This is a finite
operation-contract safety check, not a Rust refinement or a liveness proof;
see the model README for exact exclusions and implementation mapping.

Ordinary independent review identified the nonleader identity issue above;
after correction it found no remaining new blocking defect in this slice.
An initially suspected missing wake for an independent tracer's blocking
WCONTINUED was **not** treated as a compatibility bug without a native oracle.
On native Linux 6.5.0-15-generic, the reviewer repeated the diagnostic five times:
`/proc/<tracer>/syscall` confirms waitid entry before CONT, the parent's WNOWAIT
confirms publication, but no return occurs within 500 ms; an extra SIGUSR1 lets
waitid retrieve CLD_CONTINUED. The main agent rebuilt and repeated this result in
`ptrace-blocking-cont-native.log`. This finite observation does not prove that
the waiter would never wake. Linux v6.12 source also explicitly discourages
tracers from using WCONTINUED; deferred notification is distinct from the wait
slot's availability. No speculative wake-path change was made.

Still required: blocking real-parent wait and mixed-thread/lifecycle race
oracles, same-parent/attach/exit concurrency beyond this model, modern
SEIZE/LISTEN, remaining restart/disposition work, full candidate review and
physical qualification. The new queries use bounded WNOHANG polling and do not
establish wait-queue liveness. Existing namespace limitations and WEXITED/zombie
option handling are not fixed by this slice. No commit, push, board reboot,
board write or installed kernel/menu replacement occurred in this checkpoint.

## Lifecycle and blocking-wait checkpoint

Added and registered `group_stop_lifecycle.c` with four bounded native/QEMU
oracles, without changing the final production implementation:

- **Pending member exit:** a nonleader is held at PTRACE_EVENT_EXIT. STOP then
  parks the leader (confirmed through per-thread `/proc` state T), but no group
  completion may be reported while the exiting member remains pending. Letting
  that member finish exit must complete the group's wait report exactly once.
- **Join while stopped:** after completed group stop, PTRACE_CONT releases only
  the traced creator. It clones an untraced member without TRACECLONE or
  CLONE_PTRACE. The new member must park, its entry marker remain zero, and no
  duplicate group report appear; SIGCONT must then let its marker become one.
  The marker proves that the thread-body store has not executed, not that no
  clone trampoline/prologue instruction has executed. First-user-entry gating
  also requires the implementation checkpoint inspection.
- **Blocking STOP and CONT waits:** a separate controller waits for the parent's
  release store, then observes its actual sleeping state before generating the
  requested event. The parent performs only blocking waitid after that store.
  The controller remains alive until waitid returns, preventing an unrelated
  sibling exit from supplying a false successful wake. A non-restarting alarm
  bounds the wait and any alarm-assisted return is rejected.

Native Linux passes all four, including five further complete repeats. Four-hart
QEMU passes all four on the previous frozen candidate and again on the restored
rebuild; five additional boots (`lifecycle-repeat-{1,2,3,4,5}`) each pass, with
0.606--0.762 s elapsed and normal QEMU exit. Independent ordinary review found
no blocking test defect, and confirmed the explicit marker limitation above.

To establish actual runtime sensitivity, one temporary negative build omitted
only `process.enroll_group_stop(child_posix_thread)` from clone publication.
`lifecycle-skip-enroll-red` correctly fails `joined-parked`, while the exit case
still passes; regression status is 1 and QEMU exits 0 in 3.752 s. The two-case
archive SHA-256 is
`511b08beb1df64f32148dcbe77fe6ca8db573a6b822c94b195e766b638a5f7cf`, identical
to the successful `lifecycle-first` run. The deliberately broken kernel is
retained only as `lifecycle-skip-enroll-kernel.Image`, SHA-256
`e8a62ec67a7a6a5081d6b404ba60996fa1cde6d200963f5dd9e2edc1b17fd539`.
It must never be staged to the board.

The temporary edit was immediately restored; `clone.rs` has exactly its
pre-experiment SHA-256
`67498f3c1235e1810dfedc180bfc8d468196a9882d6c2f9f9b5634d3af413658`.
The fresh offline release build, `lifecycle-restored-kernel.Image`, is byte-for-
byte identical to the preceding candidate:
`490239ec597697a1e3b2b75c6bab23c719cf0395394d00f555e7da916aa0d2ae`.
The complete `lifecycle-all-green` run passes 71 checks across twelve signal
programs, QEMU exit 0, 15.305 s. Archive SHA-256:
`5b8e6e4efbac76a16033ce6f9bd8d28fd04febf096f9bfc26fc0c2649f830c71`.
The four-case standalone archive is
`3378c1db162555b6b795feb463dcb1557dac5618fe1ce961f74c032ff0fba25f`.

All six finite models and eighteen intended negative controls pass freshly in
`target/signal-job-control-model/run.g9NbzxbR/`. Existing GroupStop join/leave
contracts and WaitWake checks complement these runtime experiments; no new
Rust refinement, unbounded liveness or weak-memory proof is claimed.

Read-only board revalidation now succeeds via known-host SSH to RockOS:
Linux 6.6.87 riscv64, root `/dev/mmcblk1p3`, no partition-2 mount, and all three
previously recorded installed kernel/menu hashes unchanged. ttyS0 has a bash
session; the host serial device exists and has no observed owner. A passive
three-second serial read was empty, which says nothing about a hang or reset.
Evidence: `lifecycle-board-{ssh-check,console-check,passive}.log`. No command was
sent through serial, no reboot, disk write, fsck or artifact staging occurred.
This is readiness evidence, not Asterinas physical qualification.

Remaining gates are unchanged except for these newly covered lifecycle and
blocking paths: interrupted/mixed stop rounds, other clone/exec/exit races,
modern ptrace, remaining restart/disposition contracts, full reviewed commit
and actual lightweight plus normal-desktop board qualification. Reuse the
existing optional menu and RockOS network staging; do not publish a new default
or treat native RockOS/QEMU success as Asterinas board success.

## Physical qualification checkpoint: recovery is not yet consistently fast

The physical collection gates above have now been exercised, but the observed
slow shutdown remains unresolved. These checkmarks do not complete the overall
goal, approve the full change set, or promote the candidate to the default.

Reused the existing RockOS network staging and optional `sysboot` menu. Candidate
manifest: `target/signal-group-stop/physical-candidate/manifest.json`; menu
SHA-256 `f5b6975c888d56b455cfec3d528d2e48455a0ed4746d7f81ca052a8d0a44b3ca`.
Only content-addressed candidate artifacts were staged. The kernel is the
restored `490239ec597697a1e3b2b75c6bab23c719cf0395394d00f555e7da916aa0d2ae`
image, never the deliberately broken clone-enrollment image. Probe mode uses
the same `5b8e6e4efbac76a16033ce6f9bd8d28fd04febf096f9bfc26fc0c2649f830c71`
archive as the complete QEMU suite. Desktop mode retains the normal Stage1
`9ef253dadad3f399e6152ad8681319725e99562081f237ef851a7ec11c3ba405`, prepared
DTB and desktop arguments. No default menu or boot environment was changed.

The actual Asterinas board probe passes **71 checks across twelve programs**,
with every `REGRESSION` status zero and fresh firmware recovery in 38.716 s
from menu selection. It does not mount the Debian partition. Evidence:
`physical-probe.serial.log` and `physical-probe-run.log` under
`target/signal-group-stop/`; serial SHA-256
`49dba13ddbd4dbace5322aa86bf0c64bb3e97be60cff58c27b0364f879fbe9a0`.

Three normal tty0 desktop cycles all reach a visible Firefox window and pass
local-file navigation, JavaScript readiness, a scripted button click and live
framebuffer capture. Neither Internet browsing nor physical input is claimed.
Window times are polling observations, not precise first-render timestamps.

| Cycle | Menu to root console | Menu to observed window | Content check | Sync acknowledgement | Reboot command to firmware prompt |
| --- | --- | --- | --- | --- | --- |
| 1 | 11.378 s | 72.720 s | 7.285 s | 0.671 s | **18.932 s** |
| 2 | 9.790 s | 68.154 s | 7.055 s | 1.103 s | **107.407 s** |
| 3 | 10.075 s | 61.126 s | 7.040 s | 0.704 s | **106.931 s** |

Each recovery sends `sync` and a single `systemctl --force reboot`; it does
not bypass PID 1 with a double-force reset. The default 90 s stop timeout,
`console=tty0`, `loglevel=off` and `asterinas.klog_capture=info` are unchanged.
The approximately 90 s difference is consistent with a timeout but does not
identify its owner or prove that signal-group-stop code causes it. The first
cycle started from RockOS and had a longer pre-shutdown dwell; the following
two started directly after Asterinas warm recovery. These uncontrolled
differences must not be converted into causal claims.

Evidence: `desktop-{1,2,3}/result.json`, corresponding content serial logs and
`desktop-{1,2,3}-recovery.serial.log.json`. `desktop-2-config.serial.log`
records actual command-line options and the unchanged stop timeout;
`desktop-3-state.serial.log` records no pending systemd jobs at observation
time, along with process states. The live, decoded `desktop-1.png` is 41,508
bytes, SHA-256
`8ee06cdcc3e088ba707c87589b2ca9eccc963a4ae2b53533d014b15cdb44fb74`, and was
visually inspected: Firefox displays the local page and `INTERACTION PASS`.

A fourth diagnostic cycle retains the same kernel/desktop settings, then
enables warning-level console output using `dmesg -n 5` before shutdown.
Content passes again; recovery takes 34.443 s, with 0.703 s sync acknowledgement.
This is **not** a fourth normal performance sample: synchronous UART logging
changes timing, and the slow interval did not recur. The serial log contains
313 `A_SIGKILL_PROVENANCE` records, mostly normal exit-group sibling cleanup.
It also records runuser sending SIGKILL to openbox. The generic
systemd-shutdown "Sending SIGKILL" phase message alone is not proof of an
actual PID 1 signal to a surviving process; none was identified in this trace.
Evidence: `desktop-diag-recovery.serial.log`, SHA-256
`f3b959965f14d7d42b2c94d967c64b3faa8ce954a0123d2e7131ab3d0387ff9f`.

Many provenance messages lose their trailing target fields on the serial
console. Inspection finds a relevant bounded-loss mechanism: DW APB UART
diagnostic sends share a finite poll budget across the buffer, and Busy or
TimedOut is deliberately treated as dropped diagnostic output. The in-memory
klog message capacity is 1024 bytes, not the approximately 160 visible bytes
in some serial records. This identifies a possible truncation mechanism, not
a measured per-record cause. Preserve the UART budget; do not reintroduce
unbounded polling under disabled interrupts. Before another slow-path
experiment, put compact numeric source/target fields first and reduce routine
sibling-cleanup console traffic, with tests for diagnostic behavior. This
logging change has not yet been implemented. No persistent Debian journal was
available on partition 2 to reconstruct the preceding slow runs.

Finished in verified RockOS Linux 6.6.87, boot ID
`4cdea5e8-e425-4876-8ac0-1a634fa0ce8b`, root on partition 3. Partition 2 is
unmounted and read-only `e2fsck -fn` exits 0. Installed defaults retain hashes:

- Kernel: `485b9079c204bf6b34055f5e1061f3011381557d8cc4b4bf2d1e4831922058c1`.
- Asterinas configuration: `02280720efe7a1ad0ac084cdc20429406631e12d2e16f05638544bab0883fb26`.
- Default extlinux configuration: `eb5f39a6e2db71ccc93ae005c488fd9f7ef353e75426e5a51e8923a9cbf2ebc5`.

Final evidence is `physical-final-rockos-check.log`. No filesystem repair,
default promotion, commit or push occurred. The six finite models and eighteen
negative controls retain the preceding software checkpoint's results; they
were not rerun for these unchanged-kernel physical experiments. Remaining
work includes the unresolved slow recovery, the signal/ptrace/restart contracts
listed above, and full scoped review before publication.

## Compact provenance and a separately reproduced SIGCHLD wait race

Reworked only the temporary SIGKILL diagnostics first. Numeric sender/target
PID/TID pairs precede descriptive fields; thread-name allocation and locking
are removed. User and unusual kernel enqueues remain warnings. Routine
exit/exec sibling fanout is debug-only; delivery remains in info-level capture
without printing on a warning-level console. No rate-limit state, signal queue
change or UART-budget relaxation was added. Existing process-main-thread
lookup locking remains; this is not a claim that all logging paths are lock-free.

Added `syslog/provenance.c`, registered in the process regression runner. It
uses real `kill`, `tgkill`, multi-threaded `_exit` and `/dev/kmsg`, checking
termination status, identities, field order, length and severity. It skips
without the explicit info-capture boot option. The targeted qualification
requires three explicit PASS markers, never treating the skip as coverage.
Independent ordinary review caught a scan-budget false-pass possibility,
unrelated-process interference and a debug-console false failure. The final
test requires reaching EAGAIN, filters target identity before counting and
accepts routine records only at debug priority if capture includes them.

The initial unchanged test archive
`476a4469a9cf26962dae2a351b9b2c66dec4d034abd1c41fe98ce807877e68c2`
fails all three contracts on the `490239ec...` kernel and passes on the compact
logging kernel `40e268398e92581be78308e8dd1aaad43c56e0375492a1a2080c009b77f80d57`.
The final reviewed test was also rerun against both kernels. Full QEMU evidence
`provenance-filtered-all-green` passes all thirteen programs in 15.472 s,
QEMU exit 0; archive SHA-256
`954284fc632dddfafe1753fe46d8f99d6120237aa017b3f71f6c54abccc75326`.

The same compact-logging kernel and final full archive pass all **74 checks
on the Asterinas board**, including the three provenance cases and preceding
71 signal checks. Recovery takes 38.108 s from probe selection. Existing
network staging and optional menu were reused. The first host preparation
attempt failed before serial attachment because the QEMU artifact directory
is container-owned; preparing the candidate under a host-owned sibling
directory resolved that local path issue. No board retry/reset was based on
an observation timeout.

Candidate manifest: `provenance-physical-candidate/manifest.json`; menu SHA-256
`1d71e26e5303a52bb37981194bb24378bd796bb64433b15a60a44a6ad03a8ea0`.
Physical logs: `provenance-board-probe-run.log` and
`provenance-physical-probe.serial.log`, all under `target/signal-group-stop/`.
Desktop retains the normal Stage1 and boot arguments, reaches a root console
at 10.967 s and an observed Firefox window at 72.550 s, then passes the local
page/JavaScript/framebuffer test in 6.957 s. Warning console output is enabled
only before shutdown; this is a diagnostic run, not a normal timing sample.

This time the long shutdown is captured with complete identities:

- At kernel uptime 216.897, systemd-shutdown reports waiting for PID 122
  (`openbox`) and PID 116 (`runuser`).
- At 297.496 and 297.866, actual provenance records show **PID 1** enqueuing
  SIGKILL to 116/116 and 122/122, respectively.
- Sync acknowledgement is 0.688 s; command-to-firmware recovery is 111.389 s.

Thus an actual escalation, not merely a phase message, is established. It
does not establish those processes' states at the deadline. A zombie may
remain visible while its parent is stopped or has not reaped it. Do not infer
that openbox was executing or that ext2 caused the wait. Inspect the
`runuser` wait/stop path next, using a native oracle: util-linux v2.38.1
[`wait_for_child`](https://github.com/util-linux/util-linux/blob/v2.38.1/login-utils/su-common.c)
uses `waitpid(WUNTRACED)` and self-stops upon child STOP; it does not directly
call `sigtimedwait`. Match the installed version before attributing that path.
Systemd v252's
[`broadcast_signal`](https://github.com/systemd/systemd/blob/v252/src/shared/killall.c)
uses STOP/broadcast/CONT followed by waitpid and sigtimedwait. These references
guide experiments; they are not proof of which call was blocked on this board.

During that physical run, inspection found a separate concrete race in
`rt_sigtimedwait`: it temporarily unblocks requested signals, although its
explicit dequeue remembers the original mask. Between condition attempts,
`PauseReason::Sleep` calls `has_pending`, whose ignored-unblocked probe can
silently discard SIGCHLD. A remembered wake cannot restore the lost payload.

An ignored prototype with a 100 ms timeout passes 10,000 iterations on native
Linux; the compact-logging kernel fails at iterations 9 and 4 in independent
QEMU boots, EAGAIN with pending empty. Its timestamp is before the send call,
so it must not be represented as enqueue-completion timing. The registered
`signal/sigtimedwait_race.c` uses a 1 s timeout and a 15 s process watchdog.
After review, its timestamp was moved **after** successful `kill` completion.
The final `sigwait-completion-red` reports iteration 4, successful completion
at +16,200 ns, wait return at +1,000,149,700 ns, EAGAIN, pending empty.

The minimal fix retains the original thread mask throughout synchronous wait.
Both directed and shared enqueue already wake registered waiters irrespective
of masks; the requested-set condition explicitly consumes the signal. This
avoids changing general ignored-signal handling or adding new concurrency
state. Independent ordinary review found no critical/important defect in this
slice and checked registration, wake remembrance, unrelated interruption,
unblocked requested signals and timeout conversion. Broader signal review is
still required.

Frozen fixed kernel: `sigchld-wait-kernel.Image`, SHA-256
`fd76dd38743ba62441134eb0085d610295989fc48302ddfef3adad4136cb99ff`.
The identical prototype archive `128bba49d10ea7dce31480c2ec609cebe851b7e99b713c194d77130ff0b78c2d`
now passes all 10,000 waits in QEMU. `sigwait-all-green` passes fourteen
programs, including the earlier one-second regression, all previous signal
checks and provenance checks, in 15.911 s with normal QEMU exit. The final
completion-timestamp variant has separate `sigwait-completion-*` artifacts;
its native 10,000-iteration run passes. The final fourteen-program
`sigwait-completion-all-green` run passes in 15.866 s, QEMU exit 0, archive
`262602d9f50fd7192e98caa75b65bdc86f4bf35dd5c75e2a11fbf60c248c6eca`.
Formatting and diff whitespace checks
also pass. No architecture-wide lint or additional ABI coverage is claimed
from this focused slice.

Added the finite `SigwaitPending` model and `UnblockWaitedSignal` negative
control. TLC finds the old `Check -> Send -> Cancel` loss in four trace states
(7 generated/distinct); the corrected graph has 10 generated / 9 distinct,
queue empty. Fresh full run `target/signal-job-control-model/run.M9uMaCyK/`
passes **nineteen negative controls and seven corrected models**. The first
container invocation lacked Java and is not counted; the existing host Java
and cached, checksum-verified JAR completed the run without installation or
download. Model limits include one signal/waiter, atomic send-plus-wake and no
registration, fairness, timeout, competing consumer or weak-memory proof.

The board was recovered to RockOS, boot ID
`e167cfc7-20e5-48c1-987a-8c70c4efd84f`. Partition 2 is unmounted; read-only
fsck exits 0, and all three installed default hashes remain identical to the
previous checkpoint. Evidence: `provenance-final-rockos-check.log`. The
preserve-mask kernel has **not** been staged or tested on hardware yet. The
SIGCHLD bug is software-reproduced and fixed for its tested contract, but the
runuser/openbox shutdown delay is not declared fixed or attributed to it.
No commit, push, default promotion or network integration occurred.

### Preserve-mask hardware qualification and supervision control experiment

The previously pending hardware qualification is now complete for the focused
SIGCHLD fix. The frozen `fd76dd38743b...` kernel and the exact fourteen-program
QEMU archive `262602d9f50f...` were staged as an explicit menu canary, not as a
default. Manifest: `target/signal-group-stop/sigwait-physical-candidate/manifest.json`;
menu SHA-256 `3958659b6a63dcb5ca035bd8ecfa70bdf7ed8c48807cd099a276609a9132b406`.
The physical probe produced fourteen distinct successful regression markers,
including `SIGWAIT_RACE passed=10000 failed=0` and all three provenance cases.
Selection to fresh firmware prompt took 38.843 s, including firmware startup.
Evidence: `sigwait-physical-probe.serial.log` and `sigwait-board-probe-run.log`
under `target/signal-group-stop/`.

The same kernel then booted the unchanged normal desktop Stage1. The root
console was observed after 10.469 s and the Firefox window after 59.727 s;
these are host observations, not precise render timestamps. The local-page,
JavaScript/button and 1920x1080 framebuffer checks completed successfully in
6.799 s (`sigwait-desktop/`, `sigwait-desktop-content.*`). This does not qualify
Internet browsing, physical keyboard/mouse input, or a new human visual review.

To separate a kernel failure from userspace supervision behavior, the bounded
`runuser_broadcast.py` experiment creates only an owned `runuser -> sleep`
process pair, stops both, sends TERM then CONT to both, and records the result.
It tries three parent-first and three child-first STOP orders, with subreaper
cleanup and no broadcast signals to unrelated processes. Exact script SHA-256:
`0cac38b0a863ae4601ccd5a48c75d82223eb905e447cc4fa1ca9a98ec0a5983d`.
Native Linux with util-linux 2.39.3 and physical Asterinas with util-linux
2.41.5 each observed two timeouts among six trials. In both, the timed-out
supervisor was stopped again. Native records also show the child zombie and
pending SIGCHLD. The board's long JSON records were truncated on serial, so
the board child state and masks are **not** established by those records.
The script exit status 0 means bounded experiment/cleanup completion, not
that all six process pairs exited promptly. Evidence: `runuser-broadcast-native.log`
and `runuser-broadcast-board.serial.log`.

The matching upstream `su-common.c` implementations use `waitpid(WUNTRACED)`
and issue a new self-directed SIGSTOP after observing a child stop. A stop
report already returned around the controller's CONT is a plausible route to
a later self-stop. This is a userspace-interleaving hypothesis, not yet proof
of the exact desktop delay's cause. References:

- https://raw.githubusercontent.com/util-linux/util-linux/v2.39.3/login-utils/su-common.c
- https://raw.githubusercontent.com/util-linux/util-linux/v2.41.5/login-utils/su-common.c
- https://raw.githubusercontent.com/systemd/systemd/v257.13/src/shared/killall.c

A subsequent native `strace -f` attempt did **not** qualify this hypothesis:
the experiment failed its pre-CONT ordinary-stopped-state deadline while the
trace showed ptrace stops. Cleanup completed and the command exited 1. This
is an instrumentation/oracle incompatibility, not evidence of an additional
kernel bug. Keep `runuser-broadcast-native.strace` as failed diagnostic evidence;
do not count it as another six-trial run or silently accept tracing stops as
the original non-traced oracle.

The desktop diagnostic shutdown after these experiments was fast this time:
sync acknowledgment 1.775 s, reboot request to fresh firmware prompt 21.930 s,
then RockOS login succeeded (`sigwait-desktop-recovery.serial.log.json`). The
pre-shutdown process table identifies PID 118 as runuser and PID 120 as openbox.
At 321.919 s the provenance record attributes a SIGKILL enqueue for 120 to
118, followed by 118 terminating on SIGTERM; there is no observed 90-second
wait in this run. The log also reports `/dev/kmsg buffer overrun`, so absence
of other detailed records is not evidence that no other signals occurred.
This diagnostic run had extra workload/dwell and changed console log level:
it is neither a clean performance A/B nor proof the intermittent delay is fixed.

Fresh model run `target/signal-job-control-model/run.3bDR0aXu/` completed with
all nineteen expected negative controls and seven corrected finite models,
using the existing checksum-verified JAR and host Java without downloading.
The abstraction/refinement limits documented above remain unchanged.

Final hardware state: RockOS boot ID `0d0e6439-c14c-4524-9cc7-695b0373ad3b`;
partition 2 is unmounted and read-only `e2fsck -fn` exits 0, with 19505/131072
files and 231205/524288 blocks (`sigwait-final-rockos-fsck.log`). The installed
default kernel, Asterinas menu and top-level extlinux hashes still match the
previous checkpoint. No default promotion, persistent rootfs edit, commit,
push or network integration occurred.

Next: keep the independently reproduced SIGCHLD kernel fix separate from the
desktop supervision hypothesis. Before replacing the Openbox `runuser` wrapper
with direct credential-drop execution, qualify UID/GID/groups, capabilities,
environment and PAM/session differences in a small userspace test. Do not
change SIGSTOP semantics, add a privileged fallback, or shorten shutdown
timeouts to hide the symptom. Full signal/restart/ptrace scope and ordinary
independent review remain open.

### In progress: raw pselect6 stop/restart boundary

Current source inspection finds that `pselect6` returns `do_sys_select`'s
internal EINTR directly and never copies its remaining timeout back. The
already-fixed `ppoll` wrapper provides the local pattern, and Linux v6.12
`fs/select.c` (`do_pselect`, `core_sys_select`, `poll_select_finish`) provides
the reference contract. Add a raw-syscall regression before production edits:
no-handler STOP/CONT must resume an indefinite wait; a caught handler must
return EINTR even with SA_RESTART; writable timeout copyback must decrease;
read-only timeout must preserve success and prevent restart on interruption;
temporary masks must remain effective through stopped signal delivery and
restore on return. Gate interrupted cases on sleeping state plus the changed
mask, not merely a readiness write or fixed delay.

The scoped implementation will reuse existing signal delivery, mask restore,
and remembered-wake synchronization. It must not introduce a new lock, shared
restart state, or claim that pselect counts stopped time like poll's restart
block. Broader select/poll and default-disposition SA_NOCLDSTOP coverage remain
separate required follow-ups.

The focused pselect6 fix is now implemented. The initial seven-case test
passed on native Linux and failed four cases on the frozen `fd76dd...` kernel:
unbounded STOP/CONT returned EINTR without a handler; writable timeout expiry,
handler interruption and stopped pending-handler delivery left the timeout
unchanged. Copying the remaining duration back and converting only internal
EINTR to ERESTARTNOHAND fixes all four. Failed timeout copyback retains the
original result (success stays success; interruption stays nonrestartable).
No new mask operations, locks, atomics or shared restart state were added.

The independent specification pass identified missing finite no-handler STOP
combinations. The test now includes both: writable timeout must replay and
expire successfully; read-only timeout must return EINTR with no handler and
the original mask restored. All nine cases require exact outcomes, child
exit and bounded cleanup. Signal delivery is gated on `/proc` state S plus
the temporary unblocked SIGUSR1 bit, rather than only a readiness write.

Frozen artifacts under `target/signal-group-stop/`:

- `pselect-kernel.Image`: SHA-256
  `7541ec3ce2b6c30e5c1f40e6d552d135fbb1602b7f54bc989097973d8c1bebb4`.
- Final C source: SHA-256
  `7c4f9dccae6ab0c532e8a09d2a93a0ea682749c072be02c5eb19135b7ba3fcbe`.
- `group_stop_pselect-extended`: SHA-256
  `b2f06d610dfcd03c4646d33d08f2fc02fd997303dd9813c142b7f6813c2b2bfd`.
- `pselect-extended-native.log`: main-agent native rerun, 9/9.
- `pselect-extended-red`: old kernel 4/9; `pselect-extended-green`: fixed
  kernel 9/9 in 3.017 s. Both use identical archive SHA-256
  `98209d9a19014ab84145469e4fa9f5e9b85b9361c89bc3e0963ed6ea3f5a7978`.

The initial seven-case binary remains frozen separately as
`group_stop_pselect`; do not mistake it for the current nine-case source.
The first full fifteen-program run (`pselect-all-green`) uses that initial
binary and passes in 16.331 s. The eight preexisting signal/job-control/ptrace
programs pass in `pselect-m1-green`, 10.524 s. Each runner requires all named
guest exit markers plus QEMU exit 0, not only successful boot.

Final combined rerun `pselect-final-all-green` substitutes the nine-case
`group_stop_pselect-extended` binary and passes all fifteen programs in
18.516 s, QEMU exit 0, archive SHA-256
`f6a0bc6c6945d3d9b9ff564f062603f23ebc48ebaa6e66aa403843eae36c2363`.

Offline release build completes in 22.75 s (`pselect-build.log`), RISC-V
clippy in 10.32 s with 20 warnings (`pselect-clippy-riscv.log`), and x86-64
check in 5.56 s with 12 warnings (`pselect-check-x86_64.log`). These are not
warning-free full-lint results or x86 runtime qualification. Scoped Rust/C
formatting and whitespace checks pass. The ordinary specification and quality
passes found no Critical/Important issues after the two additional cases.
A stale adjacent select FIXME was narrowed to the still-unfixed select path;
that subsequent edit changes only a comment, not the frozen kernel behavior.
The fresh-reviewer allocation limit required the spec reviewer to also perform
the quality pass; it was independent of both the test author and root's Rust
implementation, but not a second independent reviewer.

This slice exercises `nfds=0`, not fdset replay or STICKY_TIMEOUTS. Existing
finite models were not modified and do not prove the pselect mask/timeout ABI;
the evidence here is source reasoning plus Linux differential/runtime tests.
The new kernel has not been deployed to hardware. Last verified board state
remains RockOS at the prior checkpoint; no board action, default change,
commit or push occurred in this slice. Raw select/poll restart, disposition
flags and remaining full-scope reviews/qualification are still open.

## Disposition metadata and child-notification slice

The next differential test exposed a separate compatibility defect:
`SigAction::Dfl` and `SigAction::Ign` discarded flags and masks, and child
STOP/CONT notification honored `SA_NOCLDSTOP` only for a user handler.
Blocking SIGCHLD with a default handler and SA_NOCLDSTOP therefore still
produced a signal for STOP/CONT, although only the wait event should remain.

`SigAction` now holds a private handler kind plus flags, restorer and mask
for every kind. ABI conversion is centralized. Reset-on-delivery changes
only the handler; exec clears metadata while preserving an ignored handler.
Child-state notification checks IGN or NOCLDSTOP independently of handler
kind, retains the disposition mutex through the enqueue decision, and wakes
the children wait queue after unlocking even when SIGCHLD is suppressed.
No new locks or unsafe code were introduced. SA_NOCLDWAIT/autoreaping is not
implemented by this change. The Linux reference is v6.12 `kernel/signal.c`,
`do_notify_parent_cldstop`, `flush_signal_handlers` and the SA_ONESHOT path:
<https://raw.githubusercontent.com/torvalds/linux/v6.12/kernel/signal.c>.

The registered `group_stop_disposition.c` has seven aggregate cases: DFL/IGN
query metadata, default STOP/CONT/exit notification with and without
NOCLDSTOP, DFL/IGN fork-plus-exec metadata lifecycle, and SA_RESETHAND handler
reset with metadata retention. STOP is wait-confirmed before CONT, and
notifications are consumed separately to avoid standard-signal coalescing.
The test has bounded waits and owned-child cleanup. The lifecycle cases use
SIGUSR2, not ignored SIGCHLD, to avoid depending on unimplemented autoreaping.

Frozen evidence under `target/signal-group-stop/`:

- `disposition-kernel.Image`: SHA-256
  `a0f55793b84f5bc76b2d3f47670de06962735aca010c1598f80994d0525eca7c`.
- Final C source: SHA-256
  `75fed54fb7f3d6f23242c1d621065ae5e10b64b7dbf7da4d6207e0befb8d5ba3`.
- `group_stop_disposition-final`: SHA-256
  `9920d2ebc0d2441d1763dba7c4dd3ecb0999247708993542267bbfef5d5fb119`.
- `disposition-final-native.log`: Linux 7/7.
- `disposition-final-red`: previous 7541 kernel 1/7;
  `disposition-final-green`: a0 kernel 7/7, 0.734 s, QEMU exit 0.
  Both use archive SHA-256
  `507db5061b9ee39048939388d28c26bab1a2fa8417811db3312e360a3c373f38`.
- `disposition-all-green`: all 16 programs exit 0, QEMU exit 0, 18.545 s;
  archive SHA-256
  `0dcdae8cb072d642e9152e259ad5325d187dc2763e6299ac7a371e349e8fd47a`.
  This includes the final nine-case pselect test, SIGCHLD race, provenance,
  lifecycle, ptrace/parent and all earlier group-stop restart/wait tests.
- `disposition-m1-green`: eight preexisting regression programs exit 0,
  QEMU exit 0, 10.472 s; archive SHA-256
  `eee7c2f6969cdf51369ffd27dd5bc680e89fbef14b5e3a2be142252a6e571c3e`.

Offline release build completes in 22.46 s (12 warnings); RISC-V clippy in
8.65 s (20 warnings); x86-64 check in 8.06 s (12 warnings). Logs are
`disposition-build.log`, `disposition-clippy-riscv.log` and
`disposition-check-x86_64.log`. Scoped formatting and whitespace checks pass.
These are not full-lint or x86 runtime results. A subsequent disposition
inheritance comment correction does not alter frozen-kernel behavior.
Independent ordinary spec and quality passes found no required changes.
The allocation limit again required the same independent reviewer for both
passes, distinct from the C test author and the root Rust implementer.

`ChildNotification.tla` adds bounded exploration of concurrent disposition
updates and notification commit, with wait wake as a separate unlocked step.
Two deliberate faults demonstrate that splitting policy check from enqueue
permits a stale decision, and suppressing wait wake along with SIGCHLD loses
the independent notification. Fresh run `run.QTTkDY0h` has 21 expected
negative counterexamples and eight corrected configurations with no violated
invariant. New negative state counts are 19/18 and 14/14 generated/distinct;
the corrected model has 28/26, queue empty. See
`disposition-models-split-wake.log` and the model README for source mapping.
The cached pinned TLC JAR and host Java were reused without installation.
This is finite safety checking, not a proof of all Rust interleavings,
weak-memory behavior, actual waiter registration or eventual wakeup.

Runtime coverage does not directly add a DFL|NOCLDSTOP ptrace-trap case or
a blocking-wait wake test (existing ptrace and parent-wait programs regress
those surrounding paths). Restorer ABI, unknown flags, concurrent disposition
stress and SA_NOCLDWAIT remain outside this focused test.

### Physical deployment capacity incident

The initial canary publication failed before reboot: `/boot` was full, and
direct `install` left a 4,202,496-byte partial hash-named a0 kernel. There was
no CANARY_READY marker; no partial image was booted. Evidence is preserved in
`disposition-physical-run.log` and `disposition-physical-candidate.serial.log`.
RockOS stayed at boot ID `0d0e6439-c14c-4524-9cc7-695b0373ad3b`.

Only three known earlier signal-test canary sets (4902, 40e2, fd76), their
probe archives and optional selectors, plus the failed partial image were
moved to `/var/tmp/asterinas-canary-archive.jnop3GqH` on RockOS root storage.
Known source hashes and destination content were checked. The first seven
files have an archive SHA256SUMS; the later fd76 set hashes are recorded in
`disposition-boot-archive-sigwait.log`. All are recoverable; do not restore
the partial a0 file over a subsequently validated image. Unknown older
artifacts and installed defaults were left untouched.

`disposition-boot-archive.log` and `disposition-boot-archive-sigwait.log`
record the recovery. A fresh read-only preflight reports 32,176,128 available
bytes on `/boot`, p2 unmounted, and all three installed default hashes
unchanged. The serial root shell was explicitly exited back to the ordinary
RockOS prompt (`disposition-root-shell-exit.log`). The retry uses a fresh
`disposition-physical-retry` candidate and the exact 16-program QEMU archive.
The ignored qualification helper now derives expected ordered markers from
the archive after checking recorded hashes, rather than hardcoding a suite.

Deployment follow-up remains open: the production publisher needs a capacity
preflight and atomic publication so ENOSPC cannot leave a partial final-name
file. Recoverable archival fixed this deployment's capacity, not that tooling
defect. No default promotion, commit, push or network merge occurred.

The retry completed successfully (`disposition-physical-retry.log` and
`.serial.log`): all sixteen exact, ordered program markers are zero;
`group_stop_disposition` reports 7/7 and `group_stop_pselect` 9/9. The probe
through fresh OpenSBI/U-Boot recovery takes 46.986 s. The helper then boots
RockOS and confirms Linux 6.6.87 and new boot ID
`0f5807da-986e-49eb-a997-be15a4fdca8d`; its host exit status is 0.

SSH postflight confirms the installed a0 kernel and 0dcdae probe hashes match
the QEMU-qualified artifacts, all three default hashes remain unchanged,
and p2 is unmounted. `/boot` has 12,231,680 available bytes after deployment;
another new candidate must not assume sufficient capacity. Read-only
`e2fsck -fn /dev/mmcblk1p2` exits 0, reporting 19,505/131,072 files and
231,205/524,288 blocks, with no repair performed. Evidence is in
`disposition-postflight.log` and `disposition-postflight-fsck.log`.

This qualifies the focused signal slice on hardware, not the full remaining
signal goal or Firefox performance. Desktop shutdown supervision, raw
select/poll restart coverage, broader architecture/runtime checks and the
remaining full-scope review remain open. The board is left in RockOS, with
the new image available only through its optional canary selector.

## Raw poll/select restart and x86 runtime qualification

The preceding goal turn made concrete progress (disposition fix, model
counterexamples and physical regression). This continuation addressed the
remaining raw poll/select restart gap rather than treating the existing
RISC-V suite as coverage of x86-only syscall entries. RISC-V libc uses
ppoll/pselect6; `SYS_POLL` and `SYS_SELECT` are registered only in the x86
syscall table. The new registered C test explicitly skips architectures
without those raw syscall numbers, and that skip is not counted as runtime
qualification of either entry.

Linux v6.12 `fs/select.c` (`poll_select_finish`, `do_restart_poll`, and
`SYSCALL_DEFINE3(poll)`) distinguishes two behaviors:

- Poll saves an absolute host-monotonic deadline and uses restart_syscall
  without a caught handler. Time spent stopped consumes the timeout. Each
  replay rereads the pollfd array and checks readiness even if already expired.
- Select writes the remaining timeval, then replays its user arguments only
  without a caught handler. Its saved duration excludes time spent stopped.
  Read-only timeout copyback preserves the syscall result but disables replay.

The previous implementation returned EINTR for both after an unhandled STOP,
and select never updated its timeval. The source reference is
<https://raw.githubusercontent.com/torvalds/linux/v6.12/fs/select.c>.

`PollRestart` now stores fds/nfds/deadline in the existing per-thread restart
block. It saves itself again on interruption, allowing repeated STOP episodes.
The shared polling helper accepts `Timeout::When` for poll, while ppoll and
select/pselect retain `Timeout::After`. This avoids recomputing a relative
duration before descriptor setup and accidentally extending poll's deadline.
Select mirrors the existing pselect timeout-copyback/NOHAND wrapper. Caught
handlers return EINTR even with SA_RESTART. No new locks, atomics or unsafe
code were introduced; the existing registration-before-sleep path is retained.

### Differential tests and independent review

`group_stop_poll_select.c` directly calls the raw syscalls with a pipe-backed
descriptor. Its final 21 cases cover expiry, caught handlers, STOP/CONT,
infinite waits, read-only select timeouts, continuous restart after two STOPs,
and readiness posted while stopped past poll's deadline. The parent confirms
sleeping syscall entry and wait-reported STOP before sending further events;
every child is bounded and owned/reaped. Poll's seeded revents must be cleared
on interruption. The final source SHA-256 is
`fd015adfc3a61df4d7f9106d6901f92ab50ab61093a1cd6b15749ea41c8eab0c`;
the binary `group_stop_poll_select-extended.native` is
`c0214c063fb6f886d364d391e287a89d1945249e2f57973f4bdc314089218484`.

Independent ordinary review caught insufficient timing assertions in the
initial test. The final test requires select's resumed remaining wait, spends
100 ms before STOP_READY to distinguish full-duration replay from copyback,
rejects negative timeval microseconds, and bounds ordinary expiry from below.
The reviewer approved both specification and quality after these changes and
the final repeated-stop/expired-readiness additions. No retired review skill
was used. As in the preceding slice, one independent reviewer performed both
passes; the root implemented the test and kernel changes.

Evidence under `target/signal-group-stop/`:

- `poll-select-extended-native.log`: native Linux 21/21.
- `poll-select-x86-baseline.bzImage`, old kernel SHA-256
  `e80ff81149671ea6410229571a3aa4bd301944b8a8314bb76978d93cb01889ea`.
- `poll-select-x86-fixed.bzImage`, fixed kernel SHA-256
  `5ec03b884511a55eb4bcd0babf3723968d1a0df8b2d3e07caa2919374bd8ea88`.
- `poll-select-x86-extended-red`: old 8/21, 4.122 s;
  `poll-select-x86-extended-green`: fixed 21/21, 5.578 s. Both use archive
  `814abdbf049185983879b35959a6e99c72ef41e8b3b43e0cee90c3e8ab273d15`.
  The earlier 17-case red/green archive remains separately frozen.
- `poll-select-x86-final-all-green`: all 11 programs return 0 in 18.026 s,
  including group participation, pselect, ppoll, futex, waits, sigtimedwait,
  restart context, ptrace and ptrace-parent notification. Archive SHA-256
  `08902e654538dfc044f1eb04cab784dec578a1dda7fca105c0215cf951991401`.
  In particular `signal_sigsuspend_ptrace` now runs on Asterinas x86, reports
  cancelled handler and both restored mask bits, and passes. This closes the
  previously recorded missing x86 runtime check for that individual test.
- `poll-select-riscv-fixed.Image`: SHA-256
  `df763234e77d675168f8b371935db0bbb008ad90d1ac29984305425ca1673dfd`.
  `poll-select-riscv-all-green`: all 16 existing programs return 0, QEMU
  exit 0, 18.573 s, using the unchanged `0dcdae8cb072...` disposition archive.

The minimal x86 runner uses a four-vCPU KVM microvm with
the cached OVMF microvm firmware and Linux-protocol bzImage. It uses a static
init that mounts proc, forks the explicit test list and powers off. It requires
exact ordered zero result markers, an init completion marker and QEMU exit
33, which is OSTD's success value through isa-debug-exit (65 means failure).
This differs intentionally from the RISC-V QEMU exit-0 gate.

Initial harness attempts are retained as failures, not test results: direct
multiboot ELF with SeaBIOS remained in firmware; the same ELF with OVMF was
not bootable. Switching the build's `--grub-boot-protocol linux` produced the
required bzImage. The first bzImage reached init but it exited 2 because the
minimal init had omitted creating `/proc`; `earlycon` exposed the diagnostic.
Adding mkdir and explicit setup error reporting fixed the harness, without
changing the kernel. The valid red baseline was obtained before kernel edits.
No toolchain or image downloads occurred; Linux setup packaging used the
cached offline build, including a locally compiled setup executable.

Release rebuilds took 22.63 s for x86 (plus 0.61 s cached Linux setup packaging)
and 22.59 s for RISC-V. Both architectures' clippy commands pass with 20
warnings each (x86 15.20 s; RISC-V 7.94 s), not warning-free full-lint results.
Scoped Rust/C formatting, whitespace checks and RISC-V compilation of the
explicitly skipped raw-syscall test pass.

The existing formal suite was rerun without modifying its models:
`target/signal-job-control-model/run.NhyYz4Gj`,
`poll-select-models.log`: all 21 negative controls and eight corrected finite
configurations have expected results. These models cover their documented
coordinator/wakeup contracts; they do not prove this new poll/select ABI or
the Rust implementation. This slice's restart record is thread-private and
adds no shared synchronization; its evidence is source/lock-path inspection,
independent review, differential tests and actual cross-architecture execution.

This remains a focused checkpoint, not completion of the whole group-stop
goal. Whole-change lifecycle/lock-graph review, remaining scenario coverage,
desktop shutdown supervision and scoped integration are still open. No commit,
push, network merge or installed-default promotion occurred.

### Current-image physical check and recovery

`poll-select-physical-preflight.log` confirms RockOS boot ID
`0f5807da-986e-49eb-a997-be15a4fdca8d`, an idle ttyS0 bash, unmounted p2,
unchanged defaults, and 12,231,680 bytes available on `/boot`. The existing
0dcdae probe archive is hash-identical to the current QEMU archive, so the
publisher transfers only the 5,869,096-byte df7632 kernel and optional menu.
The publication log has no stage1 download. Capacity was checked before
publication; this does not fix the publisher's previously noted ENOSPC flaw.

`poll-select-physical.log` and `poll-select-physical-candidate.serial.log`
record all 16 exact ordered zero result markers and fresh firmware recovery
in 46.110 s. The helper subsequently logs in to RockOS, exits 0, and confirms
new boot ID `ae699152-1e70-4b38-bcb0-5821df953382` with Linux 6.6.87.
SSH postflight verifies the df7632 kernel, 0dcdae archive and all three
unchanged installed default hashes. Read-only `e2fsck -fn` on unmounted p2
exits 0 with 19,505/131,072 files and 231,205/524,288 blocks. No repair or
filesystem mounting was performed. Evidence: `poll-select-postflight.log`
and `poll-select-postflight-fsck.log`.

The board is left in RockOS. `/boot` now has 6,360,064 available bytes;
further candidate publication must first make adequate recoverable space
and check the complete set of missing artifacts, not rely on this run's
capacity. This continuation archived/deleted no additional remote artifacts.
The eight older M1 RISC-V regression programs also pass on df7632 in
`poll-select-m1-green`, 10.468 s, QEMU exit 0, archive
`eee7c2f6969cdf51369ffd27dd5bc680e89fbef14b5e3a2be142252a6e571c3e`.

Remaining test limits for this slice include STICKY_TIMEOUTS, descriptor-array
mutation by a tracer or sibling during stop, and broad poll error-path ABI
coverage; no assertion is made about those from the 21 cases. Physical results
exercise shared polling and signal paths through RISC-V ppoll/pselect, not the
x86-only raw poll/select entrypoints, which were validated in x86 QEMU.

## Lifecycle integration review and logical stop observation

The ordinary whole-lifecycle review covered `signal/job_control.rs`, process
group-stop methods, clone publication, thread/process exit, first userspace
entry, and ptrace acknowledgment/resume/detach. It found no actionable defect
in those contracts. Membership initiation/enrollment/removal uses TaskSet ->
coordinator -> participant; traced acknowledgment/detach uses tracee state ->
coordinator -> participant. Notifications and wake callbacks run outside the
coordinator, with no reverse coordinator -> membership/tracee-state edge found.
The reviewer checked exec sibling termination does not commit terminal group
exit, and that a returning old ptrace stop does not erase a later obligation.
This is source review, not exhaustive Rust concurrency or hardware proof.

Six exact-name implementation ktests were rerun, each reporting **one passed,
zero failed, 225 filtered out** in `integration-ktest-<name>.log`:
`continue_cancels_selected_stop`, `exit_and_pending_kill_prevent_stop`,
`repeated_stop_preserves_completed_notification`,
`traced_member_restarts_incomplete_group_stop`, `group_stop_participation`,
and `group_stop_cancel_and_rejoin`. These executed the real private Rust
coordinator methods, not model replicas. They ran before the following proc
observer edit, which does not change those coordinator methods.

### Reproduced observability defect and repair

The original multithreaded test checked stopped userspace counters, but did not
yet check the plan's per-member proc state requirement. Adding that check
reproduced an actual defect: an ordinary blocked USR1 wake temporarily schedules
a stopped thread in kernel context, and the old `sleeping_state()` reports `R`
because it uses the scheduler CPU field and registered waiter. This does not
mean the thread escaped into userspace; its counter remains stopped.

`PosixThread::stopped_state()` now reads committed stop ownership independently
of transient scheduling. A tracee-state-locked ptrace hold yields `t`. Otherwise,
under the coordinator and participant locks, an active group-stop request plus
the thread's `Acknowledged` marker yields `T`. `Pending` is not prematurely
reported stopped, and `PtraceControlled` alone is not an execution hold after
tracer resume. Cancellation of the process stop latch prevents stale ACK
reporting during group exit. The locks follow existing ordering and are dropped
before taking `signalled_waker` for the ordinary S/D/R fallback. An old stop
waiter remaining registered after release no longer independently supplies T/t.
This adds no shared state, generation counters or unsafe code.

The final `group_stop.c` has eight aggregate cases. Its group case combines a
pause-sleeping leader and three busy userspace workers for 32 STOP/CONT rounds.
After wait-confirmed STOP it samples both `status` and `stat` for every member,
while alternating blocked process-directed and nonleader-thread-directed USR1.
It also retains stable shared-counter assertions. A separate ptrace case checks
64 held-state samples during USR1/CONT and requires `t` in both proc files,
then verifies userspace progress after explicit tracer resumes.

The first ptrace test extension failed on Linux (7/8) because it omitted the
pending SIGCONT signal-delivery stop after the first PTRACE_CONT. Parent-only
strace records CLD_TRAPPED/SIGCONT; existing `group_stop_ptrace.c` already has
the correct two-resume sequence. The new test was repaired to consume that
stop explicitly, without changing kernel semantics. Both the failed native
log/trace and the final passing log are retained. Ordinary independent review
approved the final test and stopped-state snapshot without actionable findings;
the reviewer inspected saved artifacts, not independently rerun executions.

### Frozen final comparison and cross-architecture execution

Final source `group_stop.c` SHA-256:
`24421e40cd96b3e726f8f0499f98f9fdf125a25daca4f2b650a996dc55f1ba23`.
RISC-V binary `group_stop-state-final`:
`717f9905540cfa267e7f94daf70117235a9b1a86558f5d3764e06b807c614a24`.
Native/x86 binary `group_stop-state-final.native`:
`37f4a5dd858d373b8c6f74ea3f2cbec61d2be2d36d9442fcb5691604b6a16697`.

- Linux final oracle: **8/8**, `stop-state-final-native-v2.log`.
- `stop-state-final-red`: old df7632 kernel **7/8**, first group round reports
  `tid=14 state=R expected=T`, 1.263 s. QEMU exits 0 but the program marker is
  1; this is correctly recorded as a failing test, not a passing boot.
- `stop-state-final-green`: fixed **8/8**, all 32 group rounds and ptrace case,
  QEMU exit 0, 2.050 s. Red and green use exactly the same archive, SHA-256
  `bf0b6deb81c4345e06049a809724b551756f666cc081406dafaad60e7708eb4b`.
- New RISC-V `stop-state-kernel.Image`: SHA-256
  `dc25f4ec117846a1fa3f71b6dbb16755eefef550a033192f0f9cc5f6dee54f64`.
  `stop-state-riscv-all-green`: all **16** programs return 0, QEMU exit 0,
  18.885 s; archive
  `8bef4db3fff5acd06651aaaab6814a1c97c89cad5f31dea08011f4c50c482556`.
- New x86 `stop-state-x86-fixed.bzImage`: SHA-256
  `dfb295c9da19c1e8cc83caedb09f448182ba827a1dfef8ec491f60b7f6a70a6f`.
  `stop-state-x86-all-green`: all **11** programs return 0, X86_PROBE_COMPLETE
  and QEMU success exit 33, 17.748 s; archive
  `3cdc7c30dc6c8156c94136390d1355d1f5c3400d729cbddb0fcd8849ed05b2a3`.

The suite lists are the preceding raw poll/select checkpoint's exact lists,
replacing only the old group-stop binary with the new eight-case binary.
RISC-V includes the lifecycle/parent-tracer tests that read proc states; x86
includes the actual ptrace-cancelled sigsuspend and raw poll/select entrypoints.
The x86 rebuild takes 22.29 s plus 0.60 s cached setup packaging; RISC-V takes
23.67 s. Cached clippy completes for both architectures with **20 warnings**
each (RISC-V 6.42 s, x86 6.68 s); this is not warning-free full `make check`.
Scoped Rust/C formatting and Rust whitespace checks pass.

The unchanged formal suite was rerun at
`target/signal-job-control-model/run.PC7aWPfg`, log `stop-state-models.log`:
all **21 negative controls and eight corrected configurations** have their
expected outcomes. The protocol-to-lock mappings remain applicable to group
stop, SIGCONT, clone/exit and ptrace ownership. There is **no new model of the
proc observer**: this rerun does not prove its snapshot implementation,
linearizability, Rust atomics or weak-memory behavior. Its added evidence is
explicit lock inspection, independent review, and native/red/green execution.

### Remaining integration boundary

This continuation has made no board connection or boot/storage/default change.
The latest physically qualified image remains df7632, recovered in RockOS with
boot ID `ae699152-1e70-4b38-bcb0-5821df953382` as recorded above; this is a prior
observation, not a new board health check. The last observed `/boot` capacity
is only 6,360,064 bytes, so do not stage the new kernel plus changed archive
without a fresh capacity check and recoverable archival of exact known files.

The latest candidate still needs physical qualification, the remaining bounded
multi-process/thread STOP/TERM/CONT workload gate, and reviewed scoped local
commits. The additional software integration gates below are now complete.
Earlier model/specification reviews plus the new whole-lifecycle lock review
close the source-review checkbox, not those remaining runtime gates. Modern
ptrace SEIZE/LISTEN/INTERRUPT, NOCLDWAIT automatic reaping, STICKY_TIMEOUTS and
the previously noted namespace gaps remain explicit limitations, not promises
of this patch. Desktop runuser/openbox shutdown supervision is still a separate
unresolved performance diagnosis. No whole-goal completion, Firefox latency
fix, push, network integration or default promotion is claimed.

### Additional software integration gates

Five additional independent QEMU invocations (`stop-state-repeat-1` through
`stop-state-repeat-5`) all pass the frozen eight-case test, each with 32/32
group rounds and the ptrace case. They reuse the same dc25f4 kernel and
bf0b6d archive, each taking 2.018–2.119 s. This is bounded repetition, not an
exhaustive interleaving argument. The eight unchanged older M1 programs also
all return 0 on dc25f4 in `stop-state-m1-green`, QEMU exit 0, 10.611 s, with
unchanged archive `eee7c2f6969cdf51369ffd27dd5bc680e89fbef14b5e3a2be142252a6e571c3e`.

The existing `tools/riscv/megrez_menu_qemu.py` runs both Basic and Probe with
dc25f4 and the unchanged normal Stage1 (`9ef253...`). Both pass in
`stop-state-basic-probe/{basic,probe-auto}`, respectively 0.590 s and 0.398 s.
Basic verifies shell interaction and proc/sys mounts before reboot; Probe
requires the pass marker and automatic reboot. Neither run attaches a disk,
network or physical board.

The existing clone3, execve, exit and pthread test Makefiles were compiled
offline with the cached RISC-V compiler and static pthread libc into
`target/signal-group-stop/process-integration-build/`, without modifying their
test sources. The 14-program `stop-state-process-final-green` run passes all
zero result markers, QEMU exit 0, 8.290 s; archive SHA-256
`80365becb2104b460f145cbc1f15d033f9a0785bde91c0515bbfc96c3c069768`.
It executes seven clone3 programs, `execve_mt_parent` (main-thread exec,
nonleader exec and CLONE_FILES interaction), `exit_code`, `exit_procfs`,
`set_tid_address`, `pthread_cond_handoff` (4,096 handoffs),
`pthread_signal_test` and `pthread_test`. No skip marker was emitted.
This broadens ordinary lifecycle integration; it does not exhaust races of
concurrent exec with every STOP/CONT/exit phase.

The ignored generic runner gained optional `SIGNAL_TEST_FIXTURES` paths so
`execve_mt_child` is packed but not executed as an independent test. The
fixture path also prepares `/tmp` and the existing BusyBox grep applet link.
The first run, retained as `stop-state-process-green`, is **13/14, failing**:
the minimal archive had no grep command link, so exit_procfs shell checks
failed with `sh: grep: not found`. Adding only that link corrected the harness;
the unchanged kernel and unchanged 14 binaries then all passed. No first-run
failure is attributed to or hidden by a kernel edit. Invocations without
fixtures retain the earlier archive generation behavior.

## Real-handler workload and physical sleep-test race

Added `group_stop_workload.c` and registered it with pthread flags and the
process runner. Four rounds each own two processes with a busy worker and an
interruptible sleeper. After wait-confirmed STOP, shared process-directed TERM
and privately thread-directed TERM must remain pending, with every member T
and unchanged userspace progress. CONT must execute exactly one actual handler
per process, finish both workers and exit normally. Every wait and failure
cleanup is bounded; only failed cleanup uses SIGKILL. Lock-free atomic shared
state, PID ownership and cleanup were independently specification/quality
reviewed, without the retired review skill.

Source SHA-256: `b9bfe911c6ec3c417fea359a8ef5684d60c12a4d0a24b3517311dfaa2f0dba0e`.
Frozen RISC-V `workload-riscv64`:
`14eff5cad8c7a3523b06f5cc4ac448bf53ce7219445828b1b206f36df0d1d9e3`.
Native `workload-native`:
`44efeff70260f72fd9e971921a86636bbcc754067ffe039b6418ee0a8fb1eb97`.
Native positive passes four rounds; the no-CONT negative control fails the
graceful-exit deadline, exits 1 in about 5.4 s and does not emit COMPLETE.
Independent review also reran both. Commands and identities are retained in
`target/signal-group-stop/workload-verification.md`; actual Makefile compilation
is recorded in `workload-registration.log`.

`workload-final-riscv-green` passes all 17 programs on dc25f4, QEMU exit 0,
19.596 s; archive `bb3c25ff1235c0e1c780048cee09cccf956203d9aaa82271a27bcde7fea9040b`.
`workload-final-x86-green` passes all 12 on dfb295, QEMU exit 33, 18.742 s;
archive `7cf5db12a85f714fcea5a0a38c687496d9c64910f6b3dbb6a3da3d8a4c4d9788`.
The ignored `verify_frozen_sources.sh` recompiled all 29 binaries from current
sources and every comparison was IDENTICAL (`final-source-recompile.log`).
This comparison preceded the sleep-test refinement below; it does not claim
that the new sleep binary equals the superseded one. Kernel source snapshot
`stop-state-kernel-sources.sha256` still matches. No kernel source changed in
this continuation; scoped Rust/C formatting and whitespace checks passed.

### First physical run is a failure, with safe recovery

Read-only preflight confirmed idle RockOS ttyS0, unmounted partition 2 and only
6,360,064 bytes available in /boot. Five exact old owned artifacts (a0f557 and
df7632 kernels, shared 0dcdae probe, d55b3a and 0c1add optional menus) were
copied with paths and metadata to
`/var/tmp/asterinas-stop-state-archive.v2fFbRiH`, synced, compared and hashed
before their originals were unlinked. Installed defaults remained identical.
Evidence: `stop-state-archive-preflight.log`, `stop-state-boot-archive.log`.

The dc25f4 kernel with the bb3c25 17-program archive ran in
`workload-physical-candidate`, log `workload-physical.log`. Result: **16/17**, not
a successful qualification. The new workload passed four rounds; the sleep
test returned 1 with 8/9 internal cases. Its clock-relative sibling/no-handler
case reported `phase=stop-wait result=116 status=0`: normal child exit while
the parent expected a stop, before any sleep-restart assertion. The helper
recovered through fresh firmware (50.567 s including the probe), booted RockOS
with ID `2827311d-8fdf-4971-b489-57b9991f78fa`, then exited 1. Read-only
`e2fsck -fn` on unmounted p2 exited 0 (`workload-postflight-fsck.log`). Defaults
and normal desktop Stage1/DTB hashes remained unchanged. An initial progress
message counted markers without checking values and was explicitly corrected
when the complete failure was read; it is not evidence of a passing run.

### Test setup race, strict retry classification and controls

The old test started a finite 150 ms sleep, waited for another readiness
message, then guessed a further 50 ms parent delay before STOP. A delayed
parent could stop after the sleep and process had already completed. A native
Linux control changing only the parent delay to 250 ms (plus diagnostics)
reproduces normal status 0 in all six uncaught cases: measured completion is
100 ms before STOP request, and the sleep itself correctly lasts 150/151 ms.
`sleep-late-stop-native.log` is **3/9, exit 1**. This establishes a test race;
the original board log lacks the timestamps needed to prove that this race
caused that particular physical failure. No kernel change is justified by
this evidence alone.

The revised `group_stop_sleep.c` publishes main readiness before worker
creation, observes the worker's actual sleeping state instead of a guessed
delay, and timestamps STOP immediately before the attempt. Exact timespec
comparison replaces rounded milliseconds for correctness assertions. A sample
is eligible for at most three setup retries only if its sleep returned zero,
errno and handler count are zero, the entire requested duration elapsed,
completion strictly predates STOP request, and the parent consumed a normal
zero exit. Exhaustion fails; retries never count as passes. This rule applies
even if STOP succeeds after completion but before process exit. A passing
sample must also finish no earlier than CONT. Durations and kernel behavior
were not relaxed. Three direct boundary checks reject a 149.5 ms result and
accept exactly 150 ms or longer.

Specification review initially caught two holes (successful STOP after sleep
completion and millisecond rounding); both were fixed before approval.
Independent specification and quality reviews approved the final source,
SHA-256 `1426f4f6ae23a5749ab83f794f6b16c49dce57d765338d443d3ebcab36bc4475`.
Frozen `sleep-entry-v3` RISC-V SHA-256:
`81bea5bdeb6723cd41a4f81900597b86002307bd8b687d711646d338a0bec7a0`;
`sleep-entry-v3.native`:
`035840a4b47feae55b975a043f73db5f7c9292e9b0e1cbe6bacba9132e6140d7`.
Native positive is **9/9, exit 0**. Two controls from this exact source add
250 ms parent delay, with the second also keeping the child alive for 500 ms
after publishing its completed result. Both reject all six uncaught cases
after three setup retries each: **3/9, exit 1, 18 retries**, not false passes
(`sleep-entry-missed-reviewed.log`, `sleep-entry-completed-stop.log`). Review
independently reran the positive and inspected these two controls/logs.

`sleep-entry-v3-riscv-all-green` replaces only the old sleep binary in the
17-program suite: all 17 return 0, QEMU exits 0, 19.089 s. Kernel remains dc25f4;
archive SHA-256 `f70c743f61608518c2e204f04ce1fe178e3db3c011efddef25c16f4d6b5bc934`.
`sleep-entry-v3-x86-green` passes the corrected nine-case sleep program, QEMU
exit 33, 2.990 s; archive
`6ebdc0246dd97dbe37b313abd8dd4dcbb7c16896a0c191fc8c4a5cad86b285ce`.
The other 12-program x86 suite's sources and binaries are unchanged.

For the next physical attempt, references and hashes of only the failed bb3c25
probe and 8be1dc optional menu were checked. Both were copied, synced and
verified before unlink into
`/var/tmp/asterinas-sleep-entry-archive.Xa501U9Y` (`sleep-entry-archive.log`).
The dc25f4 kernel remains installed for reuse. Available /boot space is now
26,196,992 bytes; default selectors are unchanged. This archive preserves
the failed candidate rather than deleting its evidence.

### A separate physical sigtimedwait deadline failure remains open

The next complete physical run, `sleep-entry-v3-physical.log`, is also **16/17**:
corrected sleep passes 9/9, but the unchanged 10,000-iteration sigtimedwait test
is terminated by its 15-second SIGALRM (marker 142). Every other program
returns zero. Probe plus firmware recovery takes 58.677 s; the helper recovers
RockOS with boot ID `6c4cab41-d016-43c4-90d5-e55d01ab2e14` before exiting 1.
Read-only p2 fsck again exits 0 (`sleep-entry-v3-postflight.log`). This is not
whole-suite qualification, and no timeout threshold or kernel code was changed.

A smaller three-copy probe adds only alarm-time phase/request/done/completed
counters to the sigtimedwait test. Its native and three-copy QEMU run pass;
physical `sigwait-progress-physical.log` is **2/3**. First and third complete
10,000 waits; the second reports `phase=1 request=3045 done=3044 completed=3044`
and exits 142. These separately loaded counters are advisory, not a coherent
state snapshot or proof of lost signal/wake. Recovery takes 37.048 s and
finishes in RockOS ID `84732d1c-1165-46cb-b0dd-8b4b0322f9ae`; unmounted p2
fsck again exits 0 (`sigwait-progress-postflight.log`). The frozen diagnostic
archive is `1743843b4cce5a146e6b13c4d9bf3b887045ef3a5686d289ee920e7341edf3b5`.
Its exact probe/menu (91afc8) were checked for references, copied, synced and
verified before unlink to
`/var/tmp/asterinas-sigwait-progress-archive.vYBxzNVw`. Defaults are unchanged;
the full f70c74 failing suite remains on /boot. Evidence:
`sigwait-progress-archive.log`; free space returns to 11,330,560 bytes.

The next diagnostic version adds elapsed/max-wait output every 1,000 iterations,
sender phase and current wait age, to distinguish cumulative scheduling delay
from a single stalled wait. Ordinary review caught blocking stderr output in
the initial handler: async-signal safety alone does not bound exit. The final
handler uses SA_RESETHAND | SA_NODEFER and rearms SIGALRM for one second before
best-effort writes, so a second alarm terminates with the default disposition.
The failure trigger remains 15 s; there is an explicitly additional one-second
failure-report grace, not a relaxed passing criterion. A full blocking stderr
pipe control terminates by SIGALRM in 1.015 s (`sigwait-alarm-bounded-control.log`).
Another reviewed issue sampled the clock before reading a concurrently updated
start; reading start first and omitting zero prevents unsigned age underflow.

Latest diagnostic source SHA-256:
`6f6e5e7b7d6a230c97e34713a5f452925ed51477154dfd7be8e20b71a80c23c2`.
`sigwait-timing-v2.native` passes 10,000 waits, independently repeated by the
specification reviewer. `sigwait-timing-v2-qemu` passes three identical copies,
QEMU exit 0, 1.601 s; archive
`b4ebc255aa43a7ab876d057264299b04cc801dac330b43c878377e985f9c4f9e`.
These are diagnostics, not a fix for the physical timeout. The current kernel
source snapshot remains byte-identical to dc25f4's recorded inputs. Latest
physical qualification and scoped commits remain open until this failure is
understood; the earlier finite models do not prove scheduler or timer behavior.

### Cumulative yield latency separated from signal-loss correctness

The timing probe (`sigwait-timing-v2-physical.log`) again returns **2/3**.
The first 10,000 waits take 0.644 s. In the failing second copy, 1,000/2,000/
3,000 completed waits take 4.640/9.417/14.129 s, and the maximum completed
wait is 79.727 ms. At the 15 s trigger, 3,180 iterations have completed and the
current wait age is only 13.737 ms, sender phase 0. The third copy completes
10,000 in 3.798 s. This establishes cumulative throughput delay for this run,
not a single 15 s stuck wait or newly demonstrated signal loss. It does not
establish which scheduler/placement/console mechanism causes the delay.
Recovery takes 37.948 s and finishes in RockOS ID
`309487fa-9ffb-4621-925f-a9578b01e388`; p2 fsck exits 0.

Preserved the exact yield version as `sigwait-timing-v2.c` with its frozen
binaries and physical archive for separate performance investigation. The
correctness regression now uses two POSIX semaphores for request and sender
completion rather than busy `sched_yield` polling. Posting the request still
races sender `kill` against receiver `sigtimedwait`; completion is consumed
before the next iteration, and shutdown posts a token to release the idle
sender before join/destruction. The 10,000 iterations, 1 s signal wait,
50 ms completion bound, 15 s failure trigger and 1 s reporting grace remain.
Native tests pass and independent specification/quality reviews approve.
This changes test coordination, not the kernel scheduler or a throughput claim.

Sensitivity check: the same `sigwait-blocking` binary/archive runs against
old kernel 40e268 and fixed dc25f4. Old fails iteration 229 with EAGAIN, sender
completion at +80.9 us, timeout at 1.000187 s and empty pending queue; fixed
passes 10,000 (`sigwait-blocking-{red,green}`). On the same physical board
under RockOS, three preserved yield runs and three blocking runs all pass
10,000 (`sigwait-rockos-oracle.log`), respectively 113–114 ms and 181–197 ms.
Therefore the Asterinas throughput anomaly is retained, not dismissed as a
Linux-wide expected result or declared fixed by the new test.

Quality review requested the completion semaphore's return/errno in failure
logs. This diagnostic-only refinement is the final source, SHA-256
`6f3bb6e18127a2b2e63a183db0456e44b062e80505188f9dba241fc76d35548c`.
Native 10,000 pass. The final identical-binary red/green differential again
fails on old kernel (iteration 66, EAGAIN, done=66, completion=0,
completion_error=0, send_error=0, send +80.9 us, wait 1.000082 s, pending=0)
and passes 10,000 on dc25f4 (`sigwait-blocking-final-{red,green}`). Scoped
quality re-review finds no remaining issues. Final x86 single-program QEMU
also passes (`sigwait-blocking-final-x86-green`).

### Integrated suite and a second finite-window physical setup failure

Before the final diagnostic-only errno addition, `stop-final-riscv-green`
passes all 17 programs, QEMU exit 0, 19.164 s; kernel dc25f4 and archive
`689072bbe7edcf1fda96e399d8d89f7aa837a772a2ebcf2530d29748cd4fc14b`.
`stop-final-x86-green` passes all 13 (adds sleep to the preceding 12), QEMU
exit 33, 20.620 s, kernel dfb295 and archive
`e7390af2cd1cd3a669809b6822aef4b330ab0394189a2a5fd0e781aa21f2b936`.
All other test sources and frozen binaries remain unchanged.

Fresh finite-model rerun `target/signal-job-control-model/run.T47CrfdE`
passes all **21 expected negative controls and eight corrected configurations**
(`stop-final-models.log`), with no download and the same pinned JAR. These
abstract protocol checks do not prove the throughput behavior above.

To fit the full suite, only the prior f70c74 archive and aa5b67 optional menu
were reference/hash checked, copied, synced and verified before unlink into
`/var/tmp/asterinas-stop-final-archive.WKJslGq7` (`stop-final-archive.log`).
The small b4ebc2 throughput diagnostic remains available in /boot. Installed
defaults remain unchanged; free space before publication is 20,256,768 bytes,
larger than the 14,865,408-byte new archive. Kernel dc25f4 is reused.

`stop-final-physical.log` is **16/17**, not passing. The blocking sigwait
completes all 10,000 in 14.671 s, maximum wait 74.253 ms, within the unchanged
15 s trigger. The time-namespace program fails its STOP handshake with normal
child exit status 0; all eight child sleep/futex results themselves pass.
Its last 400 ms futex expires while verbose USB/input initialization messages
are printed, before the parent obtains WSTOPPED. This is consistent with its
remaining guessed 50 ms pre-STOP delay/finite window; no direct request-time
measurement establishes the exact interleaving. Probe/recovery takes 58.779 s;
RockOS ID `fa389c71-6330-458e-9ec8-a66cdc99f865`, p2 fsck exit 0.

Next controlled comparison changes only the optional probe's console log
level from info to error, retaining the exact kernel, archive, all assertions
and `asterinas.klog_capture=info`. The ignored qualification helper accepts a
validated `PROBE_CONSOLE_LOGLEVEL=info|error|off` override; tracked boot tools,
installed default selectors and normal desktop configuration are unchanged.
The generated manifest records the changed optional selector. This is an
experimental condition, not proof that suppressing output repairs a kernel
or that verbose-mode timing tests are reliable.

### Two successful low-console physical qualifications

`stop-final-quiet-physical.log` passes **17/17**, including all 10,000 blocking
sigwait iterations (9.966 s), all eight time-namespace cases plus the STOP
handshake, the eight-case group-stop observer test, and the four-round real
handler workload. The kernel and 689072 archive are identical to the immediately
preceding verbose failure; only the optional probe console level is error,
while memory klog capture stays info. Probe plus fresh firmware recovery is
50.357 s. RockOS recovers with ID `49d3594d-2170-4f93-b90c-e2a6c0a585bf`,
and unmounted p2 fsck exits 0 (`stop-qualified-preflight.log`). This supports
console output as a timing confounder, not a proven causal mechanism or a
fix for the verbose-mode finite-window test.

The final source's diagnostic-only completion errno addition is also included
in a new complete 17-program QEMU archive: `stop-qualified-riscv-green`, all
zero markers, QEMU exit 0, 19.501 s, SHA-256
`a848faa26f60c08b123409ddd7df099013c78fa77969cfdc23b7f2b2ce0033ef`.
Final sigwait RISC-V binary `sigwait-blocking-final`:
`770fbc7300a42b1676b091f9531e5ae8c7486e0b3d19fec33380549e2490d0bf`;
native/x86 `sigwait-blocking-final.native`:
`0e75859e92b48a830ce1f8b2cec401248b0ccbb14b6f20c1def79c080be2ab37`.
The final x86 single-test qualification takes 0.841 s, QEMU exit 33,
archive `3d4a9283575111b3b39fbb00afc23e03ed1ad6c996ac2669b239128eab04b33b`.

The prior 689072 archive and both exact referencing optional menus (38eb11,
cc47a3) were copied, synced and verified before unlink into
`/var/tmp/asterinas-stop-qualified-archive.ernfozMj`. This preserves both the
verbose failure and first quiet pass conditions. All installed defaults remain
unchanged; 20,256,768 bytes were free before staging the final archive
(`stop-qualified-archive.log`). The dc25f4 kernel was again reused.

`stop-qualified-physical.log` passes **17/17** with this final a848fa archive
and error-console/info-capture configuration. Sigwait completes 10,000 in
7.965 s (maximum observed single wait 29.002 ms); probe plus firmware recovery
takes 47.929 s. These are two successful quiet-condition qualifications, not
two identical test archives: their only test-source difference is completion
errno diagnostics. The final hardware markers all have zero values, not merely
a count of 17 printed names. Final RockOS/storage postflight is recorded below.

Acceptance is deliberately scoped to the implemented signal protocols and
this qualified runtime condition. Verbose-console timing tests remain
unreliable, the preserved yield workload's throughput is markedly slower than
RockOS in some placements, and normal desktop shutdown supervision remains
unresolved. None is represented as a Firefox performance fix. No default boot
promotion, remote push, network integration, toolchain download or Docker
replacement occurred. Final scoped Rust formatting passes against all 43
changed/new Rust paths (`stop-qualified-rustfmt.log`); C formatting and
whitespace checks also pass. Full warning-free make check and modern ptrace
support are not claimed.

### Final board state and local commits

`stop-qualified-postflight.log` confirms RockOS 6.6.87, boot ID
`14362804-06a0-4b24-9536-e4e721757650`, idle bash on ttyS0 and unmounted p2.
Read-only `e2fsck -fn` exits 0 (19,505 files, 231,205 blocks used). SHA-256
checks of the installed default kernel 485b90, Asterinas selector 022807 and
vendor selector eb5f39 still match the preflight. Candidate kernel dc25f4,
final probe a848fa, unchanged normal desktop Stage1 9ef253 and DTB 465cb1 also
match. Available /boot space is **5,385,216 bytes**: any future new large
artifact requires a fresh capacity check and exact recoverable archival;
do not assume another whole image will fit. No experiment remains active.

Reviewed changes are saved as local commits on `codex/megrez-boot-main`:

- `2e343b048`: coordinated group stop, lifecycle/ptrace reporting, syscall
  restart/deadline behavior, pending-mask fix and logical proc stop state.
- `e98bff386`: bounded registered syscall/lifecycle/workload regressions,
  strict sleep setup classification and diagnostic blocking sigwait test.
- `d2a069aee`: finite protocol models, negative controls, runner and explicit
  model-to-implementation contracts/limitations.

This plan is committed separately as the evidence record. No merge, remote
push, worktree removal or default promotion is included. The full failed
experiments remain recorded above and in their frozen artifacts; successful
quiet-condition qualification does not retroactively turn them into passes.
