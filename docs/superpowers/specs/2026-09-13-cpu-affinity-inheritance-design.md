# CPU-affinity inheritance design

## Goal

Make POSIX children inherit the creating thread's CPU-affinity mask, matching
Linux for both process creation (`fork`/non-thread `clone`) and thread creation
(`pthread_create`/`CLONE_THREAD`). Keep initial process creation on the existing
all-CPU default and do not change scheduler selection policy.

## Kernel design

`PosixThreadBuilder` will carry an explicit `CpuSet`. Its default remains
`CpuSet::new_full()` so the init process keeps current behavior. A builder setter
will allow clone call sites to provide a snapshot of the creating `Thread`'s
mask. Both `clone_child_task` and `clone_child_process` will load that snapshot
and pass it to the builder.

The child receives an owned `CpuSet`, not a shared reference. Subsequent changes
to either parent or child therefore remain per-thread, as required by Linux.
The current four-CPU configurations fit in one `AtomicU64`, so the snapshot is
atomic. The existing multiword `AtomicIdSet` limitation for systems above 64
CPUs is recorded as a separate concurrency limitation rather than hidden by
this compatibility fix.

## Verification

Extend the maintained CPU-affinity regression to:

1. save the original mask and require at least two allowed CPUs;
2. pin the caller to one CPU and read the complete mask back;
3. check exact mask equality in a `fork` child;
4. check exact mask equality in a default-attribute pthread child;
5. reap/join children and restore the original mask.

The test is deterministic and does not infer affinity from timing or observed
CPU placement. Single-CPU systems report a skip because a full mask and a
singleton mask are indistinguishable there.

A bounded TLA+ model will state the invariant `childMask = cloneSnapshot` and
include the current `childMask := AllCpus` behavior as a required failing
negative control. The model documents the refinement boundary: it checks mask
copy semantics, not Rust implementation, weak memory, scheduler fairness, or
systems with multiword masks.

## Scope boundaries

This change does not address remote-target immediate migration in
`sched_setaffinity`, cross-CPU virtual-runtime normalization, or Firefox's
remaining latency. Those require separate evidence and tests.
