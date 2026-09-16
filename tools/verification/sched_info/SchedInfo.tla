----------------------------- MODULE SchedInfo -----------------------------
\* SPDX-License-Identifier: MPL-2.0
\* Copyright (c) 2026 The Asterinas Authors.
EXTENDS Naturals, FiniteSets

CONSTANTS Mode, MaxTime, MaxSteps
ASSUME Mode \in {"Correct", "ResetDuplicate", "CountBlocked"}
ASSUME MaxTime \in Nat \ {0}
ASSUME MaxSteps \in Nat \ {0}

Tasks == {"a", "b"}
Cpus == {0, 1}
None == MaxTime + 1
Owners == {"Blocked", "Q0", "Q1", "R0", "R1"}

Q(cpu) == IF cpu = 0 THEN "Q0" ELSE "Q1"
R(cpu) == IF cpu = 0 THEN "R0" ELSE "R1"
IsQueued(value) == value \in {"Q0", "Q1"}

VARIABLES owner, implQueuedAt, specQueuedAt, implWait, specWait,
          implDispatch, specDispatch, now, steps
vars == <<owner, implQueuedAt, specQueuedAt, implWait, specWait,
          implDispatch, specDispatch, now, steps>>

Init ==
    /\ owner = [task \in Tasks |-> "Blocked"]
    /\ implQueuedAt = [task \in Tasks |-> None]
    /\ specQueuedAt = [task \in Tasks |-> None]
    /\ implWait = [task \in Tasks |-> 0]
    /\ specWait = [task \in Tasks |-> 0]
    /\ implDispatch = [task \in Tasks |-> 0]
    /\ specDispatch = [task \in Tasks |-> 0]
    /\ now = 0
    /\ steps = 0

CpuIdle(cpu) == \A task \in Tasks : owner[task] # R(cpu)

Tick ==
    /\ steps < MaxSteps
    /\ now < MaxTime
    /\ now' = now + 1
    /\ steps' = steps + 1
    /\ UNCHANGED <<owner, implQueuedAt, specQueuedAt, implWait,
                    specWait, implDispatch, specDispatch>>

Wake(task, cpu) ==
    /\ steps < MaxSteps
    /\ owner[task] = "Blocked"
    /\ owner' = [owner EXCEPT ![task] = Q(cpu)]
    /\ specQueuedAt' = [specQueuedAt EXCEPT ![task] = now]
    /\ implQueuedAt' =
          [implQueuedAt EXCEPT ![task] =
              IF Mode = "CountBlocked" /\ implQueuedAt[task] # None
              THEN implQueuedAt[task]
              ELSE now]
    /\ steps' = steps + 1
    /\ UNCHANGED <<implWait, specWait, implDispatch, specDispatch, now>>

Duplicate(task) ==
    /\ steps < MaxSteps
    /\ IsQueued(owner[task])
    /\ implQueuedAt' =
          IF Mode = "ResetDuplicate"
          THEN [implQueuedAt EXCEPT ![task] = now]
          ELSE implQueuedAt
    /\ steps' = steps + 1
    /\ UNCHANGED <<owner, specQueuedAt, implWait, specWait,
                    implDispatch, specDispatch, now>>

Dispatch(task, cpu) ==
    /\ steps < MaxSteps
    /\ owner[task] = Q(cpu)
    /\ CpuIdle(cpu)
    /\ implQueuedAt[task] # None
    /\ specQueuedAt[task] # None
    /\ owner' = [owner EXCEPT ![task] = R(cpu)]
    /\ implWait' =
          [implWait EXCEPT ![task] = @ + (now - implQueuedAt[task])]
    /\ specWait' =
          [specWait EXCEPT ![task] = @ + (now - specQueuedAt[task])]
    /\ implQueuedAt' = [implQueuedAt EXCEPT ![task] = None]
    /\ specQueuedAt' = [specQueuedAt EXCEPT ![task] = None]
    /\ implDispatch' = [implDispatch EXCEPT ![task] = @ + 1]
    /\ specDispatch' = [specDispatch EXCEPT ![task] = @ + 1]
    /\ steps' = steps + 1
    /\ UNCHANGED now

Preempt(task, cpu) ==
    /\ steps < MaxSteps
    /\ owner[task] = R(cpu)
    /\ owner' = [owner EXCEPT ![task] = Q(cpu)]
    /\ implQueuedAt' = [implQueuedAt EXCEPT ![task] = now]
    /\ specQueuedAt' = [specQueuedAt EXCEPT ![task] = now]
    /\ steps' = steps + 1
    /\ UNCHANGED <<implWait, specWait, implDispatch, specDispatch, now>>

Block(task, cpu) ==
    /\ steps < MaxSteps
    /\ owner[task] = R(cpu)
    /\ owner' = [owner EXCEPT ![task] = "Blocked"]
    /\ implQueuedAt' =
          [implQueuedAt EXCEPT ![task] =
              IF Mode = "CountBlocked" THEN now ELSE None]
    /\ specQueuedAt' = [specQueuedAt EXCEPT ![task] = None]
    /\ steps' = steps + 1
    /\ UNCHANGED <<implWait, specWait, implDispatch, specDispatch, now>>

Migrate(task, fromCpu, toCpu) ==
    /\ steps < MaxSteps
    /\ fromCpu # toCpu
    /\ owner[task] = R(fromCpu)
    /\ CpuIdle(toCpu)
    /\ owner' = [owner EXCEPT ![task] = Q(toCpu)]
    /\ implQueuedAt' = [implQueuedAt EXCEPT ![task] = now]
    /\ specQueuedAt' = [specQueuedAt EXCEPT ![task] = now]
    /\ steps' = steps + 1
    /\ UNCHANGED <<implWait, specWait, implDispatch, specDispatch, now>>

Next ==
    Tick
    \/ (\E task \in Tasks, cpu \in Cpus : Wake(task, cpu))
    \/ (\E task \in Tasks : Duplicate(task))
    \/ (\E task \in Tasks, cpu \in Cpus : Dispatch(task, cpu))
    \/ (\E task \in Tasks, cpu \in Cpus : Preempt(task, cpu))
    \/ (\E task \in Tasks, cpu \in Cpus : Block(task, cpu))
    \/ (\E task \in Tasks, fromCpu \in Cpus, toCpu \in Cpus :
           Migrate(task, fromCpu, toCpu))

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ owner \in [Tasks -> Owners]
    /\ implQueuedAt \in [Tasks -> 0..None]
    /\ specQueuedAt \in [Tasks -> 0..None]
    /\ implWait \in [Tasks -> Nat]
    /\ specWait \in [Tasks -> Nat]
    /\ implDispatch \in [Tasks -> Nat]
    /\ specDispatch \in [Tasks -> Nat]
    /\ now \in 0..MaxTime
    /\ steps \in 0..MaxSteps

AtMostOneRunning ==
    \A cpu \in Cpus : Cardinality({task \in Tasks : owner[task] = R(cpu)}) <= 1

LedgerMatches ==
    /\ implQueuedAt = specQueuedAt
    /\ implWait = specWait
    /\ implDispatch = specDispatch
=============================================================================
