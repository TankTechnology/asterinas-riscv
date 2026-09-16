---------------------- MODULE CpuAffinityInheritance ----------------------
\* SPDX-License-Identifier: MPL-2.0
\* Copyright (c) 2026 The Asterinas Authors.
EXTENDS FiniteSets

CONSTANT Mode
ASSUME Mode \in {"InheritSnapshot", "ResetToAll"}

Cpus == {"cpu0", "cpu1"}
NonEmptyMasks == (SUBSET Cpus) \ {{}}

VARIABLES parentMask, childMask, cloneSnapshot, childAtClone, cloned,
          parentChanged, childChanged
vars == <<parentMask, childMask, cloneSnapshot, childAtClone, cloned,
          parentChanged, childChanged>>

Init ==
    /\ parentMask \in NonEmptyMasks
    /\ childMask = {}
    /\ cloneSnapshot = {}
    /\ childAtClone = {}
    /\ cloned = FALSE
    /\ parentChanged = FALSE
    /\ childChanged = FALSE

Clone ==
    /\ ~cloned
    /\ cloneSnapshot' = parentMask
    /\ LET inherited == IF Mode = "ResetToAll" THEN Cpus ELSE parentMask
       IN /\ childMask' = inherited
          /\ childAtClone' = inherited
    /\ cloned' = TRUE
    /\ UNCHANGED <<parentMask, parentChanged, childChanged>>

ChangeParent(newParentMask) ==
    /\ ~parentChanged
    /\ newParentMask \in NonEmptyMasks
    /\ parentMask' = newParentMask
    /\ parentChanged' = TRUE
    /\ UNCHANGED <<childMask, cloneSnapshot, childAtClone, cloned,
                    childChanged>>

ChangeChild(newChildMask) ==
    /\ cloned
    /\ ~childChanged
    /\ newChildMask \in NonEmptyMasks
    /\ childMask' = newChildMask
    /\ childChanged' = TRUE
    /\ UNCHANGED <<parentMask, cloneSnapshot, childAtClone, cloned,
                    parentChanged>>

Next == Clone
        \/ \E newParentMask \in NonEmptyMasks : ChangeParent(newParentMask)
        \/ \E newChildMask \in NonEmptyMasks : ChangeChild(newChildMask)

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ parentMask \in NonEmptyMasks
    /\ childMask \subseteq Cpus
    /\ cloneSnapshot \subseteq Cpus
    /\ childAtClone \subseteq Cpus
    /\ cloned \in BOOLEAN
    /\ parentChanged \in BOOLEAN
    /\ childChanged \in BOOLEAN

ChildInheritedSnapshot == cloned => childAtClone = cloneSnapshot
=============================================================================
