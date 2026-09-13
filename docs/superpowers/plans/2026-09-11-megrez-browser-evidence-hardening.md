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

- [x] Add a failing test that starts and closes a bridge, then requires
  `running` to be false while `summary()["ready"]` remains true and captured
  stderr remains present.
- [x] Run that test in the persistent container and verify it fails because
  `close()` currently clears readiness.
- [x] Add failing tests that reject restart after close, tolerate
  `ProcessLookupError` between `poll` and TERM while still calling `wait`, close
  an internally-owned stderr spool, and leave caller-owned stderr open.
- [x] Run the new tests and verify each failure is caused by the missing
  lifecycle behavior.
- [x] Add explicit closed and ever-ready state.  Capture stderr without
  overwriting stored bytes after the spool closes, close only owned stderr,
  reject post-close starts, and treat only `ProcessLookupError` as an already
  vanished process group.
- [x] Run `tools.riscv.tests.test_megrez_proxy_bridge` and keep all existing
  startup, escalation, configuration, and idempotent-close tests green.
- [x] Commit the proxy-only change (`5eebd1649`).

### Task 2: Align the Firefox clock and result interfaces with reality

**Files:**

- Modify: `tools/riscv/tests/test_megrez_firefox_browse.py`
- Modify: `tools/riscv/megrez_firefox_browse.py`

- [x] Change the fake operations test interface to
  `synchronize_clock(timeout)` and run it to verify the production caller still
  passes a browser PID.
- [x] Add failing tests that reject host epochs before 2024-01-01 and after
  2100-12-31 without sending a serial command.
- [x] Add failing result-invariant tests showing that `passed=true` rejects a
  missing or false proxy `ready` field while `passed=false` accepts it.
- [x] Remove the browser PID from the protocol, caller, and real operation;
  validate the host epoch before forming the bounded shell command.
- [x] Require latched proxy readiness in successful `FirefoxBrowseResult`
  construction and update test proxy summaries to describe successful startup.
- [x] Run the Firefox browse tests and then the proxy, desktop, and GMAC tests
  to catch shared-summary regressions.
- [x] Commit the Firefox contract change (`33357212a`).

### Task 3: Verify and publish the hardened baseline

**Files:**

- Modify: `docs/superpowers/plans/2026-09-11-megrez-browser-evidence-hardening.md`
- Generated under ignored `target/megrez-desktop/`: one new physical evidence
  bundle using the existing plan and MMC artifacts.

- [x] Run the browser guest-contract, proxy, Firefox browse, desktop, GMAC,
  boot-stability, physical-graphics, and Debian-rootfs unit modules in the
  persistent Docker container with `ResourceWarning` promoted to an error.
- [x] Run Python bytecode compilation, `bash -n` for touched workflow scripts,
  and `git diff --check`.  Do not install missing formatters or dependencies.
- [x] Review the complete diff normally against the maintainability,
  development, security, and hardware indexes.  Do not invoke the retired
  `aster-code-review` skill.
- [x] Run one unattended `browse-firefox` transaction with the existing
  attested MMC deployment.  Require strict URL/TLS/DOM/framebuffer evidence,
  `proxy_bridge.ready=true`, one physical boot, zero Firefox restarts, checksum
  success, and recovery to U-Boot.
- [x] Record exact tests, evidence path, hashes, timings, and review result in
  this plan and commit the notes.
- [x] Fetch `asterinas-riscv/main`, prove it is an ancestor, push `HEAD:main`
  without force, and verify the fetched remote commit equals local `HEAD`.

## Physical-validation checkpoint (2026-09-11)

The current code passes 425 selected tests in the persistent development
container with `ResourceWarning` promoted to an error.  Python bytecode
compilation, the stage-1 `bash -n` check, generated clock and browser command
`bash -n` checks, and `git diff --check` also pass.  Normal review against the
maintainability, development, security, and hardware indexes found no remaining
issue in the committed proxy and clock contracts.

The existing MMC kernel, stage 1, root filesystem, and DTB were not rewritten.
Four evidence-rich runs all recovered automatically to U-Boot and all published
`proxy_bridge.ready=true`, proving the corrected latched proxy evidence:

- `browse-52f941d38ab00777` reached Marionette and Baidu but streamed 1,010
  expected connection refusals as roughly 3,030 UART records.  The resulting
  serial backlog hid the guest timeout behind `serial-marker-not-seen`.
- `browse-79d544de08865110` reached complete graphical readiness but an invalid
  clock result took the pre-existing bare `exit 1` path, terminating the debug
  shell and allowing the later abort byte to corrupt the replacement shell.
- `browse-d0a3b15f1dd3e815` proved the revised clock contract exactly
  (`requested=observed=1789094617`) and then rejected the first buffered browser
  command before transmission because its wrapper-expanded size was 865 bytes.
