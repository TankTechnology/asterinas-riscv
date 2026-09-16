# Per-thread scheduler accounting model

This bounded TLA+ model checks the timestamp and cumulative-counter contract used by
the Asterinas per-thread `schedstat` implementation. It separates an implementation
ledger from a specification ledger and explores two tasks, two CPUs, clock values
from zero through four, and every transition sequence of up to six steps.

Run from the repository root:

```bash
bash tools/verification/sched_info/run.sh
```

The runner reuses the repository's cached TLC 1.7.4 jar after verifying its SHA-256.
It never downloads a tool. Logs, state databases, and negative traces are placed in a
new directory under `target/sched-info-model/`.

## Code correspondence

- `Wake` represents a successful `ClassScheduler::enqueue` after task ownership has
  moved from blocked to a runqueue.
- `Duplicate` represents a racing wake that finds the task still queue-owned and must
  preserve the first queued timestamp.
- `Dispatch` represents `PerCpuClassRqSet::try_pick_next` selecting the entity and
  completing one runnable-wait interval.
- `Preempt` represents replacing a still-runnable current task and requeueing it.
- `Block` represents `dequeue_current` after `Wait` or `Exit`; blocked time is excluded.
- `Migrate` represents removal from a running CPU followed by successful destination
  enqueue; its queued interval starts at destination admission.

`impl*` variables model the proposed Rust counters. `spec*` variables are an
independent reference ledger. `LedgerMatches` requires equality after every step.

## Negative controls and limits

`ResetDuplicate` resets the implementation timestamp on a duplicate enqueue and must
under-count a later wait. `CountBlocked` starts an implementation timestamp when a
task blocks and must over-count sleeping time after wakeup. Both configurations must
exit TLC with invariant failure before the corrected model is accepted.

The model assumes the concrete runqueue ownership contract serializes transitions for
one task. It does not prove Rust atomics, counter conversion, weak-memory behavior,
fair scheduling, eventual dispatch, or the procfs reader. Those boundaries are covered
by kernel tests and the userspace regression. This is exhaustive checking of the stated
finite abstraction, not a proof of the complete scheduler.
