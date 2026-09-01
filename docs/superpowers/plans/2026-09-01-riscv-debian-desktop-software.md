# RISC-V Debian Desktop Software Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (recommended) or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and validate a Debian Trixie RISC-V desktop rootfs with the official NetSurf package plus vim and ffmpeg, first in QEMU and then on Megrez.

**Architecture:** Keep `desktop-m5-network` immutable and add a distinct software profile with a deterministic evidence service. Reuse the existing QEMU desktop/browser gates and the existing Megrez GMAC/session tooling; add only the package and software-specific checks needed to make failures attributable.

**Tech Stack:** Bash rootfs builder, Python profile/evidence gates, Debian apt/debootstrap, QEMU, Asterinas RISC-V, ext2 manifests.

---

### Task 1: Add the software rootfs profile

**Files:**
- Modify: `tools/riscv/debian/rootfs/profiles.py`
- Modify: `tools/riscv/debian/rootfs/build_rootfs.sh`
- Test: `tools/riscv/tests/test_debian_rootfs.py`

- [x] Add profile `desktop-m9-software` with a new label/UUID and requested packages `vim` and `ffmpeg`; Debian's `ffmpeg` package supplies both `/usr/bin/ffmpeg` and `/usr/bin/ffprobe`. Inherit the complete `desktop-m5-network` package set without mutating that profile.
- [x] Permit the profile in `build_rootfs.sh`, select a dedicated output directory, and install the package closure through the existing signed-source/audit path.
- [x] Add tests for profile identity, package inclusion, output isolation, and rejection of accidental aliasing with M5.
- [x] Run the focused rootfs profile tests.

### Task 2: Add software smoke evidence

**Files:**
- Create: `tools/riscv/debian/rootfs/desktop_m9_software_evidence.sh`
- Create: `tools/riscv/debian/rootfs/desktop_m9_software_gate.py`
- Modify: `tools/riscv/debian/rootfs/build_rootfs.sh`
- Test: `tools/riscv/tests/test_debian_m9_software.py`

- [x] Make the guest evidence script verify exact executable presence, edit/save a temporary file through a non-interactive Vim command, and run `ffmpeg` plus `ffprobe` against a deterministic 16x16 fixture generated in the guest.
- [x] Add finite command timeouts, bounded output, and a single canonical `DEBIAN_DESKTOP_M9_SOFTWARE` marker; failures are explicit and fail closed.
- [x] Install and enable the service only for `desktop-m9-software`.
- [x] Add parser/classifier unit tests for pass, missing binary, timeout, and malformed-marker cases.

### Task 3: Wire QEMU and documentation

**Files:**
- Modify: `Makefile`
- Modify: `tools/riscv/debian/rootfs/README.md`
- Test: `tools/riscv/tests/test_debian_m9_software.py`

- [x] Add a `test_riscv_debian_desktop_m9_software_gate` target using the existing SMP=4 desktop boot contract and the new profile paths.
- [x] Document TUNA/USTC/official mirror fallback, explicit proxy checks, offline package staging, and the exact QEMU acceptance sequence.
- [x] Add the profile and gate to the local unit-test aggregate.
- [x] Run all Debian rootfs unit tests and the new focused gate parser tests.

### Task 4: Build and run QEMU

**Files:**
- Generated only: `target/debian-riscv/desktop-m9-software/`

- [x] Run the image preflight before rootfs construction.
- [x] Build the new profile once, verify its manifest and package lock, and preserve the artifact for all subsequent runs.
- [x] Run the QEMU M5/M6/M7 browser gates plus the M9 software gate with SMP=4 and bounded timeouts. M8 remains a separate optional quality gate because its title check is flaky and is not an application prerequisite.
- [x] Publish serial, screenshot, package-version, and software evidence; classify failures before changing code.

### Task 5: Run the bounded Megrez gate

**Files:**
- Reuse: `tools/riscv/megrez_gmac_gate.py`, `tools/riscv/megrez_board_session.py`, existing Megrez evidence tools
- Generated only: `target/megrez-browser-network/` and software evidence output

- [x] Preflight serial ownership and host interface without resetting the board; the FTDI is free and the suspected board address is live.
- [ ] Read MMC partition and Image/DTB/initramfs hashes through an authenticated board session.
- [ ] Stage the already verified rootfs and run GMAC link/DNS/TLS/PNG checks before desktop checks.
- [ ] Start Xorg/Openbox/PCManFM/xterm/NetSurf, capture a screenshot, and use keyboard-safe commands to smoke-test Vim and ffmpeg. Mouse checks are additive when a device is present.
- [ ] Drain the complete serial transcript and publish pass/failure evidence with recovery status; never wait indefinitely for remote CI or an unbounded board reboot.

Current physical-run blocker: the board is reachable at the preflight layer
(ping and SSH banner), but no authorized SSH key or interactive serial session
is available in this host context. No reset, flash, rootfs replacement, or
guest command has therefore been attempted.
