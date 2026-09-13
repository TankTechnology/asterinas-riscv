--------------------------- MODULE ChildNotification ---------------------------
\* SPDX-License-Identifier: MPL-2.0
\* Copyright (c) 2026 The Asterinas Authors.
EXTENDS Naturals

CONSTANTS SplitDispositionCheck, SuppressWaitWake
VARIABLES suppressed, changes, phase, saved, committedPolicy, emitted, waitWoken
vars == <<suppressed, changes, phase, saved, committedPolicy, emitted, waitWoken>>

\* One already committed child STOP/CONT event and at most two concurrent
\* disposition changes. The parent's wait event is independent of SIGCHLD.
Init ==
    /\ suppressed \in BOOLEAN /\ changes = 0 /\ phase = "start"
    /\ saved = FALSE /\ committedPolicy = FALSE
    /\ emitted = FALSE /\ waitWoken = FALSE

ChangeDisposition ==
    /\ phase # "done" /\ changes < 2
    /\ suppressed' = ~suppressed /\ changes' = changes + 1
    /\ UNCHANGED <<phase, saved, committedPolicy, emitted, waitWoken>>

Check ==
    /\ phase = "start"
    /\ saved' = suppressed
    /\ IF SplitDispositionCheck
          THEN /\ phase' = "checked"
               /\ UNCHANGED <<committedPolicy, emitted, waitWoken>>
          ELSE /\ phase' = "committed"
               /\ committedPolicy' = suppressed
               /\ emitted' = ~suppressed
               /\ UNCHANGED waitWoken
    /\ UNCHANGED <<suppressed, changes>>

CommitAfterUnlock ==
    /\ phase = "checked" /\ phase' = "committed"
    /\ committedPolicy' = suppressed
    /\ emitted' = ~saved
    /\ UNCHANGED <<suppressed, changes, saved, waitWoken>>

\* The real callback runs after dropping the disposition/coordinator locks.
WakeWaiter ==
    /\ phase = "committed" /\ phase' = "done"
    /\ waitWoken' = ~(SuppressWaitWake /\ ~emitted)
    /\ UNCHANGED <<suppressed, changes, saved, committedPolicy, emitted>>

Next == ChangeDisposition \/ Check \/ CommitAfterUnlock \/ WakeWaiter
TypeOK ==
    /\ suppressed \in BOOLEAN /\ changes \in 0..2
    /\ phase \in {"start", "checked", "committed", "done"}
    /\ saved \in BOOLEAN /\ committedPolicy \in BOOLEAN
    /\ emitted \in BOOLEAN /\ waitWoken \in BOOLEAN
DispositionMatchesCommit == phase \in {"committed", "done"} => emitted = ~committedPolicy
SuppressionPreservesWaitWake == phase = "done" => waitWoken
Spec == Init /\ [][Next]_vars
=============================================================================