- `browse-9d979cb96e917d4d` proved the corrected 693-byte transmitted command and
  bounded serial output (`A_WEB_CONNECT_RETRIES count=1192`, 65 total `A_WEB`
  lines).  It completed navigation, title, repeated live-DOM probes, a 27,478
  byte snapshot, and `BOOT_DOM_READY` at guest monotonic 797.747 seconds.  The
  650-second DOM budget then left only ten seconds for framebuffer finalization,
  bounded log replay, and the host ACK; the real `status=124` arrived just after
  the host deadline and abort byte.

At this checkpoint, the page gate still lacked a successful post-hardening
physical run and the branch remained unpublished.  This motivated replacing
the shared deadline with three explicit bounded budgets: page/DOM execution,
guest framebuffer and wrapper finalization, and host serial ACK drain.  All
three remain below the kernel-owned 1050-second recovery timer; simply retrying
or unboundedly increasing the page timeout was not acceptable.

### Task 4: Split the physical page deadline into three bounded stages

**Files:**

- Modify: `tools/riscv/tests/test_megrez_firefox_browse.py`
- Modify: `tools/riscv/megrez_firefox_browse.py`

- [x] Add a failing orchestration test requiring `run_firefox_browse` to pass
  `page_timeout=650`, `finalize_timeout=120`, and `ack_timeout=60` as separate
  `run_baidu_home` arguments.
- [x] Run the focused Firefox test and verify it fails because the production
  protocol still exposes one `browse_timeout`.
- [x] Add a failing real-command test requiring `--timeout 650`, outer
  `/usr/bin/timeout 770`, and a host `_run_long_step` timeout of 830 seconds.
- [x] Run the focused real-command test and verify it fails because the outer
  guest and host deadlines still have only ten seconds of headroom.
- [x] Update `FirefoxBrowseConfig`, `FirefoxBrowseOperations`, and the fake
  operation to use the three named budgets without changing the evidence
  retrieval or recovery interfaces.
- [x] Implement the minimal command arithmetic, retain status 124 as failure,
  and keep the wrapper-expanded command at no more than 768 bytes.
- [x] Run the focused Firefox tests, then all selected regression modules in the
  persistent development container with `ResourceWarning` promoted to an
  error.  Also run Python bytecode compilation, generated-command `bash -n`,
  Stage-1 `bash -n`, and `git diff --check`.
- [x] Commit the deadline split (`284b1bb62`), then run exactly one unattended
  physical Firefox transaction using the existing attested MMC artifacts.
  Require the strict URL/TLS/DOM/framebuffer hashes, latched proxy readiness,
  one boot, zero Firefox restarts, and automatic recovery to U-Boot before
  publishing.

## Successful physical validation (2026-09-11)

The final code passed 427 selected tests in 23.227 seconds in the persistent
development container with `ResourceWarning` promoted to an error.  The focused
Firefox module passed 17 tests.  Python bytecode compilation, Stage-1 and
generated page-command `bash -n`, and `git diff --check` passed.  The actual
wrapper-expanded page command remained 693 bytes and the observed host page
deadline was 830 seconds.  Normal review against the maintainability,
development, security, and hardware indexes found no remaining issue.

Exactly one post-split physical transaction ran with the unchanged, attested
MMC deployment.  Evidence is under
`target/megrez-desktop/evidence-6b3037dc/browse-d87ae5223ab1231e`:

- `passed=true`, `reason=baidu-home-ready`, `failure=""`, and total time
  880.244 seconds;
- one physical boot, Firefox PID 111, zero Firefox restarts, and automatic
  recovery to a new U-Boot prompt;
- latched `proxy_bridge.ready=true` and MMC-only kernel/initramfs/DTB transport;
- exact `https://www.baidu.com/` identity with `tls=verified`, completed DOM,
  and a visually complete 1920x1080 framebuffer PNG;
- serial `A_WEB_CONNECT_RETRIES count=378`,
  `BOOT_DOM_READY guest_monotonic_ns=736569458000`, and nonce-bound
  `step=baidu-home status=0`;
- page JSON SHA-256
  `47cd1015082134ac6e8241e2f6760ffff3bd8621300d8ad91fbdc2aa8ade1018`,
  framebuffer PNG SHA-256
  `74a393d5bfe7a7cea369c659cbafc2bd5b1421505675e7a48378c02f424ff789`,
  and serial SHA-256
  `9ebcf6b5b8f085735189647c3fc98b813c5fe52ab47ff0ffa9efd4b8d6334740`.

All seven entries in the published `sha256sums.txt` verified successfully.  No
MMC partition was rewritten during implementation or physical validation.

The fetched `asterinas-riscv/main` commit `67a2fd1a8` was an ancestor with zero
remote-only commits.  The branch was published by ordinary fast-forward push;
the first post-push fetch matched the code and validation-record commit
`88e5d2aa0` exactly.
