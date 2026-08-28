# Megrez Debian Desktop Preboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind the current Debian desktop bytes to QEMU desktop and recovery evidence, issue a fail-closed preboard permit, install the exact root through Asterinas, and complete one automatically recovering Megrez desktop boot.

**Architecture:** Extend the existing `megrez_debug` contract with a backward-compatible schema 2 `debian-browser` profile. Reuse the existing M6 QEMU, software-reboot, rootfs installer, board session, and GMAC gate implementations; add only thin adapters and permit validation.

**Tech Stack:** Python 3 standard library, Bash, existing Asterinas RISC-V QEMU gates, U-Boot HMP/serial helpers, PTY tests, `unittest`, GNU Make.

---

### Task 1: Freeze the schema 2 Debian browser plan

**Files:**
- Modify: `tools/riscv/megrez_debug_contract.py`
- Modify: `tools/riscv/megrez_debug.py`
- Modify: `tools/riscv/tests/test_megrez_debug.py`

- [ ] Add RED tests proving schema 1 canonical bytes remain unchanged and a
  schema 2 plan requires the exact ten-artifact order, `debian-browser`,
  SMP=4, Sv39, bounded reboot, and ordered M5/M4/M6 markers. Reject a root
  image that is not exactly 1 GiB, oversized metadata, aliases, symlinks,
  unsafe bootargs, and incomplete rootfs provenance.
- [ ] Run the focused contract/CLI tests and confirm failures arise from the
  absent schema 2 contract rather than fixture errors.
- [ ] Add per-artifact size policies and schema-specific validation. Extend
  `plan` CLI arguments with U-Boot/rootfs provenance paths and validate them
  through `load_manifest` plus `validate_frozen_root` before publication.
- [ ] Run focused and full Megrez debug/rootfs unit tests, static checks, and
  commit `feat(riscv): freeze Debian browser board plan`.

### Task 2: Bind the current M6 QEMU gate to the plan

**Files:**
- Create: `tools/riscv/megrez_debug_desktop.py`
- Modify: `tools/riscv/megrez_debug.py`
- Modify: `tools/riscv/tests/test_megrez_debug.py`
- Modify: `Makefile`

- [ ] Add RED adapter tests using an injected process runner. Require stale
  result invalidation, exact plan paths, bounded timeout, exact M6 hash map,
  Sv39/SMP4/2GiB argv, required devices, screenshots, marker evidence, and
  atomic `StageResult(stage="desktop")` publication. Cover every mismatch and
  signal path.
- [ ] Implement `simulate --tier desktop` as a thin invocation of
  `tools.riscv.debian.rootfs.desktop_m6_browser_gate`; do not reconstruct its
  QEMU argv or classifier.
- [ ] Add `test_riscv_megrez_debug_desktop` Make alias, run focused/full tests
  and static checks, then commit `feat(riscv): bind Megrez desktop simulation`.

### Task 3: Require fresh automatic-recovery evidence and issue a permit

**Files:**
- Create: `tools/riscv/megrez_preboard.py`
- Modify: `tools/riscv/megrez_debug_contract.py`
- Modify: `tools/riscv/megrez_debug.py`
- Modify: `tools/riscv/tests/test_megrez_debug.py`
- Modify: `Makefile`

- [ ] Add RED tests for exact passed `fast`, `desktop`, and `recovery` results,
  result-file one-open hashing, plan/commit/artifact drift, four-hart Sv39 DTB
  checks, recovery-kernel mismatch, stale permit invalidation, output swaps,
  and first/second signal behavior.
- [ ] Implement a canonical `PreboardPermit` and `preboard` command. Reuse the
  existing rootfs and DTB validators and accept recovery evidence only when it
  names the current kernel hash and a fresh U-Boot prompt after a second
  firmware epoch.
- [ ] Run host tests/static checks and commit
  `feat(riscv): gate Megrez desktop board access`.

### Task 4: Prove current artifacts in QEMU once

**Files:**
- Modify only when real evidence exposes a reproducible defect
- Generate under: `target/megrez-debian-preboard/`

- [ ] Verify the current signed rootfs contract and exact artifact hashes.
- [ ] Run the full host gate once.
- [ ] Run one current-artifact M6 QEMU gate with timeout 420 seconds and require
  DNS, HTTPS, M5, desktop shell, Openbox, PCManFM, LXPanel, visible NetSurf,
  limited JavaScript, keyboard/tablet, and two screenshots.
- [ ] Run one current-kernel timer reboot QEMU gate and require a second
  firmware epoch plus fresh U-Boot prompt.
- [ ] Create schema 2 plan, translate both QEMU results, and publish a passing
  preboard permit. If a run fails, first reproduce it in a focused host test;
  do not proceed to the board.

### Task 5: Install the exact root through Asterinas

**Files:**
- Create: `tools/riscv/megrez_debian_install.py`
- Modify: `tools/riscv/megrez_debug.py`
- Modify: `tools/riscv/tests/test_megrez_debug.py`
- Modify: `Makefile`

- [ ] Add RED PTY tests for permit/result validation, literal-LAN HTTP URL,
  installer archive identity, volatile write arming, exact target partition,
  one `booti`, ordered exact-hash install markers, automatic recovery, one
  deadline, signals, and no `saveenv`/reset/Linux.
- [ ] Reuse `megrez_installer.build_network_archive` and the existing board
  session/TFTP path. Publish `StageResult(stage="install")` only after exact
  readback and a fresh U-Boot prompt.
- [ ] Run focused/full host tests and commit
  `feat(riscv): install Debian root over Megrez LAN`.
- [ ] With the preboard permit passing and `/dev/ttyUSB0` exclusively
  available, perform one installation attempt. On recovery failure, release
  serial and return to QEMU work without requesting a manual reset.

### Task 6: Complete one bounded physical desktop gate

**Files:**
- Modify: `tools/riscv/megrez_gmac_gate.py` only if a shared permit hook is
  required
- Modify: `tools/riscv/megrez_debug.py`
- Modify: `tools/riscv/tests/test_megrez_gmac_gate.py`
- Modify: `tools/riscv/tests/test_megrez_debug.py`
- Modify: `tools/riscv/README.md`

- [ ] Add RED tests requiring matching preboard/install results before serial
  open and rejecting all stale/mismatched evidence. Preserve the existing
  TFTP, physical marker, drain, signal, and output contracts.
- [ ] Wire a `board --profile debian-browser` path to the existing physical
  gate with one `booti`, bounded reboot, and no persistent U-Boot writes.
- [ ] Run host/PTY tests and static checks, then commit
  `feat(riscv): boot verified Debian desktop on Megrez`.
- [ ] Execute one bounded physical desktop boot. Require GMAC link/DNS/HTTPS,
  M5 READY, M4 shell/client/READY, M6 remote/JavaScript/READY, real USB input,
  HDMI framebuffer, and automatic return to U-Boot.
- [ ] Record hashes, result JSON, serial log, and screenshots; push the clean
  commits to `TankTechnology/asterinas-riscv` `main`.

### Task 7: Continue non-board foundation work when physical recovery is absent

**Files:**
- Modify only evidence-backed Asterinas or Debian compatibility paths

- [ ] If the board cannot recover automatically, do not ask for repeated
  manual resets. Capture the result, release serial, and continue with the
  exact `systemd-sysusers` exit/errno and `/proc/sys/fs/nr_open` regression in
  QEMU.
- [ ] Keep NetSurf claims bounded to basic browsing/images/forms and explicit
  limited JavaScript. Do not claim full modern Baidu or Firefox support.
