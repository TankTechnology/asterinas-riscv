------------------------------ MODULE PollScheduler ------------------------------
(***************************************************************************)
(*  A TLA+ model of the RISC-V network interface poll scheduler.            *)
(*                                                                          *)
(*  Real code:                                                              *)
(*    kernel/src/net/iface/sched.rs   (`PollScheduler::schedule_next_poll`) *)
(*    kernel/src/net/iface/poll.rs    (the background polling thread)       *)
(*    kernel/libs/aster-bigtcp/src/iface/poll_iface.rs                      *)
(*      (`PendingConnSet`, `PollKey::IMMEDIATE_VAL = 0`)                    *)
(*                                                                          *)
(*  The caller computes the earliest deadline over the pending TCP          *)
(*  connections and calls `schedule_next_poll(Some(t))` (or `None` for      *)
(*  "nothing pending"). The scheduler stores `t` in an `AtomicU64` and      *)
(*  wakes the polling thread when the value got sooner (or when the stored  *)
(*  value is empty). The thread sleeps until `t`, and re-checks whenever    *)
(*  it is woken.                                                            *)
(*                                                                          *)
(*  The model abstracts the caller's "minimum over pending sockets" as a    *)
(*  single `pending` value that the caller publishes; the focus is the      *)
(*  stored value, the thread's interpretation of it, and the wakeup.        *)
(*                                                                          *)
(*  Two configurations:                                                     *)
(*                                                                          *)
(*   * `NOPOLL = 0`, `ZERO_MEANS_NONE = TRUE` is the pre-fix scheduler:     *)
(*     "no poll" shares the value that `aster-bigtcp` uses for "poll now"   *)
(*     (`PollKey::IMMEDIATE_VAL`). The thread then reads an immediate       *)
(*     request as "nothing to do" and waits for a wakeup that never comes,  *)
(*     stranding a socket that needs a `PollAt::Now` poll.                  *)
(*   * `NOPOLL = TimeLimit + 1`, `ZERO_MEANS_NONE = FALSE` is the fix       *)
(*     (`PollScheduler::NO_POLL = u64::MAX` in the kernel): 0 is a real     *)
(*     deadline and the empty value is unusable as one.                     *)
(***************************************************************************)

EXTENDS Naturals

CONSTANTS
    TimeLimit,        \* time is discrete, `now` ranges over 0..TimeLimit
    NOPOLL,           \* the value stored for "no poll scheduled"
    ZERO_MEANS_NONE   \* does the thread read 0 as "no poll"?

ASSUME NOPOLL = 0 \/ NOPOLL > TimeLimit
ASSUME ZERO_MEANS_NONE \in BOOLEAN

VARIABLES
    now,              \* milliseconds since boot
    pending,          \* earliest deadline over the caller's sockets
    nextPoll,         \* `PollScheduler::next_poll_at_ms`
    thread,           \* "ready" | "poll" | "sleep" | "idle"
    sleepUntil        \* deadline of the current sleep (meaningful in "sleep")

vars == <<now, pending, nextPoll, thread, sleepUntil>>

(***************************************************************************)
(* Helpers                                                                  *)
(***************************************************************************)

\* The thread's reading of the stored value: None means "no deadline".
IsNone(v) == v = NOPOLL \/ (ZERO_MEANS_NONE /\ v = 0)

Ready   == thread = "ready"
Polling == thread = "poll"
Sleeping == thread = "sleep"
Idle    == thread = "idle"

-----------------------------------------------------------------------------
\* Initialization: nothing scheduled, the thread is about to evaluate.
Init ==
    /\ now = 0
    /\ pending = NOPOLL
    /\ nextPoll = NOPOLL
    /\ thread = "ready"
    /\ sleepUntil = 0

(***************************************************************************)
(* The caller: publish the earliest deadline and call schedule_next_poll.   *)
(*                                                                          *)
(* `schedule_next_poll` stores the new value and wakes the thread when the  *)
(* new deadline is sooner than the stored one, or when the stored one is    *)
(* empty (`old == NO_POLL || new < old` in the kernel).                     *)
(***************************************************************************)
Schedule(t) ==
    /\ pending' = t
    /\ nextPoll' = t
    /\ IF nextPoll = NOPOLL \/ t < nextPoll
         THEN thread' = "ready"          \* polling_wait_queue.wake_all()
         ELSE thread' = thread
    /\ UNCHANGED <<now, sleepUntil>>

RequestImmediate == Schedule(0)
RequestLater(t) == t \in 1..TimeLimit /\ Schedule(t)
RequestNone     == Schedule(NOPOLL)

(***************************************************************************)
(* The polling thread re-evaluates its wait closure after waking.           *)
(***************************************************************************)
Observe ==
    /\ Ready
    /\ IF IsNone(nextPoll)
         THEN /\ thread' = "idle"       \* wait_until keeps waiting; no wake left
              /\ UNCHANGED sleepUntil
         ELSE IF now >= nextPoll
                THEN /\ thread' = "poll"
                     /\ UNCHANGED sleepUntil
                ELSE /\ thread' = "sleep"
                     /\ sleepUntil' = nextPoll
    /\ UNCHANGED <<now, pending, nextPoll>>

\* `iface.poll()` runs: due connections are popped and the caller reschedules.
Poll ==
    /\ Polling
    /\ now >= nextPoll
    /\ IF pending /= NOPOLL /\ pending <= now
         THEN /\ pending' = NOPOLL
              /\ nextPoll' = NOPOLL
         ELSE /\ pending' = pending
              /\ nextPoll' = nextPoll
    /\ thread' = "ready"
    /\ UNCHANGED <<now, sleepUntil>>

\* Time passes; a timed sleep expires at its deadline.
Tick ==
    /\ now < TimeLimit
    /\ now' = now + 1
    /\ IF Sleeping /\ sleepUntil <= now + 1
         THEN thread' = "ready"
         ELSE thread' = thread
    /\ UNCHANGED <<pending, nextPoll, sleepUntil>>

Next ==
    \/ RequestImmediate
    \/ (\E t \in 1..TimeLimit : RequestLater(t))
    \/ RequestNone
    \/ Observe
    \/ Poll
    \/ Tick

Fairness == WF_vars(Observe) /\ WF_vars(Poll) /\ WF_vars(RequestImmediate)

Spec == Init /\ [][Next]_vars /\ Fairness

(***************************************************************************)
(* Safety: an immediate-poll request must never be stranded behind a        *)
(* sleeping or indefinitely waiting thread.                                 *)
(***************************************************************************)
NoLostImmediate == pending = 0 => (thread /= "sleep" /\ thread /= "idle")

\* Safety: the thread never sleeps past the earliest deadline.
SleepWithinDeadline ==
    (Sleeping /\ pending /= NOPOLL /\ pending /= 0) => sleepUntil <= pending

\* Safety: a real deadline never leaves the thread waiting indefinitely.
NoIdleWithDeadline == (Idle /\ pending /= NOPOLL) => FALSE

\* The thread only sleeps until a real deadline.
SleepHasDeadline == Sleeping => ~IsNone(sleepUntil) /\ sleepUntil >= now

=======================================================================
