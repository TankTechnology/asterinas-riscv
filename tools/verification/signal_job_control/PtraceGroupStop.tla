-------------------------- MODULE PtraceGroupStop --------------------------
\* SPDX-License-Identifier: MPL-2.0
\* Copyright (c) 2026 The Asterinas Authors.
EXTENDS Integers, FiniteSets

CONSTANTS Threads, MaxStops, StaleResume, ContinueReleasesTrace, DetachLosesPark
VARIABLES state, history
vars == <<state, history>>

\* This is an operation contract for traditional ptrace, not a refinement of
\* Rust or a model of PTRACE_SEIZE/PTRACE_LISTEN. See the adjacent README.
Init ==
    /\ state = [active |-> FALSE, completed |-> FALSE, exiting |-> FALSE,
                 members |-> Threads,
                 marker |-> [t \in Threads |-> "Inactive"],
                 counted |-> {}, remaining |-> 0,
                 traced |-> {}, stopped |-> {}, returning |-> {},
                 attached |-> {}, stops |-> 0]
    \* Observers record obligations from events, never grant action permission.
    /\ history = [park |-> {}, traceHold |-> {}, needsAck |-> {},
                   cancelledReturn |-> {},
                   reported |-> FALSE, badReport |-> FALSE,
                   escaped |-> FALSE]

StartStop(t) ==
    /\ ~state.exiting /\ t \in state.members
    /\ t \notin state.stopped \cup state.returning
    /\ state.marker[t] \in {"Inactive", "PtraceControlled"}
    /\ state.stops < MaxStops
    /\ LET enroll == {u \in state.members : state.marker[u] # "Parked"} IN
       /\ state' = [state EXCEPT
              !.active = TRUE, !.stops = @ + 1,
              !.marker = [u \in Threads |->
                  IF u \in enroll THEN "Pending" ELSE state.marker[u]],
              !.counted = enroll, !.remaining = Cardinality(enroll)]
       /\ history' = [history EXCEPT
              !.park = @ \cup enroll, !.needsAck = enroll]
    \* A new STOP may follow tracer CONT with the group still latched stopped.
    \* Keep completed independent of this new round's counted participants.
    \* Existing ptrace-stopped members also enroll; Parked members do not.

Attach(t) ==
    /\ ~state.exiting /\ t \in state.members /\ t \notin state.attached
    /\ state' = [state EXCEPT
           !.attached = @ \cup {t}, !.traced = @ \cup {t},
           !.marker[t] = IF @ = "Parked" THEN "Pending" ELSE @]
    /\ UNCHANGED history
    \* Attaching a parked thread installs an uncounted checkpoint obligation.
    \* The attach signal's delivery stop is a separate action below.

SignalDeliveryStop(t) ==
    /\ ~state.exiting /\ t \in state.members \cap state.traced
    /\ t \notin state.stopped \cup state.returning
    /\ state.marker[t] # "Parked"
    /\ state' = [state EXCEPT
           !.stopped = @ \cup {t}, !.returning = @ \cup {t}]
    /\ history' = [history EXCEPT !.traceHold = @ \cup {t}]
    \* A delivery stop cannot consume an outstanding group participation.

Checkpoint(t) ==
    /\ ~state.exiting /\ t \in state.members
    /\ state.marker[t] = "Pending"
    /\ t \notin state.stopped \cup state.returning
    /\ LET traced == t \in state.traced IN
       /\ state' = [state EXCEPT
              !.marker[t] = IF traced THEN "PtraceControlled" ELSE "Parked",
              !.counted = @ \ {t},
              !.remaining = @ - IF t \in state.counted THEN 1 ELSE 0,
              !.stopped = IF traced THEN @ \cup {t} ELSE @,
              !.returning = IF traced THEN @ \cup {t} ELSE @]
       /\ history' = [history EXCEPT
              !.needsAck = @ \ {t},
              !.park = IF traced THEN @ \ {t} ELSE @,
              !.traceHold = IF traced THEN @ \cup {t} ELSE @]
    \* Read the CURRENT marker and consume its counted flag under the
    \* coordinator. For a traced thread, hold tracee state across that action
    \* and publication of its ptrace group-stop, before releasing either lock.

ReportStopped ==
    /\ ~state.exiting /\ state.active /\ ~state.completed
    /\ state.remaining = 0
    /\ state' = [state EXCEPT !.completed = TRUE]
    /\ history' = [history EXCEPT
           !.badReport = @ \/ history.reported \/ history.needsAck # {},
           !.reported = TRUE]

TracerContinue(t) ==
    /\ ~state.exiting /\ t \in state.members \cap state.traced \cap state.stopped
    /\ state' = [state EXCEPT !.stopped = @ \ {t}]
    /\ history' = [history EXCEPT !.traceHold = @ \ {t}]
    \* Tracer CONT releases only the ptrace stop. The old stop call may return
    \* later, after SIGCONT and another STOP installed a new Pending marker.

ReturnFromTraceStop(t) ==
    /\ ~state.exiting /\ t \in state.members \cap state.returning
    /\ t \notin state.stopped
    /\ LET corrupt == StaleResume /\ t \in history.cancelledReturn IN
       /\ state' = [state EXCEPT
              !.returning = @ \ {t},
              !.marker[t] = IF corrupt THEN "Inactive" ELSE @,
              !.counted = IF corrupt THEN @ \ {t} ELSE @]
    /\ history' = [history EXCEPT !.cancelledReturn = @ \ {t}]
    \* Restrict fault injection to an old return spanning SIGCONT, so the
    \* counterexample exercises SIGCONT followed by a new STOP. Correct actions
    \* never consult this history when deciding how to update participation.

