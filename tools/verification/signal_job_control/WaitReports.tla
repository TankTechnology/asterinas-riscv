----------------------------- MODULE WaitReports -----------------------------
\* SPDX-License-Identifier: MPL-2.0
\* Copyright (c) 2026 The Asterinas Authors.
EXTENDS Integers, FiniteSets

CONSTANTS CrossConsumeStop, SplitContinue, ConsumePeek
VARIABLES state, history
vars == <<state, history>>
Readers == {"Parent", "Tracer"}

\* One completed group STOP followed by one CONT, with a distinct real parent
\* and tracer. Publication and wait-slot locking are operation contracts.
Init ==
    /\ state = [phase |-> "Initial", group |-> "Empty", trace |-> FALSE,
                 reserved |-> {}, attempted |-> {}]
    /\ history = [stops |-> {}, continues |-> {},
                   stopPublished |-> FALSE, continuePublished |-> FALSE]

PublishStop ==
    /\ state.phase = "Initial"
    /\ state' = [state EXCEPT !.phase = "Stopped",
                  !.group = "Stop", !.trace = TRUE]
    /\ history' = [history EXCEPT !.stopPublished = TRUE]

PublishContinue ==
    /\ state.phase = "Stopped"
    /\ state' = [state EXCEPT !.phase = "Continued", !.group = "Continue"]
    /\ history' = [history EXCEPT !.continuePublished = TRUE]
    \* The group slot is replaced, but the tracer's stop remains waitable.

WaitStop(reader, peek) ==
    /\ IF reader = "Parent" THEN state.group = "Stop" ELSE state.trace
    /\ LET consume == ~peek \/ ConsumePeek IN
       state' = [state EXCEPT
           !.group = IF consume /\ (reader = "Parent" \/ CrossConsumeStop)
                     THEN "Empty" ELSE @,
           !.trace = IF consume /\ reader = "Tracer" THEN FALSE ELSE @]
    /\ history' = IF peek THEN history
                  ELSE [history EXCEPT !.stops = @ \cup {reader}]

PeekContinue ==
    /\ state.group = "Continue"
    /\ state' = [state EXCEPT !.group = IF ConsumePeek THEN "Empty" ELSE @]
    /\ UNCHANGED history

TakeContinue(reader) ==
    /\ ~SplitContinue /\ state.group = "Continue"
    /\ reader \notin state.attempted
    /\ state' = [state EXCEPT !.group = "Empty", !.attempted = @ \cup {reader}]
    /\ history' = [history EXCEPT !.continues = @ \cup {reader}]
    \* Both readers use the SAME lock for checking and clearing this slot.

CheckContinue(reader) ==
    /\ SplitContinue /\ state.group = "Continue"
    /\ reader \notin state.attempted
    /\ state' = [state EXCEPT !.reserved = @ \cup {reader},
                  !.attempted = @ \cup {reader}]
    /\ UNCHANGED history

FinishContinue(reader) ==
    /\ SplitContinue /\ reader \in state.reserved
    /\ state' = [state EXCEPT !.group = "Empty", !.reserved = @ \ {reader}]
    /\ history' = [history EXCEPT !.continues = @ \cup {reader}]
    \* Fault injection splits the check and clear; both readers can reserve
    \* the same event. History observes returns, never grants permission.

Next ==
    \/ PublishStop \/ PublishContinue \/ PeekContinue
    \/ \E reader \in Readers :
           (\E peek \in BOOLEAN : WaitStop(reader, peek))
           \/ TakeContinue(reader) \/ CheckContinue(reader) \/ FinishContinue(reader)

TypeOK ==
    /\ state.phase \in {"Initial", "Stopped", "Continued"}
    /\ state.group \in {"Empty", "Stop", "Continue"}
    /\ state.trace \in BOOLEAN
    /\ state.reserved \subseteq Readers /\ state.attempted \subseteq Readers
    /\ history.stops \subseteq Readers /\ history.continues \subseteq Readers
    /\ history.stopPublished \in BOOLEAN /\ history.continuePublished \in BOOLEAN

StopSlotsIndependent ==
    /\ state.trace = (history.stopPublished /\ "Tracer" \notin history.stops)
    /\ state.phase = "Stopped" =>
           (state.group = "Stop") = ("Parent" \notin history.stops)
ContinueSlotPreserved ==
    history.continuePublished =>
        (state.group = "Continue") = (history.continues = {})
SingleContinueConsumer == Cardinality(history.continues) <= 1

Spec == Init /\ [][Next]_vars
=============================================================================
