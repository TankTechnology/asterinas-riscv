----------------------------- MODULE FairYield -----------------------------
\* SPDX-License-Identifier: MPL-2.0
\* Copyright (c) 2026 The Asterinas Authors.
EXTENDS Naturals, FiniteSets

CONSTANT Mode
ASSUME Mode \in {"Correct", "IgnoreYield", "ReenqueueBeforePick", "LoseOld"}
Fair == {"fair0", "fair1", "fair2"}
Higher == {"stop", "rt"}
Tasks == Fair \cup Higher \cup {"idle"}
VARIABLES current, queued, sleeping, runnable, yielded, enqueued,
          decisionCurrent, decisionQueue
vars == <<current, queued, sleeping, runnable, yielded, enqueued,
          decisionCurrent, decisionQueue>>

\* Fixed fair ordering represents an allowed minimum-vruntime ordering.
\* In particular, the yielding task can rank before every queued fair peer.
Pick(queue) ==
    CASE "stop" \in queue -> "stop"
      [] "rt" \in queue -> "rt"
      [] "fair0" \in queue -> "fair0"
      [] "fair1" \in queue -> "fair1"
      [] "fair2" \in queue -> "fair2"
      [] OTHER -> "idle"

Init ==
    /\ current = "fair0"
    /\ queued \in SUBSET (Tasks \ {current})
    /\ "idle" \in queued
    /\ sleeping = Tasks \ (queued \cup {current})
    /\ runnable = Tasks \ sleeping
    /\ yielded = FALSE
    /\ enqueued = FALSE
    /\ decisionCurrent = current
    /\ decisionQueue = queued

\* This entire action is the existing IRQ-disabled local-runqueue critical
\* section: update_current, pick_next_entity, replace current, enqueue old.
Yield ==
    /\ ~yielded
    /\ LET shouldSwitch ==
               queued \cap Higher # {}
               \/ (Mode # "IgnoreYield" /\ queued \cap Fair # {})
           candidates == IF Mode = "ReenqueueBeforePick"
                         THEN queued \cup {current} ELSE queued
           next == IF shouldSwitch THEN Pick(candidates) ELSE current
       IN /\ current' = next
          /\ queued' = IF ~shouldSwitch THEN queued
                        ELSE IF Mode = "LoseOld"
                             THEN queued \ {next}
                             ELSE (candidates \ {next})
                                  \cup (IF current = next THEN {} ELSE {current})
    /\ yielded' = TRUE
    /\ decisionCurrent' = current
    /\ decisionQueue' = queued
    /\ UNCHANGED <<sleeping, runnable, enqueued>>

\* One already-authorized external wake/spawn enqueue takes the same lock.
\* It can therefore occur before or after Yield, never inside that action.
ExternalEnqueue(task) ==
    /\ ~enqueued
    /\ task \in sleeping
    /\ sleeping' = sleeping \ {task}
    /\ queued' = queued \cup {task}
    /\ runnable' = runnable \cup {task}
    /\ enqueued' = TRUE
    /\ UNCHANGED <<current, yielded, decisionCurrent, decisionQueue>>

Next == Yield \/ \E task \in Tasks : ExternalEnqueue(task)
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ current \in Tasks
    /\ queued \subseteq Tasks
    /\ sleeping \subseteq Tasks
    /\ runnable \subseteq Tasks
    /\ yielded \in BOOLEAN
    /\ enqueued \in BOOLEAN
    /\ decisionCurrent \in Fair
    /\ decisionQueue \subseteq Tasks

NoDuplicateOwner ==
    /\ current \notin queued
    /\ current \notin sleeping
    /\ queued \cap sleeping = {}
NoLostRunnable == runnable = {current} \cup queued
PopulationPreserved == runnable \cup sleeping = Tasks /\ runnable \cap sleeping = {}
ImmediateFairHandoff ==
    (yielded /\ decisionQueue \cap Higher = {} /\ decisionQueue \cap Fair # {})
    => current \in decisionQueue \cap Fair
HigherPriorityWins == yielded =>
    /\ ("stop" \in decisionQueue => current = "stop")
    /\ ("stop" \notin decisionQueue /\ "rt" \in decisionQueue => current = "rt")
EmptyFairQueueKeepsCurrent ==
    (yielded /\ decisionQueue \cap (Fair \cup Higher) = {})
    => current = decisionCurrent
SelectedFromOldQueue ==
    (yielded /\ current # decisionCurrent) => current \in decisionQueue
=============================================================================
