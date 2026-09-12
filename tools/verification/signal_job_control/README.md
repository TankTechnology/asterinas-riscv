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
The four negative controls run before the two corrected configurations.

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
`SignalJobControl.exiting`.
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

## Recorded results

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
