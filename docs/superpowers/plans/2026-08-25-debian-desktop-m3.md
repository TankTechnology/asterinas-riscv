# Debian RISC-V Desktop M3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Boot a signed Debian Trixie riscv64 root on Asterinas into a non-root Xorg/matchbox/xterm session and save transcript, result, and framebuffer evidence.

**Architecture:** Add a separate immutable `desktop-m3` rootfs profile and a small guest session/evidence payload. Reuse the existing descriptor-pinned Debian gate lifecycle, extending only the QEMU graphics/input and screenshot protocol needed for a bounded cold-boot desktop gate.

**Tech Stack:** Python 3, Bash, Debian Trixie riscv64 packages, systemd 257, PAM/logind, Xorg fbdev/evdev, QEMU HMP, Asterinas RISC-V generic-Sv39 SMP=4.

---

### Task 1: Freeze the desktop profile

**Files:**
- Modify: `tools/riscv/debian/rootfs/profiles.py`
- Modify: `tools/riscv/debian/rootfs/contract.py`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`

- [ ] Add a failing test requiring `get_profile("desktop-m3")` to return a
  distinct schema/label/UUID and the exact sorted explicit package tuple from
  the design.
- [ ] Run the focused profile test and verify RED is `unknown rootfs profile`.
- [ ] Add the immutable profile and teach the existing manifest contract to
  validate its identity without weakening schema 1/2.
- [ ] Run the profile and contract classes GREEN, then commit as
  `build(riscv): define Debian desktop M3 profile`.

### Task 2: Assemble the Debian desktop session

**Files:**
- Create: `tools/riscv/debian/rootfs/desktop_m3_evidence.sh`
- Create: `tools/riscv/debian/rootfs/desktop_m3_session.sh`
- Modify: `tools/riscv/debian/rootfs/build_rootfs.sh`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`

- [ ] Add RED tests that stage a fake root and require the `asterinas` account,
  PAM-backed tty1 service, xinit session script, Xorg fbdev/evdev config, and
  bounded evidence service.
- [ ] Add RED state tests where each of udev, logind, login session, input
  devices, Xorg log, matchbox, and xterm is independently absent.
- [ ] Implement profile-specific staging. The session service must contain
  `User=asterinas`, `PAMName=login`, `TTYPath=/dev/tty1`, and execute
  `/usr/lib/asterinas/desktop-m3-session`.
- [ ] Implement an evidence script that emits
  `DEBIAN_DESKTOP_M3_READY user=asterinas display=:0` only after every required
  state is observed before one monotonic deadline.
- [ ] Run focused builder/evidence tests, `bash -n`, Python static checks, and
  commit as `build(riscv): assemble Debian desktop M3 root`.

### Task 3: Add the graphical cold-boot gate

**Files:**
- Create: `tools/riscv/debian/rootfs/desktop_m3_gate.py`
- Modify: `tools/riscv/debian/rootfs/gate_protocol.py`
- Modify: `tools/riscv/debian/rootfs/rootfs_gate_backend.py`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`

- [ ] Add RED tests for exact QEMU `bochs-display`, VirtIO keyboard/tablet, no
  network, matching four-hart DTB, simple-framebuffer U-Boot commands, and HMP
  screendump.
- [ ] Add RED classifier tests that require ordered udev/logind/PAM/Xorg/input/
  session markers and reject panic or Xorg fatal output anywhere in the full
  transcript.
- [ ] Add RED PPM tests requiring the expected dimensions and more than one
  meaningful color region under a bounded byte cap.
- [ ] Implement the smallest graphics adapter around the existing rootfs gate;
  do not duplicate PTY, HMP, process-group, or pinned-output code.
- [ ] Run focused gate/runtime tests and commit as
  `test(riscv): automate Debian desktop cold boot`.

### Task 4: Build once and run one real QEMU decision gate

**Files:**
- Create after success: `docs/porting/evidence/2026-08-25-debian-desktop-m3.md`
- Modify after success: `docs/porting/README.md`

- [ ] Build `desktop-m3` once in the pinned container using the local Clash
  proxy and TUNA HTTPS mirror; preserve the signed InRelease and package hashes.
- [ ] Verify the resulting ext2 with the public contract before QEMU.
- [ ] Build the Sv39/SMP=4 kernel only if its current hash does not match the
  last verified clean image.
- [ ] Run one cold-boot QEMU gate with a total deadline and save serial, JSON,
  and PPM evidence. Do not start a second unchanged run.
- [ ] If red, classify the first failing layer and add one focused RED before
  changing code. If green, record exact commands, hashes, durations, marker
  evidence, screenshot path, and non-claims.

### Task 5: First interaction slice

**Files:**
- Modify: `tools/riscv/debian/rootfs/desktop_m3_gate.py`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`
- Modify: `docs/porting/evidence/2026-08-25-debian-desktop-m3.md`

- [ ] Add RED protocol tests for one QEMU monitor keyboard sequence typed into
  xterm and one pointer click routed through the tablet device.
- [ ] Add a guest-visible nonce command and require the xterm-rendered nonce in
  a second screenshot or serial-side terminal capture.
- [ ] Run one interaction gate only after the passive desktop gate is green.
- [ ] Record whether focus, key release, backspace, and pointer routing work;
  leave PCManFM/NetSurf and accelerated DRM to the next milestone.
