---------------------- MODULE PageCacheBatchPublish ----------------------
\* SPDX-License-Identifier: MPL-2.0
\* Copyright (c) 2026 The Asterinas Authors.
EXTENDS FiniteSets, Integers, Sequences

CONSTANT Mode
ASSUME Mode \in {"LockBeforePublish", "PublishBeforeLock"}

Threads == {"t0", "t1"}
Pages == {"p0", "p1"}
NoOwner == "none"

Order(t) == IF t = "t0" THEN <<"p0", "p1">> ELSE <<"p1", "p0">>

VARIABLES published, owner, submitted, initialized, phase, position, ownedNew
vars == <<published, owner, submitted, initialized, phase, position, ownedNew>>

Init ==
    /\ published = {}
    /\ owner = [p \in Pages |-> NoOwner]
    /\ submitted = {}
    /\ initialized = {}
    /\ phase = [t \in Threads |-> "start"]
    /\ position = [t \in Threads |-> 1]
    /\ ownedNew = [t \in Threads |-> {}]

\* The corrected implementation performs this transition while holding the
\* XArray lock: every missing page is locked before it becomes observable.
PrepareLockedBatch(t) ==
    /\ Mode = "LockBeforePublish"
    /\ phase[t] = "start"
    /\ LET missing == Pages \ published
       IN /\ published' = Pages
          /\ owner' = [p \in Pages |-> IF p \in missing THEN t ELSE owner[p]]
          /\ ownedNew' = [ownedNew EXCEPT ![t] = missing]
    /\ phase' = [phase EXCEPT ![t] = "prepared"]
    /\ UNCHANGED <<submitted, initialized, position>>

\* Negative control: publishing first lets two opposite-order collectors each
\* take one page and then wait forever before either batch is submitted.
PublishUnlockedBatch(t) ==
    /\ Mode = "PublishBeforeLock"
    /\ phase[t] = "start"
    /\ published' = Pages
    /\ phase' = [phase EXCEPT ![t] = "locking"]
    /\ UNCHANGED <<owner, submitted, initialized, position, ownedNew>>

LockNext(t) ==
    /\ Mode = "PublishBeforeLock"
    /\ phase[t] = "locking"
    /\ position[t] <= Len(Order(t))
    /\ LET p == Order(t)[position[t]]
       IN /\ owner[p] = NoOwner
          /\ owner' = [owner EXCEPT ![p] = t]
          /\ ownedNew' = [ownedNew EXCEPT ![t] = @ \cup {p}]
    /\ position' = [position EXCEPT ![t] = @ + 1]
    /\ UNCHANGED <<published, submitted, initialized, phase>>

BlockOnOwnedPage(t) ==
    /\ Mode = "PublishBeforeLock"
    /\ phase[t] = "locking"
    /\ position[t] <= Len(Order(t))
    /\ LET p == Order(t)[position[t]]
       IN /\ owner[p] \notin {NoOwner, t}
    /\ phase' = [phase EXCEPT ![t] = "waiting"]
    /\ UNCHANGED <<published, owner, submitted, initialized, position, ownedNew>>

FinishLocking(t) ==
    /\ Mode = "PublishBeforeLock"
    /\ phase[t] = "locking"
    /\ position[t] > Len(Order(t))
    /\ phase' = [phase EXCEPT ![t] = "prepared"]
    /\ UNCHANGED <<published, owner, submitted, initialized, position, ownedNew>>

SubmitBatch(t) ==
    /\ phase[t] = "prepared"
    /\ submitted' = submitted \cup ownedNew[t]
    /\ phase' = [phase EXCEPT ![t] = "waiting"]
    /\ UNCHANGED <<published, owner, initialized, position, ownedNew>>

CompletePage(p) ==
    /\ p \in submitted \ initialized
    /\ initialized' = initialized \cup {p}
    /\ owner' = [owner EXCEPT ![p] = NoOwner]
    /\ UNCHANGED <<published, submitted, phase, position, ownedNew>>

Finish(t) ==
    /\ phase[t] = "waiting"
    /\ initialized = Pages
    /\ phase' = [phase EXCEPT ![t] = "done"]
    /\ UNCHANGED <<published, owner, submitted, initialized, position, ownedNew>>

Next ==
    \/ \E t \in Threads : PrepareLockedBatch(t)
    \/ \E t \in Threads : PublishUnlockedBatch(t)
    \/ \E t \in Threads : LockNext(t)
    \/ \E t \in Threads : BlockOnOwnedPage(t)
    \/ \E t \in Threads : FinishLocking(t)
    \/ \E t \in Threads : SubmitBatch(t)
    \/ \E p \in Pages : CompletePage(p)
    \/ \E t \in Threads : Finish(t)

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ published \subseteq Pages
    /\ owner \in [Pages -> Threads \cup {NoOwner}]
    /\ submitted \subseteq Pages
    /\ initialized \subseteq Pages
    /\ phase \in [Threads -> {"start", "locking", "prepared", "waiting", "done"}]
    /\ position \in [Threads -> 1..3]
    /\ ownedNew \in [Threads -> SUBSET Pages]

NoUnsubmittedWaitCycle ==
    ~(phase["t0"] = "waiting" /\ phase["t1"] = "waiting" /\ submitted = {})
=============================================================================
