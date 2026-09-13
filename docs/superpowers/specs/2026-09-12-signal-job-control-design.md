# Signal job-control correctness design

Status: proposed design for review; no implementation or new physical test
is claimed by this document. Repository baseline: `ac23364b9`.

## Purpose and boundaries

Repair signal job control as a coherent protocol, not as Firefox-specific
exceptions. The immediate target is STOP/CONT cancellation, including a stop
already removed from a pending queue. The next gate covers stopped-thread
delivery and group-stop reporting. Only then qualify desktop shutdown.

Keep the safe-Rust kernel boundary, current boot modes, persistent build
container, installed Debian, shutdown timeout and default networking kernel.
Do not replace timeout waits with forced resets or assume the signal defect
accounts for all 107 seconds of physical recovery.

## References and what they establish

Linux v6.12 is a pinned implementation reference, not a claim about the latest
release. Inspect the complete relevant functions, including their callers and
lock preconditions, rather than translating isolated C fragments.

- [Linux signal.c](https://github.com/torvalds/linux/blob/v6.12/kernel/signal.c):
  `prepare_signal`, `__send_signal_locked`, `complete_signal`,
  `dequeue_signal`, `do_signal_stop`, and `ptrace_signal`. Generation performs
  STOP/CONT cancellation independently of disposition; dequeued stops have
  revocable eligibility. Delivery and group-stop participation are separate
  steps. This motivates a serialized transition protocol, not more wake calls.
- [Signal ownership](https://github.com/torvalds/linux/blob/v6.12/include/linux/sched/signal.h)
  and [job-control state](https://github.com/torvalds/linux/blob/v6.12/include/linux/sched/jobctl.h):
  shared dispositions, process pending signals and per-thread state have
  different ownership. Group stopping has participation state in addition to
  an already-stopped flag. Shared handler ownership must survive CLONE_SIGHAND.
- [signal(7)](https://man7.org/linux/man-pages/man7/signal.7.html):
  preserve process/thread targeting, per-thread masks, standard-signal
  coalescing, real-time queuing and handler semantics.
- [ptrace(2)](https://man7.org/linux/man-pages/man2/ptrace.2.html):
  signal-delivery stops and group stops are distinct. A signal may be replaced
  or suppressed by the tracer; ordinary SIGCONT must not release a ptrace stop.

OSTEP is the concurrency reasoning guide, not a specification of Linux signal
ABIs. Its condition-variable `signal()` is not a Unix signal. The linked
author-hosted chapters are individually versioned; do not label them all with
the version on the book landing page.

- [Condition Variables, chapter 30](https://pages.cs.wisc.edu/~remzi/OSTEP/threads-cv.pdf):
  retain a state predicate, coordinate the check with waiting, and recheck
  after waking. A notification alone is not remembered application state.
- [Common Concurrency Problems, chapter 32](https://pages.cs.wisc.edu/~remzi/OSTEP/threads-bugs.pdf):
  distinguish atomicity violations from ordering violations and lock cycles.
  Tests must exercise transitions between the check and its dependent action.
- [Locks, chapter 28, wakeup/waiting race](https://pages.cs.wisc.edu/~remzi/OSTEP/threads-locks.pdf):
  publishing a waiter and preserving an early wake are part of the protocol.
  Copying a locking pattern without its park/unpark guarantees is insufficient.

The design below is an application to our code, not an assertion that Linux
or OSTEP uses these Rust types or exact module boundaries.

## Local audit

| Concern | Current implementation | Design obligation |
| --- | --- | --- |
| Signal generation | `Process::enqueue_signal`, `PosixThread::enqueue_signal` | One implementation of process-wide generation effects |
| Pending storage | `signal/sig_queues.rs` | Explicit masked discard with correct count; no process policy in storage |
| Queue consumption | `signal/pending.rs` | Distinguish consumption from action preparation |
| Action execution | `signal/mod.rs` | Validate stop eligibility and commit under one synchronization boundary |
| Stop state/reporting | `process/status.rs` | Coherent state transitions and observable wait status |
| Stopped-thread loop | `thread/task.rs`, `signal/pause.rs` | Do not consume ordinary user-handler signals while stopped |
| Other consumers | `rt_sigaction`, `rt_sigtimedwait`, `signalfd` | Preserve their consumption semantics; no implicit stop action |
| Thread lifetime | `clone.rs`, `posix_thread/{builder,exit}.rs` | Membership changes and group-exit precedence cannot race transitions |
| Tracing | `posix_thread/ptrace/{mod,util}.rs` | Preserve origin and cancellation across tracer waits/replacement |
| Wait primitive | `ostd/src/sync/wait.rs` | Reuse persistent wake state and predicate rechecking |

Established: the blocked TSTP/CONT test passes Linux 8/8 and this kernel 0/8
in both send orders and all process/thread queue combinations. The latest
repeat took 0.429 seconds in diskless SMP=4 QEMU. See
`target/shutdown-latency/pending-confirmation/` and the
[preceding physical evidence](../../porting/evidence/2026-09-12-desktop-shutdown-latency.md).

Code-inspection findings, not yet new behavioral reproductions:

1. Queue removal and `Process::stop` are separated by disposition processing
   and potentially a ptrace wait. There is no revocable stop authorization.
2. The stopped-thread loop calls normal delivery after an interruptible wait;
   its own FIXME acknowledges user-handler signals should be deferred.
3. `rt_sigaction` holds disposition locks and the task-set lock while using
   the generic dequeue interface. Exit sends sibling SIGKILL while holding
   the task-set lock. Blindly adding a shared lock in enqueue/dequeue can
   deadlock these existing callers.
4. The current stop flag is set by one thread, with immediate parent wakeup.
   It does not track every sibling reaching a group-stop checkpoint.
5. OSTD's Waker already remembers early wakes using release/acquire operations;
   Pause publishes the waker before testing conditions. Do not replace that
   mechanism without a failing test against the actual wait path.

The five physical waiting processes are evidence of a termination delay,
not proof of which of these defects caused it. Logging/console differences
remain confounders in the single fast recovery.

## Chosen approach and alternatives

Recommend an explicit job-control coordinator alongside existing pending
storage, delivered in independently testable commits. Retain signal payloads,
mask/handler ownership, signal-frame construction and the scheduler.

Queue clearing alone is rejected: it cannot cancel an already-selected stop.
A wholesale Linux-style signal-subsystem replacement is also rejected for
this repair: it changes timers, tracing, handler sharing and ABI paths before
we have tests for their current behavior. The coordinator provides one owner
for the missing protocol without copying Linux structures or C locking idioms.

## State, operations and invariants

Use typed internal state and narrow methods in a job-control module. Avoid a
collection of unrelated public atomics or duplicated SIGCONT branches.

The coordinator owns group-stop transitions. Per-thread delivery state owns
a revocable stop permission and later group-stop participation. A permission
is not a signal number or a wake event. Prefer an explicit per-thread marker
under the coordinator's synchronization, rather than an unbounded global
sequence counter with unproved wraparound/ABA behavior. Private thread-state
storage can use a mutex for safe interior mutability; it is only accessed
after the coordinator guard, never as a separate authority.

Required operations are generation, selection for delivery, permission
invalidation, conditional stop commit, continuation, and exit cancellation.
They must make these invariants visible in the API:

1. CONT removes pending STOP/TSTP/TTIN/TTOU across shared and all member-thread
   queues and revokes selected-stop permissions, including when CONT itself
   is blocked, ignored, or already pending.
2. A generated stop-class signal removes pending CONT across those queues.
   This does not itself execute a default stop action.
3. Selection records stop eligibility before releasing the coordination
   boundary. Validation and the eventual stop-state commit share one lock
   acquisition. A CONT between selection and commit makes the old stop inert.
4. A later, newly selected STOP is still allowed to stop the process. Merely
   returning from a wait must not grant old cancelled work a new permission.
   Explicit ptrace requeue followed by fresh selection is a separate case.
5. Once generation-time continuation and cancellation are complete, the
   default CONT delivery action must not resume the process a second time.
   A user-installed CONT handler still executes according to mask/ABI rules.
6. An ordinary stopped thread waits for continuation or fatal termination;
   it must not drain regular handler signals merely to make the wait return.
7. Group exit dominates pending stop work. No delayed stop commit may restore
   a stopped state after exit cancellation, nor recreate a live parent/thread.
8. Queue count, pending bits and signal payloads remain consistent. Discarding
   job-control signals must not discard unrelated standard or real-time data.

Do not turn the generic dequeue used by signalfd/sigtimedwait into a default
action executor. Prepare delivery-specific state at the delivery boundary.
For tracing, establish cancellation tracking before releasing control to the
tracer, even if the tracer may later replace a non-stop signal with STOP.
Preserve ptrace requeue semantics explicitly: the Linux reference re-enters
`send_signal_locked` when the returned signal is blocked (or a fatal signal is
pending). Therefore, do not impose a blanket rule that requeue bypasses
generation effects. Test direct continuation of a selected stop separately
from requeue followed by a fresh dequeue, retaining the origin queue.

## Synchronization and side effects

Proposed hierarchy within this change:

`disposition locks (when required) -> task-set lock (when required) -> job-control coordinator -> individual queue/thread-state locks`

Acquire only the subset needed by an operation. In particular, ordinary
dequeue must not reacquire the task-set lock: rt_sigaction already owns it.
Sibling SIGKILL during exit must likewise use a path that does not reacquire
that lock. Enumerating members for a process-wide transition uses the stable
task set; ordinary queue consumption does not scan siblings.

Do not acquire disposition locks or the task-set lock from inside a
coordinator critical section. Keep queue guards disjoint where possible.
Review each added edge against tracing, timers, clone/exec and exit; this
proposed hierarchy is a design constraint, not a completed lock audit.

Separate transition commit from callbacks. Queue storage returns whether
notification is needed instead of invoking signalfd observers while an outer
coordinator lock is held. Drop signal locks before parent notifications,
cross-process signal sends, tracing waits, user-memory copies, or sleeping.
The committed state remains authoritative if a delayed wake becomes redundant.
Waking after unlocking is justified here by the existing persistent Waker and
post-registration predicate check, not by assuming condition variables retain
notifications. Test wake-before-park and wake-after-park against that real path.
Use weak-reference upgrades for remote lifetime races, retaining the prior fix.

Preserve CLONE_SIGHAND sharing. A per-process coordinator must not accidentally
replace the shared disposition object with private copies. No new global lock
should serialize unrelated processes.

## Completion stages and tests

### M1: pending and selected-signal cancellation

- Preserve the existing eight already-stopped CONT cases.
- Promote the current eight pending-cancellation cases into permanent tests;
  extend them to TSTP/TTIN/TTOU, ignored dispositions, repeated/coalesced CONT,
  and a signal queued to one sibling cancelled through another.
- Force selection -> CONT -> stop commit, then prove a fresh later STOP works.
  Use a private kernel state-machine test for the exact interleaving, and a
  userspace/ptrace test where supported. A stress loop alone is not proof.
- Test queue count/payload preservation, raw signal consumers, and SIGKILL
  precedence. A test that only models an unrelated algorithm is insufficient.

### M2: stopped execution and group reporting

- Reproduce and repair ordinary-signal consumption while stopped. Verify
  pending masks, no premature handler side effects, continuation and kill.
- Model running, stopping, stopped and exiting explicitly. Track participation
  so wait reporting does not claim that all siblings stopped when only one did.
  At stop initiation, enroll eligible live threads under stable membership.
  Each participant acknowledges at most once at its stop checkpoint. A joining
  thread is enrolled before publication and must check stop state before its
  first userspace entry. An exiting unacknowledged participant leaves the
  outstanding set once; acknowledged exit cannot decrement it again. CONT
  cancels outstanding participation as well as selected-stop permissions.
- Compare wait/CLD notification behavior against Linux, including interrupted
  group stops, WNOWAIT/WCONTINUED and notification coalescing. Do not invent
  an unlimited queue of child events or conflate ptrace and group-stop state.
- Run existing kill, signal, signalfd, timed-wait, job-control and available
  ptrace tests. Preserve process/handler inheritance across fork and exec.

M1 can land independently as a bounded compatibility improvement; it is not
full job-control or desktop-shutdown acceptance. Do not promote a build after
changing the stopped-thread filter without also testing group exit/wakeup.

### M3: shutdown causality and physical qualification

Use bounded diskless workloads resembling STOP -> TERM -> CONT with several
processes/threads and real handlers. Retain guest exit markers and deadlines;
QEMU exit zero alone is not acceptance. Keep failure cleanup bounded.

Reuse the existing signal provenance facility for an opt-in compact record
of sender/target, route, signal, transition, cancellation and remaining wait
reason. Avoid printing every syscall or changing timing in the baseline run.
Do not expose user memory or add a new ad-hoc public syscall for diagnostics.

After QEMU passes, repeat normal tty0 desktop recovery with unchanged timeout,
Debian and boot policy. Diagnose remaining waiters if 90-second escalation
persists. Require at least three consistent normal-configuration recoveries
before claiming a stable latency improvement. Separately verify Firefox's
local page, safe RockOS recovery and unmounted partition integrity.

## Code-quality and review gates

Use ordinary review and the repository's maintainability/concurrency rules;
the retired aster-code-review skill is not part of this workflow. Keep storage,
coordination, action execution and notification responsibilities separate.
Preparatory refactoring and semantic changes have separate commits. Public
methods must state their contract; private lock-dependent helpers should make
the guard requirement explicit. Do not add unsafe to kernel/.

Before implementation, review this specification. Before claiming a fix,
retain failing-baseline and repaired results, audit the lock graph, check
formatting and run nonzero-selected regressions. No kernel/userspace
implementation change, new build, or physical experiment was performed in
preparing this design.
