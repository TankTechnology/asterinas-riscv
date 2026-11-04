# Net poll scheduler model

A TLA+ model of the RISC-V network interface poll scheduler:

- `kernel/src/net/iface/sched.rs` — `PollScheduler::schedule_next_poll` and the
  `next_poll_at_ms` it keeps in an `AtomicU64`.
- `kernel/src/net/iface/poll.rs` — the background polling thread: wait on the
  queue, read `next_poll_at_ms`, sleep for the remainder, poll.
- `kernel/libs/aster-bigtcp/src/iface/poll_iface.rs` — the caller that computes
  the earliest deadline over pending TCP connections and, in particular,
  `PollKey::IMMEDIATE_VAL = 0` for "poll now".

The property is the one a scheduler must not break: an immediate-poll request
must never be stranded behind a sleeping or indefinitely waiting thread. The
model is what makes the sentinel bug (`9a802125b`) checkable: the pre-fix
scheduler stored `"no poll"` as `0`, the same value `aster-bigtcp` uses for
`"poll now"`, so the thread read an immediate request as "nothing to do" and
waited for a wakeup that never came.

```sh
../fetch-tla.sh
java -cp ../../../target/tla/tla2tools.jar tlc2.TLC -config PollSchedulerFixed.cfg PollScheduler.tla
java -cp ../../../target/tla/tla2tools.jar tlc2.TLC -config PollSchedulerBuggy.cfg PollScheduler.tla
```

- `PollSchedulerFixed.cfg` (`NOPOLL > TimeLimit`, 0 is a real deadline) must
  report no error. Invariants: `NoLostImmediate`, `SleepWithinDeadline`,
  `NoIdleWithDeadline`, `SleepHasDeadline`.
- `PollSchedulerBuggy.cfg` (`NOPOLL = 0`, the thread reads 0 as "none") must
  report `NoLostImmediate` violated.

`run.py` enforces both expectations from the `\* EXPECT:` comments in the
configs, so the pre-fix run is a regression test, not just documentation.

Time is discrete and bounded (`TimeLimit`); the caller's "minimum over pending
sockets" is abstracted as a single `pending` value, so the model checks the
scheduler/thread protocol rather than the pending-set bookkeeping.
