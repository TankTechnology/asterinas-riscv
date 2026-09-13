# Fair-yield selection model

This bounded TLA+ model checks immediate selection and runnable ownership
when a fair task yields before either ordinary preemption threshold is reached.
It accompanies the regression where `FairClassRq::update_current`
handled `Yield` accounting but did not request a handoff to a queued fair peer.

Run from the repository root:

```bash
bash tools/verification/fair_yield/run.sh
```

The runner requires host Java, `timeout`, `rg`, and `sha256sum`.
It reuses `target/signal-job-control-model/tla2tools-1.7.4.jar`
with SHA-256 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`.
Missing or mismatched artifacts fail the run; nothing is downloaded or installed.
Each case uses one TLC worker, a 256 MiB Java heap, and a 60-second timeout.
Logs, counterexample traces, Java version, and state counts go under
`target/fair-yield-model/run.*`.

## Code correspondence

- `Yield.shouldSwitch` represents the fair yield result in
  [`FairClassRq::update_current`](../../../kernel/src/sched/sched_class/fair.rs)
  together with higher-class lookahead in
  [`PerCpuClassRqSet::update_current`](../../../kernel/src/sched/sched_class/mod.rs).
- `Pick` represents `pick_next_entity`: Stop, then RT, then Fair, then Idle.
  A fixed fair ordering stands for one possible minimum-vruntime ordering,
  including the case where the yielding task would rank ahead of its peers.
- The atomic `Yield` action selects from the existing queue,
  replaces `current`, and requeues the old runnable task,
  following `try_pick_next`.
- `ExternalEnqueue` represents one enqueue of a previously sleeping task
  through `ClassScheduler::enqueue`.
  It shares the local-runqueue lock with
  [`yield_now`](../../../ostd/src/task/scheduler/mod.rs),
  so TLC explores its occurrence before and after the locked yield decision.

The population is three fair tasks, one Stop task, one RT task, and Idle.
`fair0` starts current; Idle starts queued;
all 16 queued/sleeping subsets of the other four tasks are initial states.
At most one yield and one external enqueue occur per execution.
An enqueue after the decision does not retroactively change that decision:
selection assertions refer to the queue snapshot at yield time.

## Checked properties and negative controls

`ImmediateFairHandoff` requires a queued fair peer to become current
when no higher-class task was queued at the decision.
`HigherPriorityWins` checks Stop and RT precedence.
`EmptyFairQueueKeepsCurrent` prevents switching to Idle
when the only non-idle runnable task is current.
`NoDuplicateOwner` excludes overlap between current, queued, and sleeping tasks.
`NoLostRunnable` compares their ownership against a separate runnable ledger.
`PopulationPreserved` checks the runnable/sleeping partition,
and `SelectedFromOldQueue` checks the origin of a replacement task.

The runner checks these deliberate regressions before the corrected model:

| Configuration | Required TLC failure (exit 12) |
| --- | --- |
| `IgnoreYield` | `Invariant ImmediateFairHandoff is violated.` |
| `ReenqueueBeforePick` | `Invariant ImmediateFairHandoff is violated.` |
| `LoseOld` | `Invariant NoLostRunnable is violated.` |

The first two counterexamples leave `fair0` current despite queued `fair1`.
The third selects `fair1` but loses runnable `fair0` from ownership.
Every case must report nonzero generated and distinct state counts;
syntax errors, timeouts, and unrelated failures cannot pass as counterexamples.
The corrected case must exhaust the state space with exit 0 and
`Model checking completed. No error has been found.`

## Assumptions and limits

The existing IRQ-disabled local-runqueue lock makes update, selection,
and replacement atomic relative to the modeled external enqueue.
This lock contract is assumed from the implementation;
the model does not prove the lock implementation or weak-memory behavior.
Task admission has already succeeded, and scheduling policies stay fixed.
Set-valued queue membership checks exclusivity across owners;
it does not model duplicate nodes inside a concrete queue representation.

Runtime accounting, changing vruntime/weights, timer thresholds,
remote preemption notification, migration, repeated yields, blocking,
and exit are outside this model.
There is no fairness assumption or eventual-scheduling claim:
the properties constrain the result when the modeled yield action executes.
This is exhaustive checking of the stated finite abstraction,
not a proof of the complete Rust scheduler or its refinement to the model.
