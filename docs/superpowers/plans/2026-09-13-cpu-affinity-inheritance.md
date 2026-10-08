# CPU Affinity Inheritance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `fork` and `pthread_create` children inherit an owned snapshot of the creating thread's CPU-affinity mask.

**Architecture:** Preserve `PosixThreadBuilder`'s all-CPU default for init, add an explicit affinity setter, and call it from both process and thread clone paths. Verify the Linux ABI through a deterministic C regression and the abstract copy invariant through a bounded offline TLA+ model with a failing negative control.

**Tech Stack:** Safe Rust kernel, C/pthreads initramfs regression, RISC-V SMP=4 QEMU, TLA+ TLC 1.7.4.

---

### Task 1: Add the failing Linux-ABI regression

**Files:**
- Create: `test/initramfs/src/regression/process/cpu_affinity/inheritance.c`
- Modify: `test/initramfs/src/regression/process/cpu_affinity/Makefile`
- Modify: `test/initramfs/src/regression/process/cpu_affinity/run_test.sh`
- Modify: `test/initramfs/src/regression/process/run_test.sh`

- [x] Add a C test that saves the original mask, skips with exit 77 when fewer than two CPUs are available, pins the caller to the first CPU, compares the complete child mask after `fork`, compares the complete new-thread mask after default `pthread_create`, joins/reaps, and restores the original mask.
- [x] Link the directory with `-static -lpthread` and invoke the new binary from both regression entry points.
- [x] Compile the regression for native Linux and RISC-V with the cached toolchains.
- [x] Run it on native Linux and require `parent`, `fork`, and `pthread` exact-mask passes.
- [x] Pack the RISC-V binary into the existing bounded diagnostic initramfs and boot the unchanged Asterinas SMP=4 kernel. Require a semantic failure where the parent has one CPU and each child has four; build or launch errors do not count as RED.

### Task 2: Pass the creator's mask through clone

**Files:**
- Modify: `kernel/src/process/posix_thread/builder.rs`
- Modify: `kernel/src/process/clone.rs`

- [x] Add `cpu_affinity: CpuSet` to `PosixThreadBuilder`, initialize it with `CpuSet::new_full()`, and add:

```rust
pub fn cpu_affinity(mut self, cpu_affinity: CpuSet) -> Self {
    self.cpu_affinity = cpu_affinity;
    self
}
```

- [x] Destructure the field in `build` and pass it unchanged to `Thread::new` instead of constructing a new full set there.
- [x] In each of `clone_child_task` and `clone_child_process`, load one owned snapshot with `ctx.thread.atomic_cpu_affinity().load(Ordering::Relaxed)` and pass it via `.cpu_affinity(...)`.
- [x] Format Rust and C sources and run `git diff --check`.
- [x] Rebuild the RISC-V kernel using the persistent offline development container; do not recreate the container or download dependencies.
- [x] Re-run the exact SMP=4 diagnostic and require all three complete-mask checks and the program exit status to pass.

### Task 3: Model the inheritance invariant

**Files:**
- Create: `tools/verification/cpu_affinity_inheritance/CpuAffinityInheritance.tla`
- Create: `tools/verification/cpu_affinity_inheritance/ResetToAll.cfg`
- Create: `tools/verification/cpu_affinity_inheritance/InheritSnapshot.cfg`
- Create: `tools/verification/cpu_affinity_inheritance/run.sh`
- Create: `tools/verification/cpu_affinity_inheritance/README.md`

- [x] Model two CPUs, every nonempty parent mask, a clone snapshot, independent post-clone parent/child updates, and the invariant that the initial child mask equals the clone snapshot.
- [x] Make `ResetToAll` reproduce the former builder behavior and require TLC exit 12 with `Invariant ChildInheritedSnapshot is violated.`
- [x] Make `InheritSnapshot` copy the snapshot and require TLC exit 0 with `Model checking completed. No error has been found.`
- [x] Reuse the pinned cached TLC JAR and its SHA-256; the runner must fail without it and must never download.
- [x] Document code correspondence and the explicit limit that multiword `AtomicCpuSet` races above 64 CPUs are not proved by this model.

### Task 4: Final verification and commit

**Files:**
- Verify all files above.

- [x] Run the model runner and inspect both the negative counterexample and corrected exhaustive result.
- [x] Run the focused native and RISC-V SMP=4 affinity tests from cleanly rebuilt inputs.
- [x] Run the affected initramfs build/check plus the relevant Rust compile or broader regression gate permitted by cached time/resources.
- [x] Confirm `git diff --check`, inspect the final diff, and ensure no unrelated files changed.
- [x] Commit the regression, kernel fix, model, and evidence with a focused message. Do not push without a separate user request.
