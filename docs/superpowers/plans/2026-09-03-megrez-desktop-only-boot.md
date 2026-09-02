# Megrez Desktop-Only Boot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Boot a browser-free Debian desktop through Asterinas on Megrez, with Openbox, PCManFM, LXPanel, xterm, keyboard, and pointer evidence.

**Architecture:** Add a runtime desktop-only switch to the existing M4 session/evidence scripts and expose it through a dedicated physical-gate target. Reuse the installed M5 root filesystem and update only the two scripts through RockOS, then run the existing bounded board lifecycle without GMAC boot configuration.

**Tech Stack:** Bash/systemd, Python `unittest`, Asterinas RISC-V QEMU, U-Boot, RockOS/ext2 maintenance path.

---

### Task 1: Define the browser-free desktop contract

**Files:**
- Modify: `tools/riscv/debian/rootfs/desktop_m4_gate.py`
- Test: `tools/riscv/tests/test_debian_m5_network.py`

- [ ] Add a failing test requiring an ordered `DESKTOP_M4_CORE_MILESTONES` tuple whose clients marker contains Openbox, PCManFM, LXPanel, and xterm but not a browser, and whose final marker is `DEBIAN_DESKTOP_M4_CORE_READY user=asterinas display=:0`.
- [ ] Run the focused unittest and confirm it fails because the tuple is absent.
- [ ] Add the immutable core tuple beside the legacy M4 tuple.
- [ ] Re-run the focused unittest and confirm it passes.

### Task 2: Make the M4 session and evidence browser-optional

**Files:**
- Modify: `tools/riscv/debian/rootfs/desktop_m4_session.sh`
- Modify: `tools/riscv/debian/rootfs/desktop_m4_evidence.sh`
- Test: `tools/riscv/tests/test_debian_m5_network.py`

- [ ] Add failing static and sandboxed-script tests proving `ASTERINAS_DESKTOP_BROWSER_ENABLED=0` starts/probes the desktop shell and terminal without executing or inspecting NetSurf, while the unset/default mode remains unchanged.
- [ ] Run the focused tests and confirm the expected failure.
- [ ] Parse only `0` or `1`; skip URL/proxy/NetSurf logic and browser diagnostics in mode `0`; emit the distinct core clients/ready markers after xterm readiness.
- [ ] Run `bash -n` for both scripts and the focused tests; confirm success.

### Task 3: Add the physical desktop target

**Files:**
- Modify: `tools/riscv/megrez_gmac_gate.py`
- Test: `tools/riscv/tests/test_megrez_gmac_gate.py`

- [ ] Add failing tests for `GateTarget.DESKTOP`, desktop marker selection, browser-disable environment, and bootargs that omit `asterinas.net`, `asterinas.neighbor`, proxy, and fixture settings.
- [ ] Run the focused gate tests and confirm the expected failure.
- [ ] Implement the desktop target, select its classifier/ready marker, avoid starting the fixture for this target, and preserve bounded reboot/root-write arguments.
- [ ] Run the focused gate tests and confirm success.

### Task 4: Run local regression checks

**Files:**
- Test only

- [ ] Run the desktop/rootfs and Megrez gate unittest modules.
- [ ] Run `git diff --check`, Python compile checks, and shell syntax checks.
- [ ] Commit only the desktop-only implementation and tests, preserving unrelated dirty changes.

### Task 5: Update and boot the physical board

**Files:**
- Runtime artifacts under `target/megrez-debug/`

- [ ] From the current U-Boot prompt, boot RockOS and connect over the persistent SSH address.
- [ ] Mount `/dev/mmcblk1p2`, back up the installed M4 session/evidence scripts, install the verified replacements, synchronize, and unmount.
- [ ] Return to U-Boot and launch Asterinas with the desktop target, SMP=4, no network bootargs, and a bounded software reboot.
- [ ] Capture the serial transcript through the core ready marker and retain a framebuffer/HDMI observation if available.
- [ ] Report PASS only if the ordered serial contract proves Xorg, evdev input, Openbox, PCManFM, LXPanel, and xterm without NetSurf.

