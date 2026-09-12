----------------------------- MODULE WaitWake -----------------------------
\* SPDX-License-Identifier: MPL-2.0
\* Copyright (c) 2026 The Asterinas Authors.
EXTENDS Naturals, FiniteSets

CONSTANTS Threads, RememberEarlyWake
VARIABLES phase, token, stopped, notified
vars == <<phase, token, stopped, notified>>

Init ==
    /\ phase = [t \in Threads |-> "start"]
    /\ token = [t \in Threads |-> FALSE]
    /\ stopped = TRUE
    /\ notified = {}

Register(t) ==
    /\ phase[t] = "start"
    /\ phase' = [phase EXCEPT ![t] = "registered"]
    /\ UNCHANGED <<token, stopped, notified>>

Check(t) ==
    /\ phase[t] \in {"registered", "ready"}
    /\ phase' = [phase EXCEPT ![t] = IF stopped THEN "checked" ELSE "done"]
    /\ UNCHANGED <<token, stopped, notified>>

Park(t) ==
    /\ phase[t] = "checked"
    /\ phase' = [phase EXCEPT ![t] = IF token[t] THEN "ready" ELSE "parked"]
    /\ token' = [token EXCEPT ![t] = FALSE]
    /\ UNCHANGED <<stopped, notified>>

Resume ==
    /\ stopped
    /\ stopped' = FALSE
    \* Actual wake callbacks may execute after releasing the coordinator.
    /\ UNCHANGED <<phase, token, notified>>

Notify(t) ==
    /\ ~stopped /\ t \notin notified
    /\ notified' = notified \cup {t}
    /\ phase' = [phase EXCEPT ![t] = IF @ = "parked" THEN "ready" ELSE @]
    /\ token' = [token EXCEPT ![t] =
           @ \/ (RememberEarlyWake /\ phase[t] \in {"registered", "checked"})]
    /\ UNCHANGED stopped

\* An unrelated wake must cause a predicate recheck, not escape the wait.
SpuriousWake(t) ==
    /\ stopped /\ phase[t] = "parked"
    /\ phase' = [phase EXCEPT ![t] = "ready"]
    /\ UNCHANGED <<token, stopped, notified>>

Next == Resume \/ \E t \in Threads :
    Register(t) \/ Check(t) \/ Park(t) \/ Notify(t) \/ SpuriousWake(t)
Fairness == WF_vars(Resume) /\
    \A t \in Threads : WF_vars(Register(t)) /\ WF_vars(Check(t)) /\
                       WF_vars(Park(t)) /\ WF_vars(Notify(t))
Spec == Init /\ [][Next]_vars /\ Fairness

TypeOK ==
    /\ phase \in [Threads -> {"start", "registered", "checked", "parked", "ready", "done"}]
    /\ token \in [Threads -> BOOLEAN]
    /\ stopped \in BOOLEAN /\ notified \subseteq Threads

NoLostWake == \A t \in Threads :
    (~stopped /\ t \in notified) => phase[t] # "parked"
DoneRequiresResume == \A t \in Threads : phase[t] = "done" => ~stopped
EventuallyDone == <> (\A t \in Threads : phase[t] = "done")
=============================================================================
