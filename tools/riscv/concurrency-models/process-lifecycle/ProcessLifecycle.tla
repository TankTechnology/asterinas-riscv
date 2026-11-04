--------------------------- MODULE ProcessLifecycle ---------------------------
(***************************************************************************)
(*  A TLA+ model of the process lifecycle around `fork`, `exit` and         *)
(*  `wait`: who may reap whom, and when the exit status exists.             *)
(*                                                                          *)
(*  Real code:                                                              *)
(*    kernel/src/process/wait.rs            (wait, reap_zombie_child)        *)
(*    kernel/src/process/status.rs          (ProcessStatus: zombie, exit code)*)
(*    kernel/src/process/posix_thread/exit.rs (exit commits the status)      *)
(*                                                                          *)
(*  POSIX rules the model encodes:                                          *)
(*    * only a process's parent may reap it (after the parent exits, the     *)
(*      child is re-parented to init, which may then reap it);              *)
(*    * a process is a zombie before it is reaped, and its exit status is    *)
(*      published before the parent can observe it as reapable;             *)
(*    * a zombie stays until reaped, and a process is reaped at most once.   *)
(*                                                                          *)
(*  `BUGGY` selects pre-fix behaviours so the invariants can be shown to     *)
(*  catch them:                                                             *)
(*    "none"        the rules above;                                        *)
(*    "reap_running" a wait may reap a child that has not exited;           *)
(*    "no_reparent"  exiting does not re-parent children to init;           *)
(*    "reap_anyone"  any running process may reap any zombie.               *)
(***************************************************************************)

EXTENDS Naturals

CONSTANTS Proc, BUGGY

ASSUME "init" ∈ Proc
ASSUME BUGGY ∈ {"none", "reap_running", "no_reparent", "reap_anyone"}

VARIABLES running, zombies, gone, parent, status, reapedBy, parentAtReap

vars == <<running, zombies, gone, parent, status, reapedBy, parentAtReap>>

Init ==
    /\ running = {"init"}
    /\ zombies = {}
    /\ gone = {}
    /\ parent = [c ∈ Proc |-> "init"]
    /\ status = [c ∈ Proc |-> "none"]
    /\ reapedBy = [c ∈ Proc |-> "none"]
    /\ parentAtReap = [c ∈ Proc |-> "none"]

(***************************************************************************)
(* `fork`: a running process creates a child.                              *)
(***************************************************************************)
Fork(p) ==
    /\ p ∈ running
    /\ ∃ c ∈ (Proc \ (running ∪ zombies ∪ gone)) \ {"init"}:
         /\ running' = running ∪ {c}
         /\ parent' = [parent EXCEPT ![c] = p]
         /\ UNCHANGED <<zombies, gone, status, reapedBy, parentAtReap>>

(***************************************************************************)
(* `exit`: the process becomes a zombie and its status is published.        *)
(* Its children are re-parented to init.                                   *)
(***************************************************************************)
Exit(p) ==
    /\ p ∈ running
    /\ p /= "init"
    /\ running' = running \ {p}
    /\ zombies' = zombies ∪ {p}
    /\ status' = [status EXCEPT ![p] = "exited"]
    /\ parent' = IF BUGGY = "no_reparent"
                   THEN parent
                   ELSE [c ∈ Proc |->
                            IF (c ∈ running ∪ zombies) /\ parent[c] = p
                            THEN "init" ELSE parent[c]]
    /\ UNCHANGED <<gone, reapedBy, parentAtReap>>

(***************************************************************************)
(* `wait`: the parent reaps a child.                                       *)
(***************************************************************************)
Reapable ==
    IF BUGGY = "reap_running" THEN zombies ∪ (running \ {"init"}) ELSE zombies

Reap(p, c) ==
    /\ p ∈ running
    /\ c ∈ Reapable
    /\ (BUGGY /= "reap_anyone" => parent[c] = p)
    /\ (BUGGY = "reap_anyone" => c /= p)
    /\ zombies' = zombies \ {c}
    /\ gone' = gone ∪ {c}
    /\ reapedBy' = [reapedBy EXCEPT ![c] = p]
    /\ parentAtReap' = [parentAtReap EXCEPT ![c] = parent[c]]
    /\ UNCHANGED <<running, parent, status>>

\* The system can also be quiescent: init has nothing to reap and no process
\* is forking or exiting. This is not a deadlock, so it is an explicit
\* stuttering action rather than TLC's deadlock report.
Idle == UNCHANGED vars

Next ==
    \/ (∃ p ∈ running : Fork(p))
    \/ (∃ p ∈ running : Exit(p))
    \/ (∃ p ∈ running, c ∈ Reapable : Reap(p, c))
    \/ Idle

Spec == Init /\ [][Next]_vars

(***************************************************************************)
(* Safety                                                                   *)
(***************************************************************************)

\* A reaped process has an exit status, i.e. it exited before it was reaped.
ReapRequiresExit ==
    gone ⊆ {c ∈ Proc : status[c] = "exited"}

\* Only the process that was the parent at reap time could reap a child.
OnlyParentReaps ==
    ∀ c ∈ gone : reapedBy[c] = parentAtReap[c]

\* A live process or a zombie has a living parent (init is always running).
NoOrphan ==
    ∀ c ∈ (running ∪ zombies) \ {"init"} : parent[c] ∈ running

\* A process is in exactly one state.
Disjoint ==
    /\ running ∩ zombies = {}
    /\ running ∩ gone = {}
    /\ zombies ∩ gone = {}

\* init never exits: it is the reaper of last resort.
InitLives == "init" ∈ running

=============================================================================
