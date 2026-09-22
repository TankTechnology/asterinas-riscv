---------------------- MODULE RiscvCpuLocalMigration ----------------------
\* SPDX-License-Identifier: MPL-2.0
\* Copyright (c) 2026 The Asterinas Authors.
EXTENDS FiniteSets, Naturals

CONSTANT Mode
ASSUME Mode \in {"Correct", "RestoreStaleGp", "SwitchWhileBorrowed"}

Cpus == {"cpu0", "cpu1"}
Tasks == {"task0", "task1", "task2"}
NoCpu == "none"
Phases == {"Runnable", "Kernel", "User"}

VARIABLES taskCpu, phase, gpOwner, savedGp, targetCpu, irqEnabled,
          cacheBorrowers, switchPending, migrationPending
vars == <<taskCpu, phase, gpOwner, savedGp, targetCpu, irqEnabled,
          cacheBorrowers, switchPending, migrationPending>>

Running(task) == taskCpu[task] # NoCpu
CpuFree(cpu) == \A task \in Tasks : taskCpu[task] # cpu
BorrowedCaches(task) == {cpu \in Cpus : task \in cacheBorrowers[cpu]}

Init ==
    /\ taskCpu = [task \in Tasks |->
        CASE task = "task0" -> "cpu0"
          [] task = "task1" -> "cpu1"
          [] OTHER -> NoCpu]
    /\ phase = [task \in Tasks |->
        IF task = "task2" THEN "Runnable" ELSE "Kernel"]
    /\ gpOwner = [task \in Tasks |->
        IF task = "task1" THEN "cpu1" ELSE "cpu0"]
    /\ savedGp = gpOwner
    /\ targetCpu = [task \in Tasks |->
        IF task = "task1" THEN "cpu1" ELSE "cpu0"]
    /\ irqEnabled = [cpu \in Cpus |-> TRUE]
    /\ cacheBorrowers = [cpu \in Cpus |-> {}]
    /\ switchPending = [task \in Tasks |-> task = "task2"]
    /\ migrationPending = [task \in Tasks |-> FALSE]

EnterUser(task) ==
    /\ Running(task)
    /\ phase[task] = "Kernel"
    /\ BorrowedCaches(task) = {}
    /\ irqEnabled[taskCpu[task]]
    /\ phase' = [phase EXCEPT ![task] = "User"]
    /\ UNCHANGED <<taskCpu, gpOwner, savedGp, targetCpu, irqEnabled,
                    cacheBorrowers, switchPending, migrationPending>>

EnterKernel(task) ==
    /\ Running(task)
    /\ phase[task] = "User"
    /\ phase' = [phase EXCEPT ![task] = "Kernel"]
    \* A user trap restores the CPU-local identity for the CPU that took it.
    /\ gpOwner' = [gpOwner EXCEPT ![task] = taskCpu[task]]
    /\ UNCHANGED <<taskCpu, savedGp, targetCpu, irqEnabled,
                    cacheBorrowers, switchPending, migrationPending>>

BeginLocalAlloc(task) ==
    /\ Running(task)
    /\ phase[task] = "Kernel"
    /\ BorrowedCaches(task) = {}
    /\ irqEnabled[taskCpu[task]]
    /\ irqEnabled' = [irqEnabled EXCEPT ![taskCpu[task]] = FALSE]
    \* The implementation selects the cache through CPU-local addressing.
    /\ cacheBorrowers' =
        [cacheBorrowers EXCEPT ![gpOwner[task]] = @ \cup {task}]
    /\ UNCHANGED <<taskCpu, phase, gpOwner, savedGp, targetCpu,
                    switchPending, migrationPending>>

EndLocalAlloc(task) ==
    /\ Running(task)
    /\ phase[task] = "Kernel"
    /\ task \in cacheBorrowers[gpOwner[task]]
    /\ ~irqEnabled[taskCpu[task]]
    /\ cacheBorrowers' =
        [cacheBorrowers EXCEPT ![gpOwner[task]] = @ \ {task}]
    /\ irqEnabled' = [irqEnabled EXCEPT ![taskCpu[task]] = TRUE]
    /\ UNCHANGED <<taskCpu, phase, gpOwner, savedGp, targetCpu,
                    switchPending, migrationPending>>

