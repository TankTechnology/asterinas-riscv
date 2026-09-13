----------------------------- MODULE GroupStop -----------------------------
\* SPDX-License-Identifier: MPL-2.0
\* Copyright (c) 2026 The Asterinas Authors.
EXTENDS Integers, FiniteSets

CONSTANTS Threads, InitialThreads, MaxEpisodes,
          PrematureReport, SkipJoinEnrollment, DoubleExitDecrement, SavedAck,
          PostAckUserspace
VARIABLES phase, members, published, participation, counted, outstanding,
          episode, required, acknowledged, departed, reports,
          saved, prepared, badReport, badStaleAck, duplicateAck,
          escaped, exitCommitted, reportAfterExit, stopObligations

vars == <<phase, members, published, participation, counted, outstanding,
          episode, required, acknowledged, departed, reports,
          saved, prepared, badReport, badStaleAck, duplicateAck,
          escaped, exitCommitted, reportAfterExit, stopObligations>>
Episodes == 1..MaxEpisodes
Active == phase \in {"Stopping", "Stopped"}
Pending == {t \in members : participation[t] = "pending" /\ counted[t]}

Init ==
    /\ phase = "Running"
    /\ members = InitialThreads
    /\ published = InitialThreads
    /\ participation = [t \in Threads |-> "idle"]
    /\ counted = [t \in Threads |-> FALSE]
    /\ outstanding = 0
    /\ episode = 0
    \* Finite history is an observation oracle, never an action permission.
    /\ required = [e \in Episodes |-> {}]
    /\ acknowledged = [e \in Episodes |-> {}]
    /\ departed = [e \in Episodes |-> {}]
    /\ reports = {}
    /\ stopObligations = {}
    /\ saved = [t \in Threads |-> 0]
    /\ prepared = {}
    /\ badReport = FALSE
    /\ badStaleAck = FALSE
    /\ duplicateAck = FALSE
    /\ escaped = FALSE
    /\ exitCommitted = FALSE
    /\ reportAfterExit = FALSE

StartStop ==
    /\ phase = "Running" /\ members # {} /\ episode < MaxEpisodes
    /\ phase' = "Stopping"
    /\ episode' = episode + 1
    /\ participation' = [t \in Threads |->
                            IF t \in members THEN "pending" ELSE "idle"]
    /\ counted' = [t \in Threads |-> t \in members]
    /\ outstanding' = Cardinality(members)
    /\ required' = [required EXCEPT ![episode + 1] = members]
    /\ stopObligations' = members
    /\ UNCHANGED <<members, published, acknowledged, departed, reports,
                   saved, prepared, badReport, badStaleAck, duplicateAck,
                   escaped, exitCommitted, reportAfterExit>>

Join(t) ==
    /\ phase # "Exiting" /\ t \notin published
    \* Membership publication and enrollment share one critical section.
    /\ members' = members \cup {t}
    /\ published' = published \cup {t}
    /\ LET enroll == Active /\ ~SkipJoinEnrollment IN
       /\ participation' = [participation EXCEPT
                               ![t] = IF enroll THEN "pending" ELSE "idle"]
       /\ counted' = [counted EXCEPT ![t] = enroll /\ phase = "Stopping"]
       /\ outstanding' = outstanding + IF enroll /\ phase = "Stopping" THEN 1 ELSE 0
    \* Joining a completed stop must park, but does not reopen its report.
    /\ required' = IF Active THEN [required EXCEPT ![episode] = @ \cup {t}]
                   ELSE required
    /\ stopObligations' = IF Active THEN stopObligations \cup {t}
                          ELSE stopObligations
    /\ UNCHANGED <<phase, episode, acknowledged, departed, reports,
                   saved, prepared, badReport, badStaleAck, duplicateAck,
                   escaped, exitCommitted, reportAfterExit>>

