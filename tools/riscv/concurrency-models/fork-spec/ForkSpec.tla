------------------------------- MODULE ForkSpec -------------------------------
(***************************************************************************)
(*  A specification-level model of `fork`: what POSIX promises, expressed    *)
(*  over the same structures the kernel uses (frames, per-process mappings,  *)
(*  reference counts, file descriptors, process states).                     *)
(*                                                                          *)
(*  Properties checked:                                                     *)
(*                                                                          *)
(*    CoWIsolation       no frame is mapped writable in two processes, so a   *)
(*                       write in one process is never visible in another;   *)
(*    RefsAccurate       the frame reference count equals the number of       *)
(*                       mappings of that frame;                             *)
(*    ReapRequiresExit   a reaped process exited before it was reaped;        *)
(*    OnlyParentReaps    a child is reaped by its parent (or init after       *)
(*                       re-parenting);                                      *)
(*    NoOrphan           every live process has a living parent;             *)
(*    InitLives, Disjoint                                                    *)
(*                                                                          *)
(*  This is a model of the DESIGN, not a refinement proof of the Rust code.  *)
(*  The gap between the two is written down in                                 *)
(*  docs/porting/2026-11-04-fork-proof-scope.md.                             *)
(***************************************************************************)

EXTENDS Naturals, FiniteSets

CONSTANTS Proc, Addr, Frames, Values, BUGGY

ASSUME "init" ∈ Proc
ASSUME Addr # {}
ASSUME Frames # {}
ASSUME BUGGY ∈ {"none", "no_ref_check"}

VARIABLES
    running, zombies, gone, parent, status,
    map, refs, frameval,
    reapedBy, parentAtReap

vars == <<running, zombies, gone, parent, status, map, refs, frameval,
          reapedBy, parentAtReap>>

\* No mapping is represented by frame 0, which is never allocated. A record
\* (rather than a string) keeps every value the same kind, which TLC needs to
\* fingerprint states.
None == [frame |-> 0, writable |-> FALSE]

Mapping(f) == [frame |-> f, writable |-> FALSE]

MappingsOf(p, f) == Cardinality({a ∈ Addr : map[p][a].frame = f})

Init ==
    /\ running = {"init", "p"}
    /\ zombies = {}
    /\ gone = {}
    /\ parent = [c ∈ Proc |-> "init"]
    /\ status = [c ∈ Proc |-> "none"]
    /\ map = [c ∈ Proc |->
                [a ∈ Addr |-> IF c = "p" THEN Mapping(1 + ((a - 1) % 2)) ELSE None]]
    /\ refs = [f ∈ Frames |->
                 IF f ∈ {1, 2} THEN 1 ELSE 0]
    /\ frameval = [f ∈ Frames |-> 0]
    /\ reapedBy = [c ∈ Proc |-> "none"]
    /\ parentAtReap = [c ∈ Proc |-> "none"]

(***************************************************************************)
(* fork: the child gets a private copy of the mappings, all read-only, and   *)
(* every mapped frame gains a reference.                                    *)
(***************************************************************************)
Fork(p) ==
    /\ p ∈ running
    /\ ∃ c ∈ (Proc \ (running ∪ zombies ∪ gone)) \ {"init"}:
         /\ running' = running ∪ {c}
         /\ parent' = [parent EXCEPT ![c] = p]
         /\ map' = [map EXCEPT
                      ![c] = [a ∈ Addr |-> IF map[p][a].frame = 0 THEN None
                                            ELSE Mapping(map[p][a].frame)],
                      ![p] = [a ∈ Addr |-> IF map[p][a].frame = 0 THEN None
                                            ELSE Mapping(map[p][a].frame)]]
         /\ refs' = [f ∈ Frames |-> refs[f] + MappingsOf(p, f)]
         /\ UNCHANGED <<zombies, gone, status, frameval, reapedBy, parentAtReap>>

