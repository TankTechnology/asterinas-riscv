# Debian systemd Mount-Monitor M3 Implementation Plan

**Goal:** Remove the proven inotify/libmount incompatibility from Debian M2,
then prove systemd observes runtime mounts in one bounded QEMU Sv39/SMP=4 run.

**Architecture:** Repair the smallest Linux ABI boundary first. Treat QEMU as
the decision point for a separate mountinfo-notification slice; do not build
that machinery speculatively. Preserve all M2 rootfs and two-boot identities.

**Tech Stack:** Safe Rust kernel VFS/syscalls, C initramfs regression tests,
Python systemd gate, Asterinas RISC-V, QEMU, Debian systemd/libmount.

---

### Task 1: Add the `IN_ISDIR` public regression

**Files:**
- Create: `test/initramfs/src/regression/fs/inotify/inotify_isdir.c`
- Modify: `test/initramfs/src/regression/fs/run_test.sh`

- [ ] Add a watch with `IN_CREATE | IN_ISDIR`, create a subdirectory, and
  assert exact watch descriptor, name, and both event bits.
- [ ] Run the focused regression on the current kernel and record `EINVAL` RED.

### Task 2: Repair inotify mask compatibility

**Files:**
- Modify: `kernel/src/fs/vfs/notify/inotify.rs`

- [ ] Add the Linux UAPI `ISDIR` event bit without duplicating VFS event logic.
- [ ] Run the focused regression GREEN.
- [ ] Run the relevant RISC-V kernel compile and static checks once.
- [ ] Commit the test and fix atomically.

### Task 3: Strengthen the M2 gate into M3

**Files:**
- Modify: `tools/riscv/debian/rootfs/systemd_m2_gate.py`
- Modify: `tools/riscv/debian/rootfs/gate_protocol.py`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`

- [ ] Add transcript rejection for libmount setup/drain and mount-unit protocol
  failures.
- [ ] Require guest evidence that `/tmp` and `/run/lock` are mount points.
- [ ] Run focused host tests and static checks.

### Task 4: Run the new QEMU decision gate

- [ ] Rebuild the affected RISC-V kernel/initramfs in the pinned container.
- [ ] Run exactly one generic-Sv39, SMP=4, 2 GiB, no-network/no-display gate.
- [ ] Preserve the complete transcript and result evidence.
- [ ] If the gate is green, skip Task 5. If it is red specifically because
  mountinfo readiness is missing, use that transcript as Task 5 RED.

### Task 5: Conditionally implement mountinfo readiness

**Files (only if Task 4 proves necessary):**
- Modify: `kernel/src/fs/vfs/path/mount_namespace.rs`
- Modify: `kernel/src/fs/vfs/path/mount.rs`
- Modify: `kernel/src/fs/fs_impls/procfs/pid/task/mountinfo.rs`
- Add or modify the narrowest public regression test.

- [ ] Add per-namespace topology notification after committed mount changes.
- [ ] Add a per-open mountinfo handle with edge/drain semantics.
- [ ] Run the focused public regression and one fresh QEMU gate.

### Task 6: Record M3 and define the desktop-base successor

**Files:**
- Create: `docs/porting/evidence/2026-08-25-debian-systemd-mount-monitor-m3.md`
- Modify: `docs/porting/README.md`

- [ ] Record exact commits, artifact hashes, commands, durations, transcript
  findings, and non-claims.
- [ ] Define the next signed desktop-base milestone around login/session/runtime
  correctness; do not claim Xorg or browser readiness in M3.
- [ ] Commit evidence after all claimed checks have completed.
