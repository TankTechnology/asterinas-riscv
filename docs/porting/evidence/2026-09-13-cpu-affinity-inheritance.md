# CPU-affinity inheritance qualification

Date: 2026-09-13

## Result

Asterinas previously replaced the creating thread's CPU-affinity mask with an
all-CPU mask in `PosixThreadBuilder::build`. Consequently, both a process child
created through `fork()` and a thread child created through `pthread_create()`
lost a singleton mask. The clone paths now take one owned snapshot of the
creating thread's mask and pass it through the builder to `Thread::new`.

This is a Linux compatibility defect, not a scheduler-timing inference. The
Linux man-pages state that affinity is per-thread, a `fork()` child inherits its
parent's mask, and a new pthread inherits a copy of its creator's mask:

- <https://man7.org/linux/man-pages/man2/sched_setaffinity.2.html>
- <https://man7.org/linux/man-pages/man3/pthread_setaffinity_np.3.html>
- <https://man7.org/linux/man-pages/man3/pthread_create.3.html>

## Deterministic regression

`test/initramfs/src/regression/process/cpu_affinity/inheritance.c` saves the
complete original mask, pins the caller to one allowed CPU, and compares the
complete masks of the parent, a `fork()` child, and a default-attribute pthread.
It does not infer affinity from `sched_getcpu()`, timing, or CPU placement. It
joins/reaps both children and restores the original mask. A system exposing
fewer than two allowed CPUs exits with skip status 77.

The same static RISC-V test binary and diagnostic initramfs produced this RED
result on the unmodified SMP=4 kernel:

```text
CPU_AFFINITY_INHERIT kind=parent expected_count=1 observed_count=1 pass=1
CPU_AFFINITY_INHERIT kind=fork expected_count=1 observed_count=4 pass=0
CPU_AFFINITY_INHERIT kind=pthread expected_count=1 observed_count=4 pass=0
CPU_AFFINITY_EXIT=1
```

After the kernel change, the same QEMU setup produced GREEN:

```text
CPU_AFFINITY_INHERIT kind=parent expected_count=1 observed_count=1 pass=1
CPU_AFFINITY_INHERIT kind=fork expected_count=1 observed_count=1 pass=1
CPU_AFFINITY_INHERIT kind=pthread expected_count=1 observed_count=1 pass=1
CPU_AFFINITY_EXIT=0
```

The native Linux control passed the same three exact-mask assertions. The
RISC-V regression package was rebuilt offline, and the SMP=4 kernel compiled in
the persistent development container without recreating the image or fetching
dependencies.

## Finite-state check

`tools/verification/cpu_affinity_inheritance/run.sh` runs a two-CPU TLA+ model
against every nonempty parent mask, including independent changes after clone.
It requires the former reset-to-all behavior to fail as a negative control and
then checks the snapshot-copy design exhaustively:

```text
ResetToAll       exit=12  4 states generated, 4 distinct states found
InheritSnapshot  exit=0   162 states generated, 54 distinct states found
PASS: expected reset-to-all counterexample and corrected finite model.
```

The negative trace contains a singleton `{"cpu0"}` snapshot followed by the
old child's `{"cpu0", "cpu1"}` mask. The corrected finite model has no invariant
violation.

## Physical qualification

The release kernel from commit `01f04a3fc` was staged as a new temporary file
on the RockOS partition. No installed boot selector or Debian root partition
was overwritten.

- kernel SHA-256:
  `ef3169f1725edab77ed1840caf7bac4269e0af24bf16530944a8147f3f84c356`
- diagnostic initramfs SHA-256:
  `1ff9016eb1942b49774cbb88134e8501ad49cbe5a5a53e6df273b80bf642aeeb`
- bounded run time: 20.94 seconds, followed by automatic return to U-Boot
- scheduler handoff checks: 20 starts and 20 successful completions

The physical four-core Megrez result was:

```text
AFFINITY_INHERIT kind=parent expected_count=1 observed_count=1 pass=1
AFFINITY_INHERIT kind=fork expected_count=1 observed_count=1 pass=1
AFFINITY_INHERIT kind=pthread expected_count=1 observed_count=1 pass=1
AFFINITY_EXIT=0
```

RockOS was then selected explicitly and became reachable over SSH with kernel
`6.6.87`; its boot ID changed from
`649b2ca3-dc67-4503-b85d-6342b361281d` to
`4d3d7852-d46b-4c58-9b62-e7647cba76af`. The installed Asterinas and vendor
menu hashes remained respectively
`02280720efe7a1ad0ac084cdc20429406631e12d2e16f05638544bab0883fb26`
and `eb5f39a6e2db71ccc93ae005c488fd9f7ef353e75426e5a51e8923a9cbf2ebc5`.

## Concurrency boundary

The child receives an owned mask snapshot, so later parent and child changes do
not alias. On the qualified four-CPU systems the mask fits in one `AtomicU64`.
The underlying `AtomicIdSet` documents that a load spanning several words is
not one atomic snapshot; systems with more than 64 CPUs therefore still need a
separately synchronized design and model. This finite check also does not prove
weak-memory behavior, scheduling fairness, CPU hotplug, or immediate migration
when another thread's affinity is changed.

This fix removes one concrete source of unpinned Firefox worker threads. It is
not, by itself, evidence that all remaining Firefox latency is fixed.
