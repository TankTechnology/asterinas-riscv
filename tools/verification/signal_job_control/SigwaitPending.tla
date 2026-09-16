---------------------------- MODULE SigwaitPending ----------------------------
\* SPDX-License-Identifier: MPL-2.0
\* Copyright (c) 2026 The Asterinas Authors.
EXTENDS Naturals

CONSTANT UnblockWaited
VARIABLES phase, pending, sent, received, wake
vars == <<phase, pending, sent, received, wake>>

\* One originally blocked, default-ignored SIGCHLD, explicitly requested by
\* sigtimedwait. The old temporary unblocking changes the cancellation probe's
\* treatment of this signal. The explicit dequeue still uses the original mask.
Init ==
    /\ phase = "Check"
    /\ pending = FALSE /\ sent = FALSE /\ received = FALSE /\ wake = FALSE

Send ==
    /\ ~sent /\ phase # "Done"
    /\ sent' = TRUE /\ pending' = TRUE /\ wake' = TRUE
    /\ UNCHANGED <<phase, received>>

Check ==
    /\ phase = "Check"
    /\ phase' = IF pending THEN "Done" ELSE "Cancel"
    /\ received' = pending /\ pending' = FALSE
    /\ UNCHANGED <<sent, wake>>

Cancel ==
    /\ phase = "Cancel"
    /\ phase' = "Wait"
    \* has_pending silently discards ignored, unblocked signals. It returns
    \* false either way, so there is no error-path condition recheck here.
    /\ pending' = IF UnblockWaited THEN FALSE ELSE pending
    /\ UNCHANGED <<sent, received, wake>>

Wake ==
    /\ phase = "Wait" /\ wake
    /\ phase' = "Check" /\ wake' = FALSE
    /\ UNCHANGED <<pending, sent, received>>

Next == Send \/ Check \/ Cancel \/ Wake
TypeOK ==
    /\ phase \in {"Check", "Cancel", "Wait", "Done"}
    /\ pending \in BOOLEAN /\ sent \in BOOLEAN
    /\ received \in BOOLEAN /\ wake \in BOOLEAN
WaitedSignalPreserved == sent => (pending \/ received)
NoInventedReceive == received => sent

Spec == Init /\ [][Next]_vars
=============================================================================
