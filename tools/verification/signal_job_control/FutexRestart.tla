--------------------------- MODULE FutexRestart ---------------------------
\* SPDX-License-Identifier: MPL-2.0
\* Copyright (c) 2026 The Asterinas Authors.
EXTENDS Naturals, FiniteSets

CONSTANTS MaxAttempts, MaxStops, MaxWakeCalls, MaxInterrupts, MaxTime,
          OriginalDeadline, PreserveDeadline, HonorDequeuedWake,
          NormalizeSpuriousWake
VARIABLES phase, queued, pauseResult, result, attempt, deadline, now, word,
          stopped, stops, wakeCalls, interrupts, interruptPending, caught,
          wakeHistory, resolvedAttempt, resolvedResult, replayAfterHandler
vars == <<phase, queued, pauseResult, result, attempt, deadline, now, word,
          stopped, stops, wakeCalls, interrupts, interruptPending, caught,
          wakeHistory, resolvedAttempt, resolvedResult, replayAfterHandler>>

Init ==
    /\ phase = "enqueue" /\ queued = FALSE
    /\ pauseResult = "none" /\ result = "none"
    /\ attempt = 1 /\ deadline = OriginalDeadline /\ now = 0 /\ word = 0
    /\ stopped = FALSE /\ stops = 0 /\ wakeCalls = 0 /\ interrupts = 0
    /\ interruptPending = FALSE /\ caught = FALSE
    /\ wakeHistory = {} /\ resolvedAttempt = 0 /\ resolvedResult = "none"
    /\ replayAfterHandler = FALSE

\* Read the word and enqueue under one bucket lock. An expired deadline
\* still permits enqueue; pause_timeout and cleanup subsequently resolve it.
Enqueue ==
    /\ phase = "enqueue" /\ ~stopped
    /\ phase' = IF word = 0 THEN "waiting" ELSE "done"
    /\ queued' = (word = 0)
    /\ result' = IF word = 0 THEN "none" ELSE "again"
    /\ UNCHANGED <<pauseResult, attempt, deadline, now, word, stopped, stops,
                    wakeCalls, interrupts, interruptPending, caught,
                    wakeHistory, resolvedAttempt, resolvedResult,
                    replayAfterHandler>>

\* Matching wake and cancellation are serialized by the bucket lock.
\* A wake without a queued item is intentionally not remembered for restart.
Wake ==
    /\ wakeCalls < MaxWakeCalls /\ phase # "done"
    /\ wakeCalls' = wakeCalls + 1
    /\ queued' = FALSE
    /\ wakeHistory' = IF queued THEN wakeHistory \cup {attempt} ELSE wakeHistory
    /\ UNCHANGED <<phase, pauseResult, result, attempt, deadline, now, word,
                    stopped, stops, interrupts, interruptPending, caught,
                    resolvedAttempt, resolvedResult, replayAfterHandler>>

Pause(reason) ==
    /\ phase = "waiting"
    /\ CASE reason = "interrupted" -> interruptPending
         [] reason = "timeout" -> now >= deadline
         [] reason = "success" -> ~queued
         [] reason = "spurious" -> TRUE
    \* An unrelated wake returns the same raw Ok as an actual futex wake.
    \* Cleanup must distinguish them using bucket membership, not this reason.
    /\ phase' = "cleanup"
    /\ pauseResult' = IF reason = "spurious" THEN "success" ELSE reason
    /\ interruptPending' = IF reason = "interrupted" THEN FALSE ELSE interruptPending
    /\ UNCHANGED <<queued, result, attempt, deadline, now, word, stopped,
                    stops, wakeCalls, interrupts, caught, wakeHistory,
                    resolvedAttempt, resolvedResult, replayAfterHandler>>

\* Membership, not wakeHistory, selects the implementation's return value.
Cleanup ==
    /\ phase = "cleanup"
    /\ LET outcome == IF HonorDequeuedWake /\ ~queued
                      THEN "success"
                      ELSE IF NormalizeSpuriousWake /\ pauseResult = "success"
                           THEN "interrupted" ELSE pauseResult
       IN /\ result' = outcome
          /\ phase' = IF outcome = "interrupted" THEN "delivery" ELSE "done"
          /\ resolvedResult' = outcome
    /\ queued' = FALSE /\ resolvedAttempt' = attempt
    /\ UNCHANGED <<pauseResult, attempt, deadline, now, word, stopped, stops,
                    wakeCalls, interrupts, interruptPending, caught,
                    wakeHistory, replayAfterHandler>>

\* Signal delivery may run a caught handler, or handle only default/ignored
\* signals. The latter includes the completed STOP/CONT path.
Deliver(handler) ==
    /\ phase = "delivery" /\ ~stopped
    /\ caught' = handler /\ phase' = "decision"
    /\ UNCHANGED <<queued, pauseResult, result, attempt, deadline, now, word,
                    stopped, stops, wakeCalls, interrupts, interruptPending,
                    wakeHistory, resolvedAttempt, resolvedResult,
                    replayAfterHandler>>