(***************************************************************************)
(* A write fault: copy unless this mapping is the frame's only reference.    *)
(***************************************************************************)
WriteFault(p) ==
    /\ p ∈ running
    /\ ∃ a ∈ Addr:
         /\ map[p][a].frame # 0
         /\ map[p][a].writable = FALSE
         /\ LET f == map[p][a].frame
            IN IF BUGGY = "no_ref_check" \/ refs[f] = 1
               THEN
                   /\ map' = [map EXCEPT ![p][a] = [frame |-> f, writable |-> TRUE]]
                   /\ UNCHANGED <<refs, frameval, running, zombies, gone, parent,
                                   status, reapedBy, parentAtReap>>
               ELSE
                   /\ ∃ g ∈ Frames:
                        /\ refs[g] = 0
                        /\ g # f
                        /\ refs' = [refs EXCEPT ![f] = refs[f] - 1, ![g] = 1]
                        /\ frameval' = [frameval EXCEPT ![g] = frameval[f]]
                        /\ map' = [map EXCEPT ![p][a] = [frame |-> g, writable |-> TRUE]]
                        /\ UNCHANGED <<running, zombies, gone, parent, status,
                                        reapedBy, parentAtReap>>

(***************************************************************************)
(* A write: only allowed through a writable mapping, and CoW guarantees that *)
(* the frame is private, so nobody else can observe the new value.           *)
(***************************************************************************)
Write(p) ==
    /\ p ∈ running
    /\ ∃ a ∈ Addr, v ∈ Values:
         /\ map[p][a].frame # 0
         /\ map[p][a].writable
         /\ frameval' = [frameval EXCEPT ![map[p][a].frame] = v]
         /\ UNCHANGED <<running, zombies, gone, parent, status, map, refs,
                        reapedBy, parentAtReap>>

(***************************************************************************)
(* exit: drop the address space, become a zombie, publish the status, and    *)
(* re-parent children to init.                                              *)
(***************************************************************************)
Exit(p) ==
    /\ p ∈ running
    /\ p /= "init"
    /\ running' = running \ {p}
    /\ zombies' = zombies ∪ {p}
    /\ status' = [status EXCEPT ![p] = "exited"]
    /\ map' = [map EXCEPT ![p] = [a ∈ Addr |-> None]]
    /\ refs' = [f ∈ Frames |-> refs[f] - MappingsOf(p, f)]
    /\ parent' = IF BUGGY = "no_ref_check" \/ TRUE
                   THEN [c ∈ Proc |->
                            IF (c ∈ running ∪ zombies) /\ parent[c] = p
                            THEN "init" ELSE parent[c]]
                   ELSE parent
    /\ UNCHANGED <<gone, frameval, reapedBy, parentAtReap>>

Reap(p, c) ==
    /\ p ∈ running
    /\ c ∈ zombies
    /\ parent[c] = p
    /\ zombies' = zombies \ {c}
    /\ gone' = gone ∪ {c}
    /\ reapedBy' = [reapedBy EXCEPT ![c] = p]
    /\ parentAtReap' = [parentAtReap EXCEPT ![c] = parent[c]]
    /\ UNCHANGED <<running, parent, status, map, refs, frameval>>

Idle == UNCHANGED vars

Next ==
    \/ (∃ p ∈ running : Fork(p))
    \/ (∃ p ∈ running : WriteFault(p))
    \/ (∃ p ∈ running : Write(p))
    \/ (∃ p ∈ running : Exit(p))
    \/ (∃ p ∈ running, c ∈ zombies : Reap(p, c))
    \/ Idle

Spec == Init /\ [][Next]_vars

(***************************************************************************)
(* Safety                                                                   *)
(***************************************************************************)

\* If a frame is mapped in two processes, neither mapping may be writable.
CoWIsolation ==
    ∀ p, q ∈ running :
        ∀ a, b ∈ Addr :
            /\ p /= q
            /\ map[p][a].frame # 0
            /\ map[q][b].frame # 0
            /\ map[p][a].frame = map[q][b].frame
            => ~(map[p][a].writable)

\* The reference count is exactly the number of mappings.
RefsAccurate ==
    ∀ f ∈ Frames :
        refs[f] = Cardinality({<<p, a>> ∈ running × Addr : map[p][a].frame = f})

ReapRequiresExit ==
    gone ⊆ {c ∈ Proc : status[c] = "exited"}

OnlyParentReaps ==
    ∀ c ∈ gone : reapedBy[c] = parentAtReap[c]

NoOrphan ==
    ∀ c ∈ (running ∪ zombies) \ {"init"} : parent[c] ∈ running

Disjoint ==
    /\ running ∩ zombies = {}
    /\ running ∩ gone = {}
    /\ zombies ∩ gone = {}

InitLives == "init" ∈ running

=============================================================================
