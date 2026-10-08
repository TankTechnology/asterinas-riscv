# Firefox Loopback Root-Cause and Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Execute inline in the current isolated worktree, preserve unrelated dirty changes, and do not update remote PRs.

**Goal:** Identify the first broken state transition in the Firefox Marionette loopback path, add a failing regression that proves the defect, implement the smallest causal fix, and demonstrate that the Firefox graphical interaction gate passes on the repaired RISC-V kernel.

**Architecture:** Use a four-level experiment ladder. Pure kernel ktests first separate `Pollee`/`Waiter` behavior from TCP; a Linux-referenced C guest test then reproduces Firefox's persistent loopback and three-fd `ppoll` shape; port-filtered bounded tracing identifies TCP queue, dispatch, notification, and Gecko callback boundaries only if the short tests do not select the cause. A semantic fix is permitted only after a failing test or a complete boundary trace proves the responsible transition.

**Tech Stack:** Safe Rust, Asterinas ktest/OSDK, `aster-bigtcp`, smoltcp loopback, C11/pthreads/`ppoll`, Python 3 diagnostics, Firefox ESR 140 overlay tooling, persistent offline Docker container, RISC-V QEMU SMP4.

---

## File map

- Modify `kernel/src/process/signal/poll.rs`: add a focused three-Pollee/one-Poller ktest only.
- Create `test/initramfs/src/regression/network/tcp_ppoll_wakeup.c`: Linux/Asterinas persistent-loopback wakeup reproducer.
- Modify `test/initramfs/src/regression/network/run_test.sh`: retain the new test in the normal regression suite.
- Create `test/initramfs/src/regression/scripts/run_tcp_ppoll_wakeup_test.sh`: isolated guest entry point.
- Modify `Makefile`: add `AUTO_TEST=tcp_ppoll_wakeup` without changing existing targets.
- Modify `kernel/libs/aster-bigtcp/src/iface/tcp_diagnostics.rs`: replace uncorrelated global one-shot evidence with opt-in, port-filtered, bounded scalar events if short tests do not reproduce.
- Modify the narrow call sites in `kernel/libs/aster-bigtcp/src/socket/bound/tcp_conn.rs`, `kernel/libs/aster-bigtcp/src/iface/poll_iface.rs`, and `kernel/libs/aster-bigtcp/src/iface/poll.rs`: emit queue/dispatch/peer-process stages for the selected port.
- Modify `kernel/src/net/socket/ip/stream/observer.rs` and its constructors only if the trace requires a kernel readiness-notify boundary.
- Modify `tools/riscv/diagnostics/firefox_actor_overlay.py` or create one adjacent focused overlay helper: add pre-dispatch Gecko transport markers if kernel boundaries all complete.
- Modify `tools/riscv/diagnostics/firefox_dmesg_experiment.py` and its tests: enable and validate the selected trace without changing the page or command.
- Create `docs/porting/evidence/2026-09-08-firefox-loopback-root-cause.md`: immutable commands, hashes, boundary result, failing regression, fix, and final acceptance.

## Task 1: Baseline and exact experiment contract

- [ ] **Step 1: Freeze the observed facts**

Record in the evidence note that request 4 wrote 1176 bytes completely, Firefox's Socket Thread entered a three-fd indefinite `ppoll` before that send and remained there, no `server.command` marker appeared, and the existing one-shot TCP marker cannot be attributed to request 4.

- [ ] **Step 2: Define the decision table**

Use these exclusive outcomes:

```text
Pollee ktest fails
  -> investigate Subject/Waiter registration and wake ordering.
C ppoll control pipe wakes but TCP does not
  -> investigate TCP poll_at/pending/dispatch/readiness propagation.
C ppoll TCP path passes with TCP_NODELAY but fails with default Nagle
  -> investigate smoltcp Nagle/ACK/poll scheduling.
Both short tests pass
  -> run one correlated kernel+Gecko boundary experiment.
Kernel send/dispatch/peer-notify completes but Gecko callback is absent
  -> investigate NSPR/poll fd mapping.
Gecko callback receives bytes but no complete packet is emitted
  -> investigate Marionette framing/async transport.
```

- [ ] **Step 3: Check cached prerequisites**