Continue ==
    /\ ~state.exiting
    /\ state' = [state EXCEPT
           !.active = FALSE, !.completed = FALSE,
           !.marker = [t \in Threads |-> "Inactive"],
           !.counted = {}, !.remaining = 0,
           !.stopped = IF ContinueReleasesTrace THEN {} ELSE @]
    /\ history' = [history EXCEPT
           !.park = {}, !.needsAck = {}, !.reported = FALSE,
           !.cancelledReturn = @ \cup state.returning]
    \* SIGCONT can arrive with no active group stop, including during a
    \* delivery-only ptrace stop. It clears group obligations but cannot
    \* authorize leaving any ptrace stop without the tracer's decision.

RemoveTracing(ts) ==
    /\ ~state.exiting /\ ts # {} /\ ts \subseteq state.traced
    /\ LET restore == {t \in ts : state.active
                                    /\ state.marker[t] = "PtraceControlled"} IN
       /\ state' = [state EXCEPT
              !.traced = @ \ ts, !.stopped = @ \ ts,
              !.marker = [t \in Threads |->
                  IF t \in restore
                  THEN IF DetachLosesPark THEN "Inactive" ELSE "Pending"
                  ELSE state.marker[t]]]
       /\ history' = [history EXCEPT
              !.traceHold = @ \ ts, !.park = @ \cup restore]
    \* Detach and tracer exit take tracee state before the coordinator.
    \* Existing Pending/count ownership survives; restored Pending is
    \* uncounted, including while another thread still has a counted marker.

Detach(t) == RemoveTracing({t})
TracerExit == RemoveTracing(state.traced)

Leave(t) ==
    /\ ~state.exiting /\ t \in state.members
    /\ state' = [state EXCEPT
           !.members = @ \ {t}, !.marker[t] = "Inactive",
           !.counted = @ \ {t},
           !.remaining = @ - IF t \in state.counted THEN 1 ELSE 0,
           !.traced = @ \ {t}, !.stopped = @ \ {t}, !.returning = @ \ {t}]
    /\ history' = [history EXCEPT
           !.park = @ \ {t}, !.traceHold = @ \ {t}, !.needsAck = @ \ {t},
           !.cancelledReturn = @ \ {t}]

CommitGroupExit ==
    /\ ~state.exiting
    /\ state' = [state EXCEPT
           !.exiting = TRUE, !.active = FALSE, !.completed = FALSE,
           !.marker = [t \in Threads |-> "Inactive"],
           !.counted = {}, !.remaining = 0, !.stopped = {}, !.returning = {}]
    /\ history' = [history EXCEPT
           !.park = {}, !.traceHold = {}, !.needsAck = {}, !.reported = FALSE,
           !.cancelledReturn = {}]

CheckUserAdmission(t) ==
    /\ ~state.exiting /\ t \in state.members
    /\ t \notin state.stopped \cup state.returning
    /\ state.marker[t] \in {"Inactive", "PtraceControlled"}
    /\ history' = [history EXCEPT
           !.escaped = @ \/ t \in history.park \cup history.traceHold]
    /\ UNCHANGED state

Next ==
    \/ ReportStopped \/ Continue \/ TracerExit \/ CommitGroupExit
    \/ \E t \in Threads : StartStop(t) \/ Attach(t) \/ SignalDeliveryStop(t)
           \/ Checkpoint(t) \/ TracerContinue(t) \/ ReturnFromTraceStop(t)
           \/ Detach(t) \/ Leave(t) \/ CheckUserAdmission(t)

TypeOK ==
    /\ state.active \in BOOLEAN /\ state.completed \in BOOLEAN
    /\ state.exiting \in BOOLEAN /\ state.members \subseteq Threads
    /\ state.marker \in [Threads ->
           {"Inactive", "Pending", "Parked", "PtraceControlled"}]
    /\ state.counted \subseteq state.members
    /\ state.remaining \in 0..Cardinality(Threads)
    /\ state.traced \subseteq state.members
    /\ state.stopped \subseteq state.traced
    /\ state.returning \subseteq state.members
    /\ state.attached \subseteq Threads /\ state.stops \in 0..MaxStops
    /\ history.park \subseteq state.members
    /\ history.traceHold \subseteq state.members
    /\ history.needsAck \subseteq state.members
    /\ history.cancelledReturn \subseteq state.returning
    /\ history.reported \in BOOLEAN /\ history.badReport \in BOOLEAN
    /\ history.escaped \in BOOLEAN

NoLostGroupObligation == \A t \in history.park :
    state.marker[t] \in {"Pending", "Parked"}
PtraceStopOwnedByTracer == state.stopped = history.traceHold
OutstandingMatchesPending ==
    /\ state.remaining = Cardinality(state.counted)
    /\ \A t \in state.counted : state.marker[t] = "Pending"
    /\ state.counted = history.needsAck
NoPrematureOrDuplicateReport == ~history.badReport
CompletedLatchPreserved == state.completed = history.reported
NoUserEscape == ~history.escaped
NoStopAfterExit == state.exiting =>
    ~state.active /\ ~state.completed /\ state.remaining = 0
    /\ \A t \in state.members : state.marker[t] = "Inactive"

Spec == Init /\ [][Next]_vars
=============================================================================
