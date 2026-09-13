---------------------------- MODULE JobControl ----------------------------
\* SPDX-License-Identifier: MPL-2.0
\* Copyright (c) 2026 The Asterinas Authors.
EXTENDS Naturals, FiniteSets

CONSTANTS Threads, MaxGenerations, CancelPending, RevokeSelected
VARIABLES pending, kind, generation, selected, permit, stopped, exiting,
          lastStop, lastCont, staleCommit, pendingKill, killedCommit

vars == <<pending, kind, generation, selected, permit, stopped, exiting,
          lastStop, lastCont, staleCommit, pendingKill, killedCommit>>
Queues == Threads \cup {"shared"}
Ids == 1..MaxGenerations
Opposite(s) == IF s = "STOP" THEN "CONT" ELSE "STOP"

Init ==
    /\ pending = [q \in Queues |-> {}]
    /\ kind = [i \in Ids |-> "unused"]
    /\ generation = 0
    /\ selected = [t \in Threads |-> 0]
    /\ permit = [t \in Threads |-> FALSE]
    /\ stopped = FALSE
    /\ exiting = FALSE
    \* These history variables are an oracle, never a permission to act.
    /\ lastStop = 0
    /\ lastCont = 0
    /\ staleCommit = FALSE
    /\ pendingKill = {}
    /\ killedCommit = FALSE

Generate(s, target) ==
    /\ ~exiting
    /\ generation < MaxGenerations
    /\ LET id == generation + 1 IN
       /\ generation' = id
       /\ kind' = [kind EXCEPT ![id] = s]
       \* Standard signals coalesce in their target queue. Cancellation scans
       \* every queue atomically with insertion and the continue side effects.
       /\ pending' = [q \in Queues |->
              {i \in pending[q] :
                  ~(CancelPending /\ kind[i] = Opposite(s)) /\
                  ~(q = target /\ kind[i] = s)}
              \cup (IF q = target THEN {id} ELSE {})]
       /\ lastStop' = IF s = "STOP" THEN id ELSE lastStop
       /\ lastCont' = IF s = "CONT" THEN id ELSE lastCont
    /\ permit' = IF s = "CONT" /\ RevokeSelected
                  THEN [t \in Threads |-> FALSE] ELSE permit
    /\ stopped' = IF s = "CONT" THEN FALSE ELSE stopped
    /\ UNCHANGED <<selected, exiting, staleCommit, pendingKill, killedCommit>>

Select(t, q, id) ==
    /\ ~exiting /\ ~stopped /\ selected[t] = 0
    /\ q \in {"shared", t} /\ id \in pending[q]
    /\ pending' = [pending EXCEPT ![q] = @ \ {id}]
    /\ selected' = [selected EXCEPT
                      ![t] = IF kind[id] = "STOP" THEN id ELSE 0]
    /\ permit' = [permit EXCEPT ![t] = kind[id] = "STOP"]
    /\ UNCHANGED <<kind, generation, stopped, exiting,
                   lastStop, lastCont, staleCommit, pendingKill, killedCommit>>

CommitStop(t) ==
    /\ selected[t] # 0
    \* The implementation consults only its permit and exit state, under the
    \* same coordinator as generation. It cannot consult the history oracle.
    /\ LET fatalPending == pendingKill \cap {"shared", t} # {}
           accepted == permit[t] /\ ~exiting /\ ~fatalPending IN
       /\ stopped' = IF accepted THEN TRUE ELSE stopped
       /\ staleCommit' = (staleCommit \/
                             (accepted /\ selected[t] <= lastCont))
       /\ killedCommit' = (killedCommit \/ (accepted /\ (fatalPending \/ exiting)))
    /\ selected' = [selected EXCEPT ![t] = 0]
    /\ permit' = [permit EXCEPT ![t] = FALSE]
    /\ UNCHANGED <<pending, kind, generation, exiting, lastStop, lastCont, pendingKill>>

EnqueueKill(q) ==
    /\ ~exiting /\ q \notin pendingKill
    /\ pendingKill' = pendingKill \cup {q}
    /\ UNCHANGED <<pending, kind, generation, selected, permit, stopped, exiting,
                   lastStop, lastCont, staleCommit, killedCommit>>

\* This is actual group-exit commitment, not mere SIGKILL generation.
CommitExit ==
    /\ ~exiting
    /\ exiting' = TRUE
    /\ UNCHANGED <<pending, kind, generation, selected, stopped, permit,
                   lastStop, lastCont, staleCommit, pendingKill, killedCommit>>

Next ==
    \/ \E s \in {"STOP", "CONT"}, q \in Queues : Generate(s, q)
    \/ \E t \in Threads, q \in Queues, id \in Ids : Select(t, q, id)
    \/ \E t \in Threads : CommitStop(t)
    \/ \E q \in Queues : EnqueueKill(q)
    \/ CommitExit

TypeOK ==
    /\ pending \in [Queues -> SUBSET Ids]
    /\ kind \in [Ids -> {"unused", "STOP", "CONT"}]
    /\ generation \in 0..MaxGenerations
    /\ selected \in [Threads -> 0..MaxGenerations]
    /\ permit \in [Threads -> BOOLEAN]
    /\ stopped \in BOOLEAN
    /\ exiting \in BOOLEAN /\ staleCommit \in BOOLEAN
    /\ pendingKill \subseteq Queues /\ killedCommit \in BOOLEAN
    /\ lastStop \in 0..MaxGenerations /\ lastCont \in 0..MaxGenerations

PendingMatchesLatestGeneration ==
    \A q \in Queues : \A id \in pending[q] :
        /\ kind[id] # "unused"
        /\ (kind[id] = "STOP" => id > lastCont)
        /\ (kind[id] = "CONT" => id > lastStop)

NoStaleStopCommit == ~staleCommit
NoStopAfterFatal == ~killedCommit
Spec == Init /\ [][Next]_vars
=============================================================================
