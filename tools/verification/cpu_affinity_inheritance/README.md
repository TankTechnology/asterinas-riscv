# CPU-affinity inheritance model

This bounded TLA+ model checks the Linux-compatible rule that a child starts
with an owned snapshot of its creating thread's CPU-affinity mask.

Run from the repository root:

```bash
bash tools/verification/cpu_affinity_inheritance/run.sh
```

The runner reuses the repository's cached TLC 1.7.4 JAR after verifying its
SHA-256. It fails if that artifact is absent or mismatched and never downloads
anything. Logs, the required negative-control trace, Java version, and state
counts are written below `target/cpu-affinity-inheritance-model/`.

## Code correspondence

- `parentMask` represents `Thread::cpu_affinity` on the thread executing clone.
- `cloneSnapshot` represents the `AtomicCpuSet::load` performed in
  `clone_child_task` or `clone_child_process`.
- `childAtClone` represents the owned `CpuSet` passed through
  `PosixThreadBuilder` to `Thread::new`.
- `ResetToAll` represents the former unconditional `CpuSet::new_full()` in
  `PosixThreadBuilder::build`; `InheritSnapshot` represents the corrected copy.
- `ChangeParent` and `ChangeChild` show that later per-thread affinity changes
  do not alter the recorded value at the clone boundary.

`ChildInheritedSnapshot` requires `childAtClone = cloneSnapshot` after clone.
The runner first requires `ResetToAll` to violate it for a singleton parent
mask, then exhaustively checks the corrected case over all nonempty masks of a
two-CPU system.

## Limits

This is exhaustive checking of the stated finite abstraction, not a proof of
the Rust implementation or scheduler. It does not model CPU placement,
fairness, migration, cpusets, CPU hotplug, or weak memory. In particular,
`AtomicIdSet::load` is documented as non-atomic across multiple `AtomicU64`
words. Current four-CPU qualification uses one word; a system with more than
64 CPUs needs a separately synchronized snapshot design and model.