Checkpoint(t) ==
    /\ Active /\ t \in members /\ participation[t] = "pending"
    \* Read and consume the CURRENT marker at the actual checkpoint while
    \* holding the coordinator; no pre-coordinator epoch or decrement survives.
    /\ participation' = [participation EXCEPT ![t] = "acknowledged"]
    /\ outstanding' = outstanding - IF counted[t] THEN 1 ELSE 0
    /\ counted' = [counted EXCEPT ![t] = FALSE]
    /\ acknowledged' = [acknowledged EXCEPT ![episode] = @ \cup {t}]
    /\ duplicateAck' = (duplicateAck \/ t \in acknowledged[episode])
    /\ UNCHANGED stopObligations
    /\ UNCHANGED <<phase, members, published, episode, required, departed,
                   reports, saved, prepared, badReport, badStaleAck,
                   escaped, exitCommitted, reportAfterExit>>

Leave(t) ==
    /\ phase # "Exiting" /\ t \in members
    /\ members' = members \ {t}
    /\ LET consume == participation[t] = "pending" /\ counted[t]
           double == DoubleExitDecrement /\ phase = "Stopping"
                     /\ participation[t] = "acknowledged" IN
       /\ outstanding' = outstanding - IF consume \/ double THEN 1 ELSE 0
    /\ participation' = [participation EXCEPT ![t] = "idle"]
    /\ counted' = [counted EXCEPT ![t] = FALSE]
    /\ departed' = IF Active THEN [departed EXCEPT ![episode] = @ \cup {t}]
                   ELSE departed
    /\ stopObligations' = stopObligations \ {t}
    /\ UNCHANGED <<phase, published, episode, required, acknowledged,
                   reports, saved, prepared, badReport, badStaleAck,
                   duplicateAck, escaped, exitCommitted, reportAfterExit>>

