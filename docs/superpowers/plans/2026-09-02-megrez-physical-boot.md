# Megrez Physical Boot Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints.

**Goal:** Complete a protected Megrez Asterinas desktop/network boot by making the Debian M5 link gate bounded and diagnosable, then validating it in QEMU and on the board.

**Architecture:** Keep the proven kernel/GMAC and Desktop M4 path unchanged. Harden only the userspace M5 gate: bound each `ip` probe, emit raw link/address diagnostics on timeout, and preserve the existing ordered acceptance markers. Run the pinned QEMU gate first, then one recovery-armed physical transaction.

**Tech Stack:** Bash userspace gate, Python `unittest`, pinned Asterinas Docker image, Megrez serial/U-Boot runner, deterministic host fixture and proxy.

---

### Task 1: Bound and diagnose physical link probing

**Files:**
- Modify: `tools/riscv/debian/rootfs/desktop_m5_network_evidence.sh`
- Test: `tools/riscv/tests/test_debian_m5_network.py`

- [ ] Add a test with a hanging fake `ip` and assert the gate exits within the configured probe/deadline bounds while emitting `DEBIAN_NETWORK_M5_DIAGNOSTIC` records.
- [ ] Add a two-second default probe timeout, wrap both link and address calls with `timeout`, and emit status plus hex-encoded output for link and address on failure.
- [ ] Run the focused M5 tests and update only assertions that intentionally include the new diagnostic records.
- [ ] Commit the bounded diagnostic change.

### Task 2: Re-run the pinned QEMU acceptance

**Files:**
- Use: `target/megrez-debug/plan-current-desktop-browser-20260902-real-now.json`
- Produce: `target/qemu-uboot/desktop-browser-real-now-20260902-d/`

- [ ] Run the full Desktop M5 QEMU gate in `asterinas-env:uboot-sim` with the host fixture.
- [ ] Require the existing Debian, desktop, network, browser, and recovery evidence to pass.

### Task 3: Execute one protected physical boot

**Files:**
- Use: `tools/riscv/megrez_debug.py` and the frozen plan.
- Produce: `target/megrez-debug/board-current-desktop-browser-20260902-diagnostic/`

- [ ] Start only the bounded host proxy/fixture and the serial runner.
- [ ] Issue one `booti`; classify success, bounded guest failure, or recovery without persistent U-Boot writes.
- [ ] If M5 still fails, use its diagnostic records to select the next code change instead of repeating the same boot.

