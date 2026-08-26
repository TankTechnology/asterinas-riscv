# Debian Desktop M4 Basic Applications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Boot signed Debian Trixie on Asterinas with Matchbox, PCManFM, NetSurf, and xterm in QEMU and on the Milk-V Megrez.

**Architecture:** Add a separate immutable `desktop-m4` profile and reuse the M3 builder, session, evidence, and gate boundaries through small profile-aware extensions. Keep networking, DRM acceleration, and generic USB hotplug out of this slice.

**Tech Stack:** Python 3, Bash, Debian Trixie riscv64 packages, systemd, Xorg fbdev/evdev, Matchbox, PCManFM, NetSurf GTK, QEMU HMP, Asterinas Sv39 SMP=4.

---

### Task 1: Freeze the Desktop M4 package identity

**Files:**
- Modify: `tools/riscv/debian/rootfs/profiles.py`
- Modify: `tools/riscv/debian/rootfs/contract.py`
- Modify: `tools/riscv/debian/rootfs/build_rootfs.sh`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`

- [ ] Add a failing profile test requiring schema 4, label
  `ASTER_DEBIANM4`, the fixed M4 UUID, a separate default output, and the
  sorted M3 package set plus `netsurf-gtk` and `pcmanfm`.
- [ ] Run the focused profile/builder tests; require RED on unknown M4.
- [ ] Add the immutable profile and extend the existing schema/profile switch
  from `(2, 3)` to `(2, 3, 4)` without weakening older validation.
- [ ] Run focused tests GREEN and commit `build(riscv): define Debian desktop M4 profile`.

### Task 2: Assemble and prove the application session

**Files:**
- Create: `tools/riscv/debian/rootfs/desktop_m4_session.sh`
- Create: `tools/riscv/debian/rootfs/desktop_m4_evidence.sh`
- Create: `tools/riscv/debian/rootfs/desktop_m4_welcome.html`
- Modify: `tools/riscv/debian/rootfs/build_rootfs.sh`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`

- [ ] Add failing staging tests for the non-root service, local welcome page,
  `pcmanfm --no-desktop /home/asterinas`, NetSurf local URL, xterm, and exact
  executable/file modes.
- [ ] Add state tests in which PCManFM, NetSurf, or its mapped window is absent;
  each must prevent `DEBIAN_DESKTOP_M4_READY` before a bounded timeout.
- [ ] Implement the M4 session and evidence scripts, keeping the M3 files
  unchanged and launching xterm last for deterministic focus.
- [ ] Run focused shell/Python tests GREEN and commit
  `build(riscv): assemble Debian desktop applications`.

### Task 3: Add the M4 cold-boot visual gate

**Files:**
- Create: `tools/riscv/debian/rootfs/desktop_m4_gate.py`
- Modify: `tools/riscv/debian/rootfs/desktop_m3_gate.py`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`

- [ ] Add failing classifier tests for the ordered M4 client/READY markers,
  fatal markers, schema/profile rejection, and screenshot publication.
- [ ] Extract only the reusable M3 graphical operation mechanics needed by
  M4; do not duplicate PTY, HMP, process-group, or pinned-output logic.
- [ ] Require schema 4/profile `desktop-m4`, a non-blank screenshot, complete
  teardown, and atomic result/log/PPM publication.
- [ ] Run the focused gate and full host rootfs suite GREEN; commit
  `test(riscv): gate Debian desktop applications`.

### Task 4: Build once and run one QEMU decision gate

**Files:**
- Create after success: `docs/porting/evidence/2026-08-26-debian-desktop-m4-apps.md`
- Modify after success: `docs/porting/README.md`

- [ ] Reuse the pinned cached container, local Clash proxy at
  `127.0.0.1:17892`, and HTTPS TUNA mirror; build M4 once and preserve signed
  InRelease, package lock, checksums, and artifact hashes.
- [ ] Validate the frozen root with the public contract, then run one Sv39,
  SMP=4, 2 GiB, no-network QEMU graphical gate.
- [ ] Save result JSON, serial log, and screenshot; record exact hashes,
  duration, package versions, and non-claims.

### Task 5: Install and start the persistent Megrez desktop

**Files:**
- Reuse: `tools/riscv/debian/rootfs/megrez_installer.py`
- Reuse: `tools/riscv/megrez_board_session.py`
- Modify after success: `docs/porting/evidence/2026-08-26-debian-desktop-m4-apps.md`

- [ ] Build the restart-safe installer from the verified M4 root and transfer
  it over the same-switch HTTP path; compare SHA-256 on host and board.
- [ ] Run the bounded partition-2 installer gate and verify the final ext2
  label/hash before normal boot.
- [ ] Boot Asterinas with dual DWC3 selectors and a protection timer; require
  M4 READY, both HID registrations, and no panic/xHCI failure marker.
- [ ] After the bounded gate succeeds, reboot through RAM-only U-Boot commands
  without `asterinas.reboot_after` and without `saveenv`.
- [ ] Record the final serial evidence and ask the operator only for the
  inherently visual mouse/window interaction confirmation.