ReportStopped ==
    /\ phase = "Stopping"
    /\ (outstanding = 0 \/ PrematureReport)
    /\ phase' = "Stopped"
    /\ reports' = reports \cup {episode}
    /\ badReport' = (badReport \/
           (required[episode] \ (acknowledged[episode] \cup departed[episode]) # {}))
    /\ reportAfterExit' = (reportAfterExit \/ exitCommitted)
    /\ UNCHANGED stopObligations
    /\ UNCHANGED <<members, published, participation, counted, outstanding,
                   episode, required, acknowledged, departed, saved, prepared,
                   badStaleAck, duplicateAck, escaped, exitCommitted>>

Continue ==
    /\ Active
    /\ phase' = "Running"
    /\ participation' = [t \in Threads |-> "idle"]
    /\ counted' = [t \in Threads |-> FALSE]
    /\ outstanding' = 0
    /\ stopObligations' = {}
    /\ UNCHANGED <<members, published, episode, required, acknowledged,
                   departed, reports, saved, prepared, badReport, badStaleAck,
                   duplicateAck, escaped, exitCommitted, reportAfterExit>>

CheckUserAdmission(t) ==
    /\ phase # "Exiting" /\ t \in members
    /\ (participation[t] = "idle" \/
           (PostAckUserspace /\ participation[t] = "acknowledged"))
    \* This is the final stop check admitting user execution, not architectural
    \* entry. STOP may arrive after admission but before actual entry, leaving
    \* a pending participant running until its next checkpoint. ACK does not
    \* release its obligation; the observer uses neither markers nor reports.
    /\ escaped' = (escaped \/ t \in stopObligations)
    /\ UNCHANGED stopObligations
    /\ UNCHANGED <<phase, members, published, participation, counted,
                   outstanding, episode, required, acknowledged, departed,
                   reports, saved, prepared, badReport, badStaleAck,
                   duplicateAck, exitCommitted, reportAfterExit>>

CommitGroupExit ==
    /\ phase # "Exiting"
    /\ phase' = "Exiting"
    /\ exitCommitted' = TRUE
    /\ participation' = [t \in Threads |-> "idle"]
    /\ counted' = [t \in Threads |-> FALSE]
    /\ outstanding' = 0
    /\ stopObligations' = {}
    /\ UNCHANGED <<members, published, episode, required, acknowledged,
                   departed, reports, saved, prepared, badReport, badStaleAck,
                   duplicateAck, escaped, reportAfterExit>>

\* Negative control only: incorrectly save a participation before taking the
\* coordinator and later apply that decrement even after CONT and a new STOP.
PrepareSavedAck(t) ==
    /\ SavedAck /\ Active /\ t \in members
    /\ participation[t] = "pending" /\ t \notin prepared
    /\ saved' = [saved EXCEPT ![t] = episode]
    /\ prepared' = prepared \cup {t}
    /\ UNCHANGED stopObligations
    /\ UNCHANGED <<phase, members, published, participation, counted,
                   outstanding, episode, required, acknowledged, departed,
                   reports, badReport, badStaleAck, duplicateAck,
                   escaped, exitCommitted, reportAfterExit>>

ApplySavedAck(t) ==
    /\ SavedAck /\ Active /\ t \in members
    /\ saved[t] # 0 /\ participation[t] = "pending"
    /\ participation' = [participation EXCEPT ![t] = "acknowledged"]
    /\ outstanding' = outstanding - IF counted[t] THEN 1 ELSE 0
    /\ counted' = [counted EXCEPT ![t] = FALSE]
    /\ acknowledged' = [acknowledged EXCEPT ![saved[t]] = @ \cup {t}]
    /\ duplicateAck' = (duplicateAck \/ t \in acknowledged[saved[t]])
    /\ badStaleAck' = (badStaleAck \/ saved[t] # episode)
    /\ saved' = [saved EXCEPT ![t] = 0]
    /\ UNCHANGED stopObligations
    /\ UNCHANGED <<phase, members, published, episode, required, departed,
                   reports, prepared, badReport, escaped,
                   exitCommitted, reportAfterExit>>

Next ==
    \/ StartStop \/ ReportStopped \/ Continue \/ CommitGroupExit
    \/ \E t \in Threads : Join(t) \/ Checkpoint(t) \/ Leave(t)
                          \/ CheckUserAdmission(t)
                          \/ PrepareSavedAck(t) \/ ApplySavedAck(t)

TypeOK ==
    /\ phase \in {"Running", "Stopping", "Stopped", "Exiting"}
    /\ members \subseteq published /\ published \subseteq Threads
    /\ participation \in [Threads -> {"idle", "pending", "acknowledged"}]
    /\ counted \in [Threads -> BOOLEAN]
    /\ outstanding \in (-Cardinality(Threads))..Cardinality(Threads)
    /\ episode \in 0..MaxEpisodes
    /\ required \in [Episodes -> SUBSET Threads]
    /\ acknowledged \in [Episodes -> SUBSET Threads]
    /\ departed \in [Episodes -> SUBSET Threads]
    /\ reports \subseteq Episodes
    /\ stopObligations \subseteq Threads
    /\ saved \in [Threads -> 0..MaxEpisodes] /\ prepared \subseteq Threads
    /\ badReport \in BOOLEAN /\ badStaleAck \in BOOLEAN
    /\ duplicateAck \in BOOLEAN /\ escaped \in BOOLEAN
    /\ exitCommitted \in BOOLEAN /\ reportAfterExit \in BOOLEAN

OutstandingMatchesPending == outstanding = Cardinality(Pending)
EveryMemberEnrolled == Active => \A t \in members : participation[t] # "idle"
AcknowledgeAtMostOnce == ~duplicateAck
NoPrematureReport == ~badReport
NoStaleAcknowledgment == ~badStaleAck
NoUserEscape == ~escaped
NoStopReportAfterExit == ~reportAfterExit /\ (exitCommitted => phase = "Exiting")
Spec == Init /\ [][Next]_vars
=============================================================================
