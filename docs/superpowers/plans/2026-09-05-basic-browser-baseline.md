# Basic Firefox Browser Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints.

**Goal:** Add a controlled fixture-only Firefox acceptance path that can be run in QEMU and once on Megrez without depending on Baidu or Bilibili.

**Architecture:** Keep the existing public-site browser gate intact for compatibility, and add a basic-only mode selected through an explicit environment/kernel argument.  The mode validates the repository fixture, emits a shorter exact timeline/evidence contract, and reuses the existing QEMU and physical-run safety checks.

**Tech Stack:** Python 3 gate code, Bash systemd evidence service, pytest, QEMU RISC-V SMP=4, existing Debian browser-web rootfs.

---

### Task 1: Define the fixture-only contract

**Files:**
- Modify: `tools/riscv/debian/rootfs/browser_web_marionette_gate.py`
- Modify: `tools/riscv/debian/rootfs/browser_web_evidence.sh`
- Test: `tools/riscv/tests/test_debian_browser_web.py`

- [ ] Add an explicit `--basic-only`/`ASTERINAS_BROWSER_WEB_BASIC_ONLY` contract and reject malformed values.
- [ ] Keep fixture home/search/download phases and return a deterministic `DEBIAN_BROWSER_WEB_CONTENT_BASIC fixture_search=pass download=pass` result.
- [ ] Ensure the basic path closes the Marionette session and does not navigate to public sites.
- [ ] Add unit tests for argument validation, output shape, and the no-public-site guarantee.
- [ ] Run the focused browser tests and commit the contract change.

### Task 2: Propagate the mode through QEMU

**Files:**
- Modify: `tools/riscv/debian/rootfs/desktop_m5_qemu_gate.py`
- Modify: `tools/riscv/debian/rootfs/browser_web_qemu_gate.py`
- Test: `tools/riscv/tests/test_debian_browser_web.py`

- [ ] Add a `--basic-only` QEMU option that appends the validated kernel environment argument.
- [ ] Validate the shorter basic timeline/evidence set while preserving strict boot, network, security, and screenshot checks.
- [x] Run QEMU SMP=4 in proxy mode and direct mode, preserving output manifests.
- [x] Proxy mode produced a passing result JSON. Direct mode was bounded at
  420 seconds and classified as a protocol timeout after fixture transfer;
  it is not treated as a pass.

### Task 3: Controlled Megrez run

**Files:**
- Modify: `docs/reviews/2026-09-05-megrez-board-preparation.md`
- Test artifacts: `target/megrez-basic-browser-*/`

- [x] Verify the current serial device is present and unowned, and re-check the
  current artifact inventory before touching the board.
- [ ] Run one bounded basic-only Firefox/Xorg session. The physical permit is
  still locked because the browser profile lacks matching desktop/recovery
  evidence for the current Sv48 bundle; no reset, transfer, or rootfs write was
  attempted.
- [x] Record the direct-path timeout and physical lock in the board-preparation
  review instead of retrying blindly.

### Task 4: Final verification and handoff

- [ ] Run all affected host tests and shell syntax checks.
- [ ] Check `git diff --check` and review generated evidence paths.
- [ ] Push the commits to `asterinas-riscv` branch `codex/firefox-startup-compat`.
- [ ] Report QEMU proxy/direct and Megrez results separately; do not claim third-party-site compatibility.