ReturnInterrupted ==
    /\ phase = "decision" /\ ~stopped /\ caught
    /\ phase' = "done" /\ result' = "interrupted"
    /\ UNCHANGED <<queued, pauseResult, attempt, deadline, now, word, stopped,
                    stops, wakeCalls, interrupts, interruptPending, caught,
                    wakeHistory, resolvedAttempt, resolvedResult,
                    replayAfterHandler>>

Restart ==
    /\ phase = "decision" /\ ~stopped /\ ~caught
    /\ attempt < MaxAttempts
    /\ attempt' = attempt + 1 /\ phase' = "enqueue"
    /\ deadline' = IF PreserveDeadline THEN deadline ELSE now + OriginalDeadline
    /\ pauseResult' = "none" /\ result' = "none"
    /\ replayAfterHandler' = replayAfterHandler \/ caught
    /\ UNCHANGED <<queued, now, word, stopped, stops, wakeCalls, interrupts,
                    interruptPending, caught, wakeHistory, resolvedAttempt,
                    resolvedResult>>

\* stopped abstracts a stop obligation that blocks delivery/replay, not an
\* instantaneous CPU park. In-flight pause and bucket cleanup can complete.
Stop ==
    /\ ~stopped /\ stops < MaxStops /\ phase # "done"
    /\ stopped' = TRUE /\ stops' = stops + 1 /\ interruptPending' = TRUE
    /\ UNCHANGED <<phase, queued, pauseResult, result, attempt, deadline, now,
                    word, wakeCalls, interrupts, caught, wakeHistory,
                    resolvedAttempt, resolvedResult, replayAfterHandler>>

Continue ==
    /\ stopped /\ stopped' = FALSE
    /\ UNCHANGED <<phase, queued, pauseResult, result, attempt, deadline, now,
                    word, stops, wakeCalls, interrupts, interruptPending,
                    caught, wakeHistory, resolvedAttempt, resolvedResult,
                    replayAfterHandler>>

Interrupt ==
    /\ interrupts < MaxInterrupts /\ phase # "done"
    /\ interrupts' = interrupts + 1 /\ interruptPending' = TRUE
    /\ UNCHANGED <<phase, queued, pauseResult, result, attempt, deadline, now,
                    word, stopped, stops, wakeCalls, caught, wakeHistory,
                    resolvedAttempt, resolvedResult, replayAfterHandler>>

Tick ==
    /\ now < MaxTime /\ now' = now + 1
    /\ UNCHANGED <<phase, queued, pauseResult, result, attempt, deadline, word,
                    stopped, stops, wakeCalls, interrupts, interruptPending,
                    caught, wakeHistory, resolvedAttempt, resolvedResult,
                    replayAfterHandler>>

ChangeWord ==
    /\ word = 0 /\ word' = 1
    /\ UNCHANGED <<phase, queued, pauseResult, result, attempt, deadline, now,
                    stopped, stops, wakeCalls, interrupts, interruptPending,
                    caught, wakeHistory, resolvedAttempt, resolvedResult,
                    replayAfterHandler>>

Next == Enqueue \/ Wake \/ Cleanup \/ ReturnInterrupted \/ Restart \/ Stop \/
        Continue \/ Interrupt \/ Tick \/ ChangeWord \/
        (\E reason \in {"interrupted", "timeout", "success", "spurious"} : Pause(reason)) \/
        (\E handler \in BOOLEAN : Deliver(handler))
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ phase \in {"enqueue", "waiting", "cleanup", "delivery", "decision", "done"}
    /\ queued \in BOOLEAN /\ stopped \in BOOLEAN /\ caught \in BOOLEAN
    /\ interruptPending \in BOOLEAN /\ replayAfterHandler \in BOOLEAN
    /\ pauseResult \in {"none", "interrupted", "timeout", "success"}
    /\ result \in {"none", "interrupted", "timeout", "success", "again"}
    /\ resolvedResult \in {"none", "interrupted", "timeout", "success"}
    /\ attempt \in 1..MaxAttempts /\ resolvedAttempt \in 0..MaxAttempts
    /\ deadline \in OriginalDeadline..(MaxTime + OriginalDeadline)
    /\ now \in 0..MaxTime /\ word \in {0, 1}
    /\ stops \in 0..MaxStops /\ wakeCalls \in 0..MaxWakeCalls
    /\ interrupts \in 0..MaxInterrupts
    /\ wakeHistory \subseteq 1..MaxAttempts

DeadlinePreserved == deadline = OriginalDeadline
NoLostSuccessfulWake == resolvedAttempt \in wakeHistory => resolvedResult = "success"
NoInventedSuccessfulWake == resolvedResult = "success" => resolvedAttempt \in wakeHistory
NoReplayAfterCaughtHandler == ~replayAfterHandler
QueueOnlyDuringWait == queued => phase \in {"waiting", "cleanup"}
=============================================================================
