# Signal job-control protocol models

These finite TLA+ models check abstract signal-ordering and waiting protocols.
They are **not a proof of the Rust implementation, RISC-V memory ordering,
or the whole kernel**.
Reviewing the implementation against the operation contracts below,
and running kernel regressions, remain necessary.

## Run

From the repository root:

```sh
bash tools/verification/signal_job_control/run.sh
```

The runner requires Java 11 or newer, Bash, curl, ripgrep, GNU timeout,
and the SHA checksum utilities.
It downloads one 2.2 MiB JAR from the official
[TLA+ v1.7.4 release](https://github.com/tlaplus/tlaplus/releases/tag/v1.7.4)
if the cache is empty.
The release publishes SHA-1 `bee4a54f3ee3d4afc347c3240ec2d9e93b075104` for that JAR.
The runner verifies this checksum and additionally pins SHA-256
`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`,
including on cache hits.
It uses TLC 2.19, one worker, a 768 MiB Java heap, a fixed fingerprint polynomial
and seed, and a 120-second timeout per model invocation.
It installs no toolchain and needs no container, kernel build, or board.

The JAR, state databases, logs, counterexample traces, Java version,
and a tab-separated result summary go under the ignored directory
`target/signal-job-control-model/`.
Each invocation creates a fresh `run.*` directory and prints its path.
The runner accepts a negative control only if TLC returns the expected
invariant/liveness exit status, names the expected failed property,
and reports nonzero explored states.
Syntax failures and timeouts do not count as successful negative controls.
Each protocol's negative controls run before its corrected configuration.
There are twenty-one negative controls and eight corrected configurations.

## Job-control model

`JobControl.tla` has two threads, one shared pending queue, and one private
pending queue per thread.
There are at most three STOP/CONT generation events in a behavior.
Generation IDs are ghost history, bounded by `MaxGenerations = 3`;
they are not proposed implementation counters.
STOP abstracts the stop-signal family and its default stop disposition.
Each standard signal kind coalesces within its destination queue.
Each thread has at most one selected stop decision in flight.
Its permit abstracts `SelectedStop.valid`, while `exiting` abstracts
the M1 `SignalJobControl.exiting` field (now the terminal `Exiting` phase).
Stopped state is one process-wide Boolean, matching `ProcessStatus.stop_status`;
this model does not add thread-participation accounting.
There is also one monotonic pending-SIGKILL bit per queue and one terminal
process/group-exit commitment.
No state constraints prune the reachable graph inside these bounds.
The three-event bound includes STOP/CONT/STOP and two selected thread stops
followed by one CONT.

| Model operation | Implementation contract to review |
| --- | --- |
| `Generate` | Under the process coordinator, remove opposing pending signals from every shared/private queue and insert into the target queue. CONT also revokes all selected-stop permits and clears the process-wide stopped state before releasing the coordinator. |
| `Select` | Under the same coordinator, dequeue a shared or current-thread signal and mark a selected stop decision with a per-thread permit. Rust prepares a permit for every delivery selection because a tracer may subsequently substitute STOP. |
| `CommitStop` | Under that coordinator, check the selected permit, process exit state, and current-thread/shared pending SIGKILL before changing stopped state; consume the selected decision and permit. |
| `EnqueueKill` | Record SIGKILL in its destination queue. This action does not commit group exit. A sibling's privately targeted SIGKILL does not itself veto another thread's stop. |
| `CommitExit` | Mark actual process/group exit committed under the coordinator, preventing subsequent stop commitment. Existing stopped state and selected permits can remain; the exit check must still reject them. This abstracts the terminal transition after fatal-signal delivery or another group-exit path. |

Generation, selection, stop commitment, and exit commitment are separate atomic
actions, so TLC explores all their interleavings.
Atomicity is a contract of this abstraction, not something TLC establishes
about the code's locking or atomics.

`lastStop` and `lastCont` independently record generation history.
`PendingMatchesLatestGeneration` requires every pending event to be newer
than the most recent opposing generation, even when the opposing pending
signal has already been consumed.
`NoStaleStopCommit` observes whether an accepted stop was selected from an
event predating the latest CONT.
Neither history value grants or revokes implementation permissions:
the commit action consults only its permit and fatal-state checks.
This separation prevents the history oracle from silently repairing the
implementation when selected-permit invalidation is disabled.
`NoStopAfterFatal` observes accepted stop commitments in the presence of
current/shared SIGKILL or committed exit.
`TypeOK` checks state domains.
Exit dominance means that no later stop is committed, not that marking exit
necessarily clears the preexisting stopped Boolean.

This model checks safety without fairness assumptions.
It makes no claim that pending SIGKILL is eventually delivered,
that stop participation spans every thread, or that fatal-signal generation
implements all Linux eager side effects.
Exit may be committed after any interleaving; it is not inferred from enqueue.

The model omits masks, disposition changes, raw signal consumers, ptrace
attachment, signal substitution, and blocked requeue;
those Rust paths need separate code review and regression tests. It does not
dequeue SIGKILL, so its monotonic pending-KILL abstraction does not establish
correctness for kill-consumption races. `Select` is disabled while stopped in
the model, whereas M1 deliberately retains the kernel's existing stopped-thread
delivery loop. The model therefore does not validate that loop's filtering or
full Linux group-stop participation/reporting semantics.

## Wait/wake model

`WaitWake.tla` models two waiters in one stop/resume episode.
Each waiter registers before reading the stopped predicate, then parks if
the predicate was true.
A remembered wake token prevents an early callback from being lost between
the check and the park.
The resume operation clears the predicate separately from the per-waiter
callbacks, permitting callbacks to execute outside the process coordinator.
A wake of a parked waiter returns it to a predicate check.
Spurious wakes while still stopped exercise this recheck rather than allowing
an unconditional return from the wait.

| Model operation | Implementation contract to review |
| --- | --- |
| `Register` | Make the waiter discoverable before checking the condition. |
| `Check` | Check stopped state after registration and after every wake; finish only when it is false. |
| `Park` | Atomically consume an existing wake token or become parked, closing the check-to-park race. |
| `Resume` | Clear the stop predicate before delivering wake callbacks. |
| `Notify` | Wake an already parked waiter, or remember the wake if it has registered but not yet parked. |
| `SpuriousWake` | Allow an unrelated wake; the waiter must still recheck the predicate. |

`NoLostWake` forbids a parked waiter after resume and completion of its callback.
`DoneRequiresResume` forbids finishing while the stopped predicate is true.
`EventuallyDone` requires both waiters eventually to finish.
Weak fairness is assumed separately for `Resume` and each waiter's register,
check, park, and notify actions: a continuously enabled action eventually runs.
Spurious wakes need no fairness assumption.
Fairness cannot fix the broken early-wake case: after the callback is lost and
the waiter parks, no action capable of waking it remains enabled.

The model does not include an unbounded sequence of STOP/CONT episodes,
waiter destruction/reuse, allocation failures, scheduler internals, or Rust
memory visibility.
Deadlock checking is disabled because terminal states permit stuttering;
the explicit liveness property still detects a permanently parked waiter.

## Group-stop participation model

`GroupStop.tla` extends the participation contract beyond the M1 signal model.
It has three possible threads, two initially published members,
one possible joining thread, and at most two stop episodes.
Thread IDs are never reused after exit.
Each episode permits one CONT, so the graph includes STOP/CONT/STOP
and CONT before all participants have acknowledged.
No state constraints prune the reachable graph inside these bounds.
The process phases are `Running`, `Stopping`, `Stopped`, and terminal `Exiting`.
Each thread has an idle, pending, or acknowledged participation marker.
A separate counted bit distinguishes a pending participant in `Stopping`
from a thread that joins an already completed `Stopped` episode.

| Model operation | Implementation contract to review |
| --- | --- |
| `StartStop` | With stable membership and the coordinator held, enroll every live member as pending and initialize outstanding participation before exposing `Stopping`. |
| `Join` | Hold membership and coordinator protection together; enroll a new thread before publishing it. In `Stopping`, increment outstanding participation. In `Stopped`, require its checkpoint/parking without reopening the completed report or incrementing outstanding participation. |
| `Checkpoint` | At the actual stop checkpoint, read and consume the current thread's pending marker under the coordinator. A counted marker consumes exactly one outstanding participation. An already acknowledged thread cannot acknowledge again. |
| `Leave` | Remove membership once under the membership/coordinator lock order. A pending counted participant consumes one outstanding participation; an acknowledged participant does not decrement again. |
| `ReportStopped` | Publish the completed stop only when outstanding participation is zero, under the coordinator. |
| `Continue` | Under the coordinator, return to `Running`, clear all participation markers and outstanding participation, and cancel the old episode. Selected-signal permit invalidation is checked separately by `JobControl.tla`. |
| `CheckUserAdmission` | At the final stop check admitting user execution, require the thread's participation marker to be clear. A pending member must acknowledge and wait; acknowledgment alone does not permit admission before CONT. This check is distinct from architectural userspace entry. |
| `CommitGroupExit` | Under the coordinator, enter terminal `Exiting` and clear participation. No later stop report is possible. |

The corrected checkpoint has no saved ACK token or implementation epoch counter.
A call that started before CONT and a later STOP may acknowledge the current
episode, provided it reads the current marker at the actual checkpoint
under the coordinator.
`PrepareSavedAck` and `ApplySavedAck` exist only in the stale-ACK negative control.
They represent incorrectly saving a decrement before acquiring the coordinator
and applying it to a later episode after CONT.

`required`, `acknowledged`, and `departed` independently record finite
per-episode membership, actual checkpoint, and departure history.
They do not authorize a checkpoint, change its marker, or repair its counter.
`NoPrematureReport` observes that every required live participant has
acknowledged at the point of the completed report.
`OutstandingMatchesPending` compares the counter with counted pending members;
`EveryMemberEnrolled` checks publication;
`AcknowledgeAtMostOnce` and `NoStaleAcknowledgment` check accepted checkpoints.
`stopObligations` independently records each live member's obligation
from stop initiation or joining an active stop until CONT or exit.
Checkpoints and reports leave these obligations intact.
`NoUserEscape` checks every `CheckUserAdmission` transition against this history,
without consulting acknowledgment markers, outstanding counts, or reports.
It excludes admission by both pending and acknowledged members during `Stopping`
as well as `Stopped`.
A later join may be pending in `Stopped`, but cannot pass this admission check.
The operation maps to the final stop-predicate check that admits user execution
in `kernel/src/thread/task.rs`, before `user_mode.execute`.
It does not combine that check and architectural userspace entry atomically.
STOP may arrive after a successful admission check but before actual entry;
that still-pending thread may enter userspace before its next checkpoint.
A thread already running in userspace when STOP arrives may continue there
until it reaches an actual checkpoint; that execution is abstracted by
stuttering and is not a `CheckUserAdmission` transition.
The model therefore does not exclude such pending userspace execution
or claim immediate preemption of all members.
An acknowledged member remains obligated until CONT or exit,
and the observer still rejects granting it admission during that interval.
`NoStopReportAfterExit` checks report exclusion and terminal exit dominance.

These are safety checks without fairness or eventual-stop claims.
For additional interleavings, the model separates the last participation
consumption from report publication; concrete code may combine them in one
coordinator critical section.
Atomic membership publication, marker consumption, and status updates remain
implementation contracts, not a proof of Rust locking or memory visibility.
The models are checked independently, not as a composed refinement.
The group model omits signal queue selection, ptrace, masks, dispositions,
scheduler internals, allocation failures, and unbounded thread creation.
Its report event describes completed wait-state publication;
it does not establish wait-state coalescing behavior or ordering of SIGCHLD
callbacks delivered outside the coordinator.
In particular, CONT during `Stopping` and the associated notification choice
still require implementation review and regression tests.

On 2026-09-12, the extended runner completed with OpenJDK 11.0.31
and the pinned TLC version.
Artifacts are in `target/signal-job-control-model/run.981Xay54/`.
All preexisting configurations also retained their expected results.

| Configuration | TLC exit | Generated | Distinct | Result |
| --- | ---: | ---: | ---: | --- |
| `PrematureGroupReport` | 12 | 9 | 7 | `NoPrematureReport` fails |
| `MissingJoinEnrollment` | 12 | 15 | 13 | `EveryMemberEnrolled` fails |
| `DoubleExitDecrement` | 12 | 43 | 29 | `OutstandingMatchesPending` fails |
| `StaleGroupAck` | 12 | 819 | 556 | `NoStaleAcknowledgment` fails |
| `PostAckUserspace` | 12 | 44 | 30 | `NoUserEscape` fails |
| `GroupStop` | 0 | 59,705 | 35,805 | All invariants pass; graph depth 18; zero queued states |

The retained counterexamples show a report immediately after initiation,
an unenrolled published join, a checkpoint followed by a second decrement
on that thread's exit, a saved ACK applied after STOP/CONT/STOP,
and an acknowledged thread wrongly receiving userspace admission before CONT.
The last fault is named `PostAckUserspace`; it changes the admission guard,
not the abstraction's treatment of architectural entry.
These results establish sensitivity to those faults and finite exhaustion
within the stated bounds; they are not an unbounded proof.

## Timed futex restart model

`FutexRestart.tla` checks one timed futex syscall,
with at most three wait attempts, two STOP/CONT episodes,
two matching wake calls, and two other interrupt events.
Each attempt may instead finish its pause through an arbitrary spurious wake,
independently of the matching-wake and interrupt counts.
The host clock advances nondeterministically from 0 through 4;
the original absolute deadline is 2.
One futex word starts at the expected value 0 and may change once to 1.
No state constraints prune the graph inside these bounds.
Attempt IDs and successful-removal history are model observers,
not proposed Rust counters.

| Model operation | Implementation contract to review |
| --- | --- |
| `Enqueue` | Read the futex word and conditionally enqueue under the same bucket lock; a changed word returns `EAGAIN`. An expired deadline can still enqueue before the timeout path runs. |
| `Wake` | A matching waker removes a queued item under the bucket lock. Wake calls without a queued item have no effect on a later attempt. |
| `Pause` | The pause may report interruption, deadline expiry, or raw success from either a matching or arbitrary spurious wake. A competing futex wake can precede or follow this result, before cleanup obtains the bucket lock. |
| `Cleanup` | Remove the item under the bucket lock. An absent item means the futex waker already removed it and overrides any pause result with success. Otherwise, return the pause error or normalize raw success to `EINTR`. |
| `Stop` / `Continue` | A stop obligation interrupts waiting and blocks signal delivery/replay until CONT. In-flight pause and cleanup can finish before actual parking. |
| `Deliver` / `ReturnInterrupted` | Resolve whether signal delivery selected a caught handler. For the timed restart-block path, a caught handler exposes `EINTR`, without replay. |
| `Restart` | Replay only without a caught handler, using the originally saved host absolute deadline. The next attempt rereads the futex word under the bucket lock. |
| `Tick` / `ChangeWord` | Let host time advance, including during stop, and let another thread update the futex word independently of its wake call. |

`DeadlinePreserved` compares the active deadline to the original syscall input.
The deadline-reset negative control incorrectly adds the original duration
to the time at replay.
`NoLostSuccessfulWake` checks resolved attempts against `wakeHistory`,
which records actual removals by `Wake` only.
Cancellation does not enter this history.
`NoInventedSuccessfulWake` checks the reverse implication.
The spurious-success negative control omits normalization of a raw successful
pause while the item is still queued and violates this invariant.
`Pause("spurious")` produces the same raw result as a matching wake;
cleanup cannot inspect the wake's origin to choose its response.
The cleanup implementation consults only bucket membership and its pause result;
it never consults this history to choose its return value.
`NoReplayAfterCaughtHandler` audits replay decisions,
and `QueueOnlyDuringWait` checks queue ownership.
`TypeOK` checks the finite state domains.

This model deliberately permits a wake while off the queue to be forgotten.
For example, cancellation, STOP, an unchanged-word wake, CONT, and reenqueue
can leave the caller waiting until its original timeout.
Futex wake calls are not stored for future waiters.
Changing the word instead causes the next attempt's locked read to return
`EAGAIN`; the model does not claim that every wake across STOP/CONT succeeds.

These are safety checks with no fairness or eventual-return assertion.
Time may stutter, and reaching the attempt bound disables further replay.
The model assumes atomic bucket operations and a correctly normalized host
deadline; it does not prove Rust locking, memory ordering, timer/scheduler
progress, clock conversion, or architecture-specific restart registers.
The futex address, expected value, matching bitset, visibility, and clock choice
are fixed abstractions; preservation of all saved Rust fields needs code review.
Untimed `ERESTARTSYS`/`SA_RESTART`, signal masks, ptrace, group participation,
futex requeue, and page faults are outside this model.
The model is checked independently of the other three signal models.
Its `done` phase denotes a resolved syscall, not permission to enter userspace
while a stop obligation remains.

On 2026-09-13, the full runner succeeded with OpenJDK 11.0.31
and the pinned TLC version.
Artifacts are in `target/signal-job-control-model/run.8crgvbCP/`.
All three futex negative controls ran before the corrected configuration.
The initial two negative controls were also checked before adding the corrected
configuration, in `target/signal-job-control-model/futex-red.Lofrhn66/`.
All preexisting configurations retained their expected results.

| Configuration | TLC exit | Generated | Distinct | Result |
| --- | ---: | ---: | ---: | --- |
| `ResetFutexDeadline` | 12 | 2,047 | 806 | `DeadlinePreserved` fails |
| `LostFutexWake` | 12 | 197 | 109 | `NoLostSuccessfulWake` fails |
| `SpuriousFutexSuccess` | 12 | 75 | 51 | `NoInventedSuccessfulWake` fails |
| `FutexRestart` | 0 | 119,928 | 32,415 | All invariants pass; graph depth 29; zero queued states |

The retained counterexamples show a spurious pause normalized to interruption
followed by replay at host time 1 changing the deadline from 2 to 3,
a real futex wake wrongly normalized to interruption when its dequeue is ignored,
and a spurious pause incorrectly returned as successful futex completion
without any matching waker removal.
These results establish fault sensitivity and finite exhaustion
under the stated operation contracts, not a proof of the Rust implementation.

## Recorded M1 results

On 2026-09-12, with OpenJDK 11.0.31 and the pinned TLC version,
the complete runner succeeded.
Artifacts from that run are in `target/signal-job-control-model/run.W2izZSUL/`.
The corrected models exhausted their reachable finite state spaces,
with zero states remaining on the queue.

| Configuration | TLC exit | Generated | Distinct | Result |
| --- | ---: | ---: | ---: | --- |
| `NoPendingCancellation` | 12 | 13 | 13 | `PendingMatchesLatestGeneration` fails |
| `NoSelectedRevocation` | 12 | 1,062 | 532 | `NoStaleStopCommit` fails |
| `NoRememberedWake` | 12 | 109 | 60 | `NoLostWake` fails |
| `NoRememberedWakeLiveness` | 13 | 412 | 169 | `EventuallyDone` fails |
| `JobControl` | 0 | 21,343 | 7,407 | All invariants pass; graph depth 12 |
| `WaitWake` | 0 | 466 | 194 | All invariants and liveness pass |

The `.trace.txt` files retain the full state sequences for each negative control.
The shortest pending-cancellation counterexample generates STOP then CONT
while leaving the STOP pending.
The selected-stop counterexample generates STOP, selects it, generates CONT,
then wrongly commits that selected stop.
The lost-wake counterexample registers, checks a true predicate, resumes,
delivers the callback too early, then parks forever.
The separate liveness run disables only the lost-wake invariant so TLC can
exhibit the infinite stuttering suffix with one waiter still parked.

These results establish sensitivity to the named regressions and absence of
counterexamples within the stated abstract bounds.
They do not constitute an inductive proof for arbitrary threads/signals,
a refinement proof, or evidence about hardware execution.

## Traditional ptrace and group-stop model

`PtraceGroupStop.tla` checks the intended traditional ptrace/group-stop
operation contract independently of the preceding four models.
The extended runner has fifteen negative controls and five corrected models;
the earlier totals above describe the stages before this addition.
This model is part of the full ptrace/group-stop work, not proof that the
corresponding Rust hooks have been implemented or wired correctly.

There are two initially live threads, no joins or thread-ID reuse,
at most two STOP initiations, and at most one attachment per thread.
Either thread may detach or leave; tracer exit removes all remaining tracing.
There is no clock, step limit, fairness assumption, or state constraint.
Repeated signal-delivery stops and tracer resumes may cycle within the finite
graph, including after the second STOP.
STOP represents one abstract stop signal;
the model does not check preservation of the original group-stop signal number
when a tracer subsequently injects a different stop signal.

The process has separate active-stop, completed-report, and terminal-exit bits.
Participation is `Inactive`, `Pending`, `Parked`, or `PtraceControlled`.
The counted set is independent of the marker and completed latch:
a completed group can acquire new counted participants,
and an uncounted pending thread can coexist with counted pending siblings.
A separate ptrace-stopped set blocks execution until the tracer releases it.
The contract's repeated-STOP behavior follows `do_signal_stop` and
`task_participate_group_stop` in
[Linux v6.12 signal.c](https://github.com/torvalds/linux/blob/v6.12/kernel/signal.c#L2287):
already parked threads stay parked,
other members can participate again after tracer resume,
and the existing completion latch prevents a duplicate parent stop report.

| Model operation | Intended implementation contract to review |
| --- | --- |
| `StartStop` | Under membership/coordinator protection, enroll all live non-parked members, including members already in a ptrace stop. Initialize counted participation independently of the existing completion latch. |
| `SignalDeliveryStop` | Publish a signal-delivery ptrace stop without acknowledging group participation. A pending group marker must survive this stop and its return. |
| `Checkpoint` | Consume the current pending counted obligation under the coordinator. For an untraced member, park it. For a traced member, hold tracee state before the coordinator across consumption, the transition to `PtraceControlled`, and ptrace-stop publication. |
| `ReportStopped` | Publish at zero outstanding participation only if this active interval has not already reported completion. The model separates this action from the last consumption to explore additional interleavings. |
| `TracerContinue` / `ReturnFromTraceStop` | Clear only the ptrace stop. The returning stop call does not write participation: SIGCONT and another STOP may have installed a new pending obligation while it was suspended. |
| `Continue` | Clear group obligations, counted participation, and completion, while retaining every ptrace stop. SIGCONT can arrive before group completion or without any active group stop, including during a delivery-only ptrace stop. |
| `Attach` | Turn an already parked member into an uncounted pending checkpoint without reopening the completed parent report. Delivery of the attach signal is abstracted separately. |
| `Detach` / `TracerExit` | Under tracee-state then coordinator protection, remove tracing and its stop. Preserve any existing pending counted marker; restore an uncounted pending obligation for a controlled member if the group is still active. |
| `Leave` / `CommitGroupExit` | Consume a departing member's outstanding counted obligation once. Terminal group exit clears obligations and excludes every later stop action or report. |
| `CheckUserAdmission` | Require no ptrace stop, no outstanding stop-call return, and no pending/parked group obligation. A tracer-resumed controlled thread can be admitted while the process completion latch remains set. |

Review these contracts against `kernel/src/process/signal/job_control.rs`,
`kernel/src/process/posix_thread/ptrace/`, the signal-delivery paths,
and the final admission check in `kernel/src/thread/task.rs`.
These are intended correspondence points, not a checked refinement mapping.

`history.park` records a live thread's obligation to reach or remain at a group
checkpoint until SIGCONT, exit, or transfer of control to a ptrace group-stop.
Detach/tracer exit restores that obligation when the group remains active.
`history.traceHold` independently records published ptrace stops and releases
authorized by the tracer, detach, or exit; SIGCONT never releases it.
`history.needsAck` records enrollment and actual checkpoint/departure events.
The oracles never authorize a corrected action or repair its markers/counters.
`NoLostGroupObligation`, `PtraceStopOwnedByTracer`,
and `OutstandingMatchesPending` compare implementation state to those histories.
`NoUserEscape` independently audits every successful admission against both
hold histories.
`NoPrematureOrDuplicateReport` observes outstanding acknowledgments and earlier
reports, while `CompletedLatchPreserved` checks the remembered completion.
`TypeOK` and `NoStopAfterExit` check domains and terminal exit dominance.

The three negative controls respectively overwrite participation on an old
ptrace-stop return, let SIGCONT clear a ptrace stop, and discard a detached
controlled thread's group-park obligation.
Fault injection for the first case is restricted to a return spanning SIGCONT
using `history.cancelledReturn`, so its counterexample contains SIGCONT
followed by a new STOP; corrected participation updates never consult this
observer. The graph also includes STOP/CONT/STOP.
Detach and tracer exit intentionally share the same abstract transition,
so the retained counterexample may select tracer exit to expose a detach fault.

The abstraction assumes atomic operation contracts and fixed membership during
each transition; it does not establish Rust locking, memory ordering, or
scheduler progress.
No eventual-stop, eventual-resume, or starvation-freedom property is asserted.
Terminal states permit stuttering, and deadlock checking is disabled.
SIGCONT is enabled in every non-exiting state and may leave it unchanged;
this is not evidence of progress or absence of a lost-wake deadlock.
Admission is a predicate check before architectural user entry;
a later STOP can still arrive between that check and actual execution.
The model omits queue selection, signal substitution and masks,
wait-status consumption/coalescing, SIGCHLD callback ordering,
attachment permissions, repeated attachment to one thread,
new-thread publication, and seized tracing (`PTRACE_SEIZE`/`PTRACE_LISTEN`).
The other models and runtime regressions cover separate contracts;
their conjunction has not been checked as a composed model.

On 2026-09-13, the complete extended runner passed with OpenJDK 11.0.31
and the pinned TLC version.
Artifacts are in `target/signal-job-control-model/run.WA7ckyQ7/`.
Every preexisting configuration retained its expected result.
The new corrected invocation finished in under one second as reported by TLC.

| Configuration | TLC exit | Generated | Distinct | Result |
| --- | ---: | ---: | ---: | --- |
| `StalePtraceResume` | 12 | 2,325 | 631 | `NoLostGroupObligation` fails; depth 7 |
| `SigcontReleasesPtrace` | 12 | 119 | 61 | `PtraceStopOwnedByTracer` fails; depth 4 |
| `DetachLosesGroupPark` | 12 | 252 | 114 | `NoLostGroupObligation` fails; depth 5 |
| `PtraceGroupStop` | 0 | 13,025 | 1,735 | All invariants pass; graph depth 14; zero queued states |

The stale-return trace attaches a thread before any group stop,
enters a delivery stop, generates SIGCONT, removes tracing,
starts a group stop through the other member,
then lets the old stop call overwrite its new pending obligation.
The other traces expose SIGCONT releasing a delivery-only ptrace stop
when no group stop has ever been active,
and tracing removal losing the park obligation after a group checkpoint.
These are finite protocol checks with three demonstrated fault sensitivities,
not an unbounded proof or proof of the implementation.

### Runtime integration checkpoint

The 2026-09-13 traditional ptrace integration now implements the corresponding
counted `Pending { counted, signum }`, parked (`Acknowledged`) and
`PtraceControlled` transitions. `TraceeStatus::group_stop` holds tracee state,
the coordinator and participation across ACK and ptrace publication;
`finish_ptrace_stop` never rewrites participation. Attach and detach/tracer-exit
use the same outer lock order. Thread-effective predicates govern task entry,
signal selection and ordinary waits. `Process::stop_if_selected` checks the
model's initiating-participant guard, permitting a tracer-resumed member to
restart an incomplete round but retaining an already pending obligation.

Ordinary review found both a previously rejected incomplete-round restart and
the missing initiator guard. The former has a failing-then-passing exact kernel
test. A dedicated multithreaded runtime oracle for the latter is still pending.
Seven native/QEMU ptrace regressions additionally cover group versus delivery
stops, tracer continuation, SIGCONT, repeated stops, attach, detach, tracer exit
and same-parent SIGCHLD deduplication. They complement this model but do not
prove its operations refine the Rust code. Queued SIGKILL priority is checked
under the implementation coordinator, but pending-KILL races are not modeled
here. Parent/tracer notification claims, namespaces, stop signal numbers and
modern tracing remain outside this model as documented above.

Independent full-run artifacts after integration:
`target/signal-job-control-model/run.6w7srjHL/`. All twenty configurations retain
their expected results; the ptrace corrected graph and bounds are unchanged.

## Parent/tracer wait-report model

`WaitReports.tla` explores one completed group stop followed by one SIGCONT,
with distinct real-parent and tracer consumers. Their STOP slots are separate;
the process-wide CONT slot is shared. CONT replaces the parent's pending STOP,
but does not consume the tracer's pending stop. Queries may use `WNOWAIT`.

`StopSlotsIndependent` checks that only a consuming wait by the owner removes
its STOP slot. `ContinueSlotPreserved` checks that peeking cannot clear CONT,
and `SingleContinueConsumer` checks that the two readers cannot both consume
that one event. Independent observer sets record successful consuming returns;
corrected actions never use those sets as authorization.

| Model operation | Implementation contract |
| --- | --- |
| `PublishStop` | Completed group status and the tracer's private ptrace-stop become available. This abstraction collapses their publication; it does not prove callback ordering. |
| `PublishContinue` | `StopStatus::resume` replaces the process wait slot under its lock; it leaves the tracer's private stop untouched. |
| `WaitStop` | `wait_child_group_status` reads the parent's slot, while `wait_ptrace_stopped` reads the tracee slot. Neither consumes the other's STOP. |
| `PeekContinue` / `TakeContinue` | Both wait paths call `StopStatus::wait` on the same process slot. Check and clear hold one lock; `WNOWAIT` leaves the slot intact. |

Three fault configurations demonstrate sensitivity: `CrossConsumeStop` lets
the tracer erase the parent's stop, `SplitContinue` separates checking and
clearing so both readers can consume one continuation, and `ConsumeWaitPeek`
incorrectly consumes on `WNOWAIT`.

The full runner passed on 2026-09-13 in
`target/signal-job-control-model/run.bIL8pSSG/`, with all eighteen expected
negative controls and all six corrected models. The new corrected graph has
38 generated / 17 distinct states and zero queued states. The three negative
configurations respectively explore 6/5, 71/31 and 5/5 generated/distinct
states and terminate with the intended invariant violations (exit 12).

This deliberately small safety model does **not** prove waiter wakeups,
fairness, same-parent suppression, concurrent attach/detach, exit ownership,
event identity, repeated/coalescing event generations, or Rust/weak-memory
refinement. Deadlock checking is disabled. The corresponding C regression
checks both parent/tracer consumption orders, WNOWAIT, shared CONT consumption,
exit ownership and nonleader wait identity separately. These runtime tests
and the six protocol models are not a composed proof of the subsystem.

## Synchronous signal-wait preservation

`SigwaitPending.tla` models one originally blocked, default-ignored SIGCHLD
requested by `sigtimedwait`. It separates the explicit dequeue condition,
generic cancellation probe and remembered wake. It checks conservation of the
sent signal: it must remain pending until explicitly received. It does not
equate the presence of a remembered wake with preservation of the payload.

| Model operation | Implementation contract |
| --- | --- |
| `Send` | `enqueue_signal_for_thread` publishes the signal, then unconditionally wakes a registered waiter. The model collapses these into one action. |
| `Check` | `dequeue_signal_with_checking_ignore` uses the requested set and the original mask, allowing blocked SIGCHLD to be received despite its default ignore disposition. |
| `Cancel` | `PauseReason::Sleep` calls `has_pending`, which may discard ignored, unblocked signals. Retaining the original mask protects the requested blocked signal. |
| `Wake` | The waiter consumes its remembered wake and retries the condition. A wake alone cannot recover a discarded signal. |

The negative control `UnblockWaitedSignal` represents the preceding temporary
unblocking in `rt_sigtimedwait`: an empty `Check`, then `Send`, then `Cancel`
loses SIGCHLD without returning it. TLC reports `WaitedSignalPreserved` violated
after four states (7 generated / 7 distinct). The corrected configuration
retains the mask and explores 10 generated / 9 distinct states, queue empty.
Full-run evidence: `target/signal-job-control-model/run.M9uMaCyK/`; all nineteen
negative controls and seven corrected configurations have their expected
results. The cached JAR was reused with host Java, without installing Java in
the development container.

The registered C test `signal/sigtimedwait_race.c` complements this safety model
with 10,000 real inter-thread sends and synchronous waits, retaining the
signal mask and bounding total execution. The previous kernel fails before
completion with EAGAIN and an empty pending set after a successful send;
the preserve-mask candidate passes. This probabilistic scheduling experiment
does not force every modeled interleaving. The model has one sender, one
signal and no explicit timeout, fairness, unrelated-signal interruption,
STOP/CONT, disposition change, mask change or weak-memory behavior. It is
neither a liveness proof nor a refinement proof of the kernel. It also does
not establish the cause of a particular desktop shutdown delay.

## Child-state notification and concurrent disposition changes

`ChildNotification.tla` models one committed child STOP/CONT event and up to
two changes to the parent's notification policy. `suppressed` represents
explicit SIG_IGN or SA_NOCLDSTOP, independently of handler kind. The kernel
stores this metadata in one `SigAction` protected by the disposition mutex.

| Model action | Implementation correspondence |
| --- | --- |
| `ChangeDisposition` | `rt_sigaction` updates the action under the disposition mutex. |
| Atomic `Check` | `Process::notify_child_state` checks that action and decides whether to enqueue while retaining the same disposition lock; enqueue also takes the signal coordinator. |
| `CommitAfterUnlock` | Negative control: save a policy decision, drop the disposition lock, then enqueue after the policy can change. |
| `WakeWaiter` | After releasing the locks, unconditionally call `children_wait_queue.wake_all()`, even when SIGCHLD was suppressed. |

The first invariant requires the enqueue decision to match the policy at
its commit point, not a policy observed before an unlocked gap. The second
requires notification completion to preserve the independent wait wake.
Policy changes after commit can interleave with the separate wake action and
do not retroactively change the recorded enqueue decision.

Fresh full run `target/signal-job-control-model/run.QTTkDY0h/` reports:

| Configuration | Exit | Generated / distinct | Result |
| --- | --- | --- | --- |
| `SplitDispositionCheck` | 12 | 19 / 18 | `DispositionMatchesCommit` fails. |
| `SuppressWaitWake` | 12 | 14 / 14 | `SuppressionPreservesWaitWake` fails. |
| `ChildNotification` | 0 | 28 / 26 | Both invariants hold; queue empty. |

All twenty-one negative controls and eight corrected models meet their
expected outcomes. The earlier `disposition-models.log` run folded wake into
commit; `disposition-models-split-wake.log` is the current, separate-wake model.
The native/QEMU `group_stop_disposition.c` test complements the model with
actual sigaction query, wait status, and synchronous SIGCHLD observations.

Limits: `emitted` is an enqueue decision, not a promise of a new queue entry
(standard signals can coalesce). The child wait status is assumed committed
before this model starts. It does not model metadata encoding, delivery or
discard of an already queued signal, competing waiters, parent exit, ptrace
attachment, actual waiter registration, scheduler fairness, or weak memory.
No eventual-wakeup or Rust-refinement proof is claimed. In particular, this
model cannot establish that the Rust mutexes are held correctly; that mapping
requires source inspection and independent review.