Run the persistent launcher with `--offline` and verify the repository-local OSDK binary, RISC-V compiler, initramfs inputs, kernel image inputs, and Firefox root image are already present. Do not install packages, pull images, or recreate the container.

## Task 2: Isolate multi-object poll wakeup

**Files:**
- Modify `kernel/src/process/signal/poll.rs`

- [ ] **Step 1: Add the focused characterization ktest**

Add `one_poller_wakes_from_one_of_three_pollees`. It creates three `Pollee` objects, registers one `Poller` handle with all three for `IoEvents::IN`, spawns a task that yields and notifies only the middle Pollee, waits with a finite one-second timeout, and asserts that only the middle readiness closure returns `IN` after wakeup.

- [ ] **Step 2: Run only the new ktest**

Run:

```sh
tools/docker/run_dev_container.sh --workspace "$WORKTREE" --offline -- \
  tools/riscv/kernel_ktest.sh \
  aster_kernel::process::signal::poll::test::one_poller_wakes_from_one_of_three_pollees \
  target/firefox-loopback-root-cause/pollee-ktest
```

Expected: one test passes. A timeout or wrong readiness result selects the poll subsystem and stops TCP experimentation until that failure is understood.

- [ ] **Step 3: Run the existing neighboring tests**

Run the complete `aster_kernel::process::signal::poll::test` filter and require every existing notify-before/middle/after test plus the new test to pass on RISC-V SMP4.

## Task 3: Reproduce the persistent loopback three-fd wait

**Files:**
- Create `test/initramfs/src/regression/network/tcp_ppoll_wakeup.c`
- Create `test/initramfs/src/regression/scripts/run_tcp_ppoll_wakeup_test.sh`
- Modify `test/initramfs/src/regression/network/run_test.sh`
- Modify `Makefile`

- [ ] **Step 1: Write the bounded C regression**

The test must use one IPv4 loopback TCP connection for its full lifetime. The server makes the accepted socket nonblocking, drains to `EAGAIN`, and waits in `ppoll` on exactly three descriptors: accepted TCP socket, watchdog control pipe, and an inert pipe. The client sends four length-prefixed messages and reads a response after the first three; the fourth payload is exactly 1176 bytes. Response sizes are 739, 51, and 25 bytes to preserve the observed bidirectional shape. A watchdog writes the control pipe after two seconds, converting a lost TCP wake into a finite assertion failure. The test runs once with default Nagle and once with `TCP_NODELAY`; it logs only case, iteration, lengths, return mask, and monotonic duration.

- [ ] **Step 2: Verify the Linux reference**

Compile the same source natively with:

```sh
cc -O2 -Wall -Wextra -Werror -pthread \
  test/initramfs/src/regression/network/tcp_ppoll_wakeup.c \
  -o target/firefox-loopback-root-cause/tcp_ppoll_wakeup-linux
target/firefox-loopback-root-cause/tcp_ppoll_wakeup-linux
```

Expected: both default-Nagle and `TCP_NODELAY` cases pass, no watchdog wake is consumed, and the process exits zero.

- [ ] **Step 3: Add a dedicated Asterinas auto-test entry point**

Add `AUTO_TEST=tcp_ppoll_wakeup` following the existing `tcp_user_buffer_prefault` pattern. Its init script runs only `/test/network/tcp_ppoll_wakeup` and prints exactly `TCP ppoll wakeup regression passed.` after success.

- [ ] **Step 4: Run the current kernel before any semantic fix**

Build and run through the persistent container, offline, with `TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode AUTO_TEST=tcp_ppoll_wakeup`. Preserve the complete QEMU log under `target/firefox-loopback-root-cause/current-kernel/`.

Expected diagnostic outcomes: a TCP-watchdog failure is a valid RED reproduction; a full pass rejects the simple syscall/TCP sequence and advances to Task 4.

## Task 4: Trace the selected loopback connection once

**Files:**
- Modify `kernel/libs/aster-bigtcp/src/iface/tcp_diagnostics.rs`
- Modify `kernel/libs/aster-bigtcp/src/socket/bound/tcp_conn.rs`
- Modify `kernel/libs/aster-bigtcp/src/iface/poll_iface.rs`
- Modify `kernel/libs/aster-bigtcp/src/iface/poll.rs`
- Modify `kernel/src/net/socket/ip/stream/observer.rs` only for the final readiness boundary
- Modify `tools/riscv/diagnostics/firefox_dmesg_experiment.py`
- Modify `tools/riscv/tests/test_firefox_dmesg_experiment.py`