Deschedule(task) ==
    /\ Running(task)
    /\ phase[task] = "Kernel"
    /\ (Mode = "SwitchWhileBorrowed" \/ BorrowedCaches(task) = {})
    /\ savedGp' = [savedGp EXCEPT ![task] = gpOwner[task]]
    /\ irqEnabled' = [irqEnabled EXCEPT ![taskCpu[task]] = TRUE]
    /\ taskCpu' = [taskCpu EXCEPT ![task] = NoCpu]
    /\ phase' = [phase EXCEPT ![task] = "Runnable"]
    /\ switchPending' = [switchPending EXCEPT ![task] = TRUE]
    /\ UNCHANGED <<gpOwner, targetCpu, cacheBorrowers, migrationPending>>

Migrate(task, cpu) ==
    /\ taskCpu[task] = NoCpu
    /\ phase[task] = "Runnable"
    /\ switchPending[task]
    /\ cpu # targetCpu[task]
    /\ targetCpu' = [targetCpu EXCEPT ![task] = cpu]
    /\ migrationPending' = [migrationPending EXCEPT ![task] = TRUE]
    /\ UNCHANGED <<taskCpu, phase, gpOwner, savedGp, irqEnabled,
                    cacheBorrowers, switchPending>>

Resume(task) ==
    /\ taskCpu[task] = NoCpu
    /\ phase[task] = "Runnable"
    /\ switchPending[task]
    /\ CpuFree(targetCpu[task])
    /\ irqEnabled[targetCpu[task]]
    /\ taskCpu' = [taskCpu EXCEPT ![task] = targetCpu[task]]
    /\ phase' = [phase EXCEPT ![task] = "Kernel"]
    /\ gpOwner' = [gpOwner EXCEPT
        ![task] = IF Mode = "RestoreStaleGp"
                  THEN savedGp[task]
                  ELSE targetCpu[task]]
    /\ switchPending' = [switchPending EXCEPT ![task] = FALSE]
    /\ migrationPending' = [migrationPending EXCEPT ![task] = FALSE]
    /\ UNCHANGED <<savedGp, targetCpu, irqEnabled, cacheBorrowers>>

Next ==
    \/ \E task \in Tasks : EnterUser(task)
    \/ \E task \in Tasks : EnterKernel(task)
    \/ \E task \in Tasks : BeginLocalAlloc(task)
    \/ \E task \in Tasks : EndLocalAlloc(task)
    \/ \E task \in Tasks : Deschedule(task)
    \/ \E task \in Tasks, cpu \in Cpus : Migrate(task, cpu)
    \/ \E task \in Tasks : Resume(task)

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ taskCpu \in [Tasks -> Cpus \cup {NoCpu}]
    /\ phase \in [Tasks -> Phases]
    /\ gpOwner \in [Tasks -> Cpus]
    /\ savedGp \in [Tasks -> Cpus]
    /\ targetCpu \in [Tasks -> Cpus]
    /\ irqEnabled \in [Cpus -> BOOLEAN]
    /\ cacheBorrowers \in [Cpus -> SUBSET Tasks]
    /\ switchPending \in [Tasks -> BOOLEAN]
    /\ migrationPending \in [Tasks -> BOOLEAN]

UniqueCpuOccupancy ==
    \A left, right \in Tasks :
        left # right => taskCpu[left] = NoCpu \/ taskCpu[left] # taskCpu[right]

GpMatchesRunningCpu ==
    \A task \in Tasks :
        (Running(task) /\ phase[task] = "Kernel")
        => gpOwner[task] = taskCpu[task]

BorrowHasLocalIrqsDisabled ==
    \A cpu \in Cpus :
        \A task \in cacheBorrowers[cpu] :
            Running(task) /\ ~irqEnabled[taskCpu[task]]

BorrowOwnerRunsOnCacheCpu ==
    \A cpu \in Cpus :
        \A task \in cacheBorrowers[cpu] : taskCpu[task] = cpu

NoTaskOwnsTwoCaches ==
    \A task \in Tasks : Cardinality(BorrowedCaches(task)) <= 1

NoBorrowConflict ==
    \A cpu \in Cpus : Cardinality(cacheBorrowers[cpu]) <= 1

NoSwitchWithLiveBorrow ==
    \A task \in Tasks : taskCpu[task] = NoCpu => BorrowedCaches(task) = {}
=============================================================================
