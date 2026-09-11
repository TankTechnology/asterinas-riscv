# Megrez Browser Evidence Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:executing-plans` to implement this plan task-by-task.  Execute
> inline in the current task; do not delegate to subagents.

**Goal:** Make the physical Firefox result internally consistent and make the
owned host proxy and serial clock lifecycles explicit, bounded, and fully
tested before any network-stack merge.

**Architecture:** Keep the current guest, MMC, and network paths unchanged.
Harden only the host-side `ProxyBridge` state machine and Firefox orchestration
contracts, then verify them against the already attested physical deployment.

**Tech Stack:** Python 3 standard library, `unittest`, `socat`, existing serial
boot controller, persistent Asterinas Docker development container.

---

### Task 1: Make the proxy lifecycle one-shot and auditable

**Files:**

- Modify: `tools/riscv/tests/test_megrez_proxy_bridge.py`
- Modify: `tools/riscv/megrez_proxy_bridge.py`

- [ ] Add a failing test that starts and closes a bridge, then requires
  `running` to be false while `summary()["ready"]` remains true and captured
  stderr remains present.
- [ ] Run that test in the persistent container and verify it fails because
  `close()` currently clears readiness.
- [ ] Add failing tests that reject restart after close, tolerate
  `ProcessLookupError` between `poll` and TERM while still calling `wait`, close
  an internally-owned stderr spool, and leave caller-owned stderr open.
- [ ] Run the new tests and verify each failure is caused by the missing
  lifecycle behavior.
- [ ] Add explicit closed and ever-ready state.  Capture stderr without
  overwriting stored bytes after the spool closes, close only owned stderr,
  reject post-close starts, and treat only `ProcessLookupError` as an already
  vanished process group.
- [ ] Run `tools.riscv.tests.test_megrez_proxy_bridge` and keep all existing
  startup, escalation, configuration, and idempotent-close tests green.
- [ ] Commit the proxy-only change.

### Task 2: Align the Firefox clock and result interfaces with reality

**Files:**

- Modify: `tools/riscv/tests/test_megrez_firefox_browse.py`
- Modify: `tools/riscv/megrez_firefox_browse.py`

- [ ] Change the fake operations test interface to
  `synchronize_clock(timeout)` and run it to verify the production caller still
  passes a browser PID.
- [ ] Add failing tests that reject host epochs before 2024-01-01 and after
  2100-12-31 without sending a serial command.
- [ ] Add failing result-invariant tests showing that `passed=true` rejects a
  missing or false proxy `ready` field while `passed=false` accepts it.
- [ ] Remove the browser PID from the protocol, caller, and real operation;
  validate the host epoch before forming the bounded shell command.
- [ ] Require latched proxy readiness in successful `FirefoxBrowseResult`
  construction and update test proxy summaries to describe successful startup.
- [ ] Run the Firefox browse tests and then the proxy, desktop, and GMAC tests
  to catch shared-summary regressions.
- [ ] Commit the Firefox contract change.

### Task 3: Verify and publish the hardened baseline

**Files:**

- Modify: `docs/superpowers/plans/2026-09-11-megrez-browser-evidence-hardening.md`
- Generated under ignored `target/megrez-desktop/`: one new physical evidence
  bundle using the existing plan and MMC artifacts.

- [ ] Run the browser guest-contract, proxy, Firefox browse, desktop, GMAC,
  boot-stability, physical-graphics, and Debian-rootfs unit modules in the
  persistent Docker container with `ResourceWarning` promoted to an error.
- [ ] Run Python bytecode compilation, `bash -n` for touched workflow scripts,
  and `git diff --check`.  Do not install missing formatters or dependencies.
- [ ] Review the complete diff normally against the maintainability,
  development, security, and hardware indexes.  Do not invoke the retired
  `aster-code-review` skill.
- [ ] Run one unattended `browse-firefox` transaction with the existing
  attested MMC deployment.  Require strict URL/TLS/DOM/framebuffer evidence,
  `proxy_bridge.ready=true`, one physical boot, zero Firefox restarts, checksum
  success, and recovery to U-Boot.
- [ ] Record exact tests, evidence path, hashes, timings, and review result in
  this plan and commit the notes.
- [ ] Fetch `asterinas-riscv/main`, prove it is an ancestor, push `HEAD:main`
  without force, and verify the fetched remote commit equals local `HEAD`.