- [ ] **Step 1: Write failing pure tests for the trace policy**

Specify an opt-in destination/source-port filter for 2828, a maximum of 128 scalar records, monotonic sequence numbers, and a terminal suppression count. The original 64-record budget was increased after the focused TCP smoke test showed that it only covered one protocol-shaped cycle; 128 remains bounded while retaining the fourth Firefox request boundary. Assert that disabled tracing emits nothing, unrelated ports emit nothing, payload bytes are never formatted, and exhaustion emits only the bounded summary.

- [ ] **Step 2: Verify RED**

Run the trace-policy unit test and observe failure because filtered bounded tracing does not yet exist.

- [ ] **Step 3: Implement the minimal trace stages**

Emit only:

```text
send-buffered(bytes, local_port, remote_port)
poll-at(old, new, need_iface_poll)
pending-pop
segment-generated(payload_len)
peer-process(recv_queue_before, recv_queue_after)
socket-events(bits)
pollee-notify(bits)
```

Use socket/connection sequence identifiers, not pointers or payload contents. Keep the facility at `info`, disabled by default, and activate it only for port 2828 in the diagnostic boot.

- [ ] **Step 4: Verify GREEN and no default output**

Run the trace-policy tests, relevant `aster-bigtcp` ktests, static RISC-V kernel check, and a short boot without the diagnostic parameter. Require zero trace lines in the default boot.

- [ ] **Step 5: Add pre-dispatch Gecko markers**

Instrument the frozen Firefox transport at `onInputStreamReady` entry, after `available()`, after `_processIncoming`, and immediately before `TCPConnection.onPacket`. Records contain request-independent sequence, available byte count, buffered byte count, and packet type only; no command body, URL, script, or response data.

- [ ] **Step 6: Run one bounded Firefox experiment**

Reuse the frozen kernel/rootfs inputs, full 300-second request budget, SMP4, dmesg collector, and page. Rebuild only the deterministic Firefox overlay and kernel. Run exactly once and classify the first missing trace stage.

## Task 5: Prove and repair the selected defect

- [ ] **Step 1: Reduce the selected boundary to a failing regression**

If Task 3 already failed, use that C test. Otherwise add one focused ktest or C variation reproducing the exact missing transition identified by Task 4. Run it on the unmodified semantic code and require the expected assertion failure before changing production behavior.

- [ ] **Step 2: Implement one minimal causal fix**

Change only the state transition that the failing regression proves incorrect. Do not add retries, sleeps, forced unconditional interface polls, Firefox workarounds, or wider logging as the repair.

- [ ] **Step 3: Verify RED to GREEN**

Run the exact failing test first, its neighboring module tests second, and the Linux reference comparison third. Preserve before/after logs and hashes.

- [ ] **Step 4: Remove or retain diagnostics deliberately**

Retain reusable bounded, default-off diagnostics only if they have an explicit test and documented activation. Remove experiment-only Firefox overlays from acceptance inputs; the fix must work without diagnostic timing effects.

## Task 6: Final Firefox and regression verification

- [ ] **Step 1: Run scoped checks**

Run format, clippy/static checks for changed crates, the full focused ktest groups, the isolated `tcp_ppoll_wakeup` guest, and the existing TCP regression set using cached offline inputs.

- [ ] **Step 2: Run an observer-disabled Firefox acceptance**

Run the uninstrumented Firefox physical-graphics QEMU gate for all required interaction cycles. Require `WebDriver:ExecuteScript`, DOM evidence, screenshot evidence, pointer/keyboard interaction evidence, and clean shutdown to pass without the diagnostic overlay.

- [ ] **Step 3: Record claim boundaries**

Write the evidence note with the precise root cause, why it occurred, the failing test, the causal patch, verification commands and hashes. Mark QEMU success separately from physical-board success; do not claim the board until the same gate is executed on hardware.

- [ ] **Step 4: Review the final diff**

Review only task-related paths against Asterinas maintainability, kernel-development, and security guidelines. Confirm no `unsafe` entered `kernel/`, no payload data is logged, and unrelated dirty files were not changed.
