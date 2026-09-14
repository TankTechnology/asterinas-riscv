# Firefox Network Gate Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the useful Firefox/QEMU follow-ups from the remote IPv6 branch, make browser runs bounded and observable, and leave `origin/main` with reproducible desktop and simple-web evidence.

**Architecture:** Preserve the existing schema-seven root and one-VM gate. Serialize the owned network fixture before Firefox, constrain optional Firefox processes, stream only structured serial markers to a live progress artifact, and translate host timeouts into phase-specific failure reasons. Keep the Stage-1 debug-console shutdown bound in a separate commit.

**Tech Stack:** Python 3 `unittest`, Bash/systemd units, the existing QEMU gate runtime, persistent Docker/rootfs caches, RISC-V Asterinas release images.

---

### Task 1: Serialize the owned network qualification before Firefox

**Files:**
- Modify: `tools/riscv/debian/rootfs/desktop_m5_qemu_gate.py`
- Modify: `tools/riscv/debian/rootfs/browser_web.service`
- Modify: `tools/riscv/debian/rootfs/build_rootfs.sh`
- Test: `tools/riscv/tests/test_debian_m5_network.py`
- Test: `tools/riscv/tests/test_debian_browser_web.py`

- [ ] **Step 1: Write failing contract tests**

Add assertions that the browser network boot arguments carry a 300-second
guest network budget, that `browser_web.service` both `Requires` and orders
itself `After` the network service, and that the browser root installs this
drop-in:

```ini
[Service]
TimeoutStartSec=600s
```

Rename the existing browser dependency test to
`test_browser_waits_for_network_and_evidence_waits_for_desktop` because the
old `without_hard_link` name describes behavior this task intentionally
replaces.

- [ ] **Step 2: Prove the tests fail on current main**

Run:

```bash
python3 -m unittest \
  tools.riscv.tests.test_debian_m5_network.DebianDesktopM5NetworkTests.test_qemu_classifier_and_adapter_bind_network_before_desktop \
  tools.riscv.tests.test_debian_browser_web.BrowserWebContractTests.test_browser_waits_for_network_and_evidence_waits_for_desktop
```

Expected: failure because the current guest budget is 120 seconds and the
browser service has only a soft parallel dependency.

- [ ] **Step 3: Implement the sequencing contract**

Define `QEMU_WEB_NETWORK_TIMEOUT_SECONDS = 300`, use it only in
`qemu_web_network_bootargs`, add the hard browser dependency, and install
`etc/systemd/system/asterinas-desktop-m5-network.service.d/browser-web.conf`
only for the `browser-web` profile. Do not change the generic M5 timeout.

- [ ] **Step 4: Run focused and surrounding tests**

```bash
python3 -m unittest \
  tools.riscv.tests.test_debian_m5_network \
  tools.riscv.tests.test_debian_browser_web
bash -n tools/riscv/debian/rootfs/build_rootfs.sh
```

Expected: all tests pass.

- [ ] **Step 5: Commit the isolated change**

```bash
git add tools/riscv/debian/rootfs/desktop_m5_qemu_gate.py \
  tools/riscv/debian/rootfs/browser_web.service \
  tools/riscv/debian/rootfs/build_rootfs.sh \
  tools/riscv/tests/test_debian_m5_network.py \
  tools/riscv/tests/test_debian_browser_web.py
git commit -m "Serialize Firefox after network qualification"
```

### Task 2: Reduce optional Firefox process pressure

**Files:**
- Modify: `tools/riscv/debian/rootfs/browser_web_firefox.sh`
- Test: `tools/riscv/tests/test_debian_browser_web.py`

- [ ] **Step 1: Write a failing preference test**

Require the generated online profile to contain exactly these additional
preferences:

```javascript
user_pref("dom.ipc.processCount", 1);
user_pref("dom.ipc.processPrelaunch.enabled", false);
user_pref("fission.autostart", false);
user_pref("media.rdd-process.enabled", false);
```

Keep the existing evidence assertion that at least one separate content
process is present and audited.

- [ ] **Step 2: Prove the test fails**

```bash
python3 -m unittest \
  tools.riscv.tests.test_debian_browser_web.BrowserWebContractTests.test_firefox_proxy_profile_is_explicit \
  tools.riscv.tests.test_debian_browser_web.BrowserWebContractTests.test_firefox_direct_profile_removes_proxy_state
```

Expected: failure listing the four missing preferences.

- [ ] **Step 3: Add only the four measured low-pressure preferences**

Place them with the existing network/background-work preferences in
`configure_network_profile`. Retain `pageLoadStrategy: "none"`; current main
already contains the final asynchronous-navigation behavior of remote commit
`4e2229d11`, so that remote patch is recorded as already satisfied instead of
being cherry-picked.

- [ ] **Step 4: Verify and commit**

```bash
bash -n tools/riscv/debian/rootfs/browser_web_firefox.sh
python3 -m unittest tools.riscv.tests.test_debian_browser_web
git add tools/riscv/debian/rootfs/browser_web_firefox.sh \
  tools/riscv/tests/test_debian_browser_web.py
git commit -m "Reduce optional Firefox process pressure"
```

### Task 3: Stream structured browser progress during QEMU execution

**Files:**
- Modify: `tools/riscv/debian/rootfs/gate_runtime.py`
- Modify: `tools/riscv/debian/rootfs/rootfs_gate_backend.py`
- Modify: `tools/riscv/debian/rootfs/browser_web_qemu_gate.py`
- Test: `tools/riscv/tests/test_debian_rootfs.py`
- Test: `tools/riscv/tests/test_debian_browser_web.py`

- [ ] **Step 1: Write failing observer and progress tests**

Add a `SerialConsole` test proving an observer receives the exact bytes that
were admitted to the capped transcript. Add browser tests that feed split
serial chunks such as:

```text
DEBIAN_WEB_NETWORK_READY mode=direct layers=10
A_WEB_PHASE phase=navigate-baidu-home state=start firefox_pid=70
A_WEB_PHASE phase=navigate-baidu-home state=done firefox_pid=70
```

The reporter must publish only completed structured lines, retain
`last_marker`, `active_phase`, and `sequence`, and clear `active_phase` on the
matching `done` line.

- [ ] **Step 2: Prove both tests fail**

```bash
python3 -m unittest \
  tools.riscv.tests.test_debian_rootfs.DebianRootfsGateRuntimeTests.test_serial_console_observes_admitted_chunks \
  tools.riscv.tests.test_debian_browser_web.BrowserWebContractTests.test_qemu_progress_tracks_split_structured_lines
```

Expected: `SerialConsole` has no observer API and the browser progress reporter
does not exist.

- [ ] **Step 3: Add the generic observer hook**

Extend `SerialConsole.__init__` with
`observer: Callable[[bytes], None] | None = None`. Call the observer from
`_append` only after the byte-cap check and transcript append succeed. Add a
`ConcreteOperations.serial_observer(config, boot_number)` hook returning
`None`, and pass its result when constructing the console.

- [ ] **Step 4: Add the browser-only structured reporter**

Implement a line-buffering `BrowserWebProgress` in
`browser_web_qemu_gate.py`. Accept only the prefixes
`DEBIAN_WEB_NETWORK_`, `DEBIAN_BROWSER_WEB_`, `A_WEB_TIMELINE`, and
`A_WEB_PHASE`. For every accepted line, atomically replace
`browser-web-progress.json` and print one `browser-web-progress:` line to
stderr with `flush=True`. Never mirror arbitrary page text or raw serial data.

- [ ] **Step 5: Verify and commit**

```bash
python3 -m unittest \
  tools.riscv.tests.test_debian_rootfs.DebianRootfsGateRuntimeTests \
  tools.riscv.tests.test_debian_browser_web
git add tools/riscv/debian/rootfs/gate_runtime.py \
  tools/riscv/debian/rootfs/rootfs_gate_backend.py \
  tools/riscv/debian/rootfs/browser_web_qemu_gate.py \
  tools/riscv/tests/test_debian_rootfs.py \
  tools/riscv/tests/test_debian_browser_web.py
git commit -m "Publish live Firefox QEMU progress"
```

### Task 4: Bound the browser protocol and classify its timeout phase

**Files:**
- Modify: `tools/riscv/debian/rootfs/desktop_m3_gate.py`
- Modify: `tools/riscv/debian/rootfs/browser_web_qemu_gate.py`
- Modify: `Makefile`
- Test: `tools/riscv/tests/test_debian_browser_web.py`

- [ ] **Step 1: Write failing timeout-classification tests**

Exercise these exact classifications:

```text
browser-timeout:network-direct
browser-timeout:firefox-launch
browser-timeout:phase-navigate-baidu-home
browser-timeout:after-platform-ready
```

The phase case must be derived from a `state=start` line without its matching
`state=done`. The platform case must be derived from
`DEBIAN_BROWSER_WEB_PLATFORM_READY` with no final content/pass marker.

- [ ] **Step 2: Prove the tests fail**

```bash
python3 -m unittest \
  tools.riscv.tests.test_debian_browser_web.BrowserWebContractTests.test_qemu_timeout_reason_identifies_last_browser_phase
```

Expected: the timeout classifier is absent.

- [ ] **Step 3: Add a browser-specific host bound**

Define `BROWSER_WEB_PROTOCOL_TIMEOUT_SECONDS = 900`. Add a protected
`protocol_deadline(config)` hook to the generic M3 protocol that returns no
overall deadline by default, preserving every existing gate's two-stage wait
semantics. The browser override returns one absolute monotonic deadline using
the smaller of the caller's timeout and 900 seconds; both the U-Boot and final
milestone waits must share it. Catch `TimeoutError`, inspect the retained
transcript, and raise `GateFailure` with one of the stable reasons above. Do not
change the 540/480-second guest page/formal budgets and do not import the remote
1200-second timeout-only patch.

Change the Makefile browser gate invocation from `--boot-timeout 7200` to
`--boot-timeout 900`; other Debian gates keep their existing values.

- [ ] **Step 4: Verify and commit**

```bash
python3 -m unittest tools.riscv.tests.test_debian_browser_web
git diff --check
git add Makefile tools/riscv/debian/rootfs/desktop_m3_gate.py \
  tools/riscv/debian/rootfs/browser_web_qemu_gate.py \
  tools/riscv/tests/test_debian_browser_web.py
git commit -m "Bound Firefox QEMU protocol failures"
```

### Task 5: Bound Stage-1 debug-console shutdown independently

**Files:**
- Modify: `tools/riscv/debian/rootfs/stage1_debug_console.c`
- Test: `tools/riscv/tests/test_debian_rootfs.py`

- [ ] **Step 1: Replace the flaky test with the actual service contract**

Keep a subprocess test showing the installed TERM trap exits after Bash is
woken from its interactive read, and add an exact generated-unit assertion for:

```ini
TimeoutStopSec=2s
SendSIGKILL=yes
```

This models Bash's documented deferred trap execution while guaranteeing that
systemd cannot leave the debug console blocking shutdown.

- [ ] **Step 2: Prove the service-bound assertion fails**

```bash
python3 -m unittest \
  tools.riscv.tests.test_debian_rootfs.DebianStage1Tests.test_debug_console_exits_on_sigterm \
  tools.riscv.tests.test_debian_rootfs.DebianStage1Tests.test_debug_console_runtime_tree_is_exact
```

Expected: the generated unit lacks the two stop-bound settings.

- [ ] **Step 3: Implement the bounded fallback**

Add `TimeoutStopSec=2s` and `SendSIGKILL=yes` to the generated service. Keep
the TERM trap so normal idle shutdown can still exit with status zero; the
two-second SIGKILL is only the deterministic fallback for Bash's prompt/read
race.

- [ ] **Step 4: Verify and commit**

```bash
python3 -m unittest \
  tools.riscv.tests.test_debian_rootfs.DebianStage1Tests.test_debug_console_exits_on_sigterm \
  tools.riscv.tests.test_debian_rootfs.DebianStage1Tests.test_debug_console_runtime_tree_is_exact
git add tools/riscv/debian/rootfs/stage1_debug_console.c \
  tools/riscv/tests/test_debian_rootfs.py
git commit -m "Bound Stage-1 debug console shutdown"
```

### Task 6: Build, run QEMU acceptance, document, and push

**Files:**
- Modify: `docs/porting/evidence/2026-09-14-network-foundation-main-replay.md`

- [ ] **Step 1: Run final source verification**

```bash
git diff --check
for script in \
  tools/riscv/debian/rootfs/build_rootfs.sh \
  tools/riscv/debian/rootfs/browser_web_firefox.sh \
  tools/riscv/debian/rootfs/browser_web_evidence.sh; do
  bash -n "$script"
done
python3 -m ruff check \
  tools/riscv/debian/rootfs/gate_runtime.py \
  tools/riscv/debian/rootfs/rootfs_gate_backend.py \
  tools/riscv/debian/rootfs/browser_web_qemu_gate.py \
  tools/riscv/tests/test_debian_browser_web.py \
  tools/riscv/tests/test_debian_rootfs.py
python3 -m unittest tools.riscv.tests.test_debian_browser_web
make test_riscv_debian_rootfs_unit
```

Expected: all tests pass, including the formerly unstable debug-console test.

- [ ] **Step 2: Rebuild the immutable JIT root in the persistent container**

```bash
ASTERINAS_EXPLICIT_QEMU=1 tools/riscv/debian/rootfs/build_rootfs.sh \
  --profile browser-web \
  --output-dir target/debian-riscv/browser-web-jit/rootfs \
  --cache-dir target/debian-riscv/cache \
  --firefox-jit-package-dir target/debian-riscv/firefox-jit-packages
```

Expected: `FIREFOX_JIT_OVERLAY_PASS`, followed by successful schema-seven
publication. Reuse the named builder container and existing package cache.

- [ ] **Step 3: Run proxy and direct QEMU gates**

Run the Makefile browser gate with the current release kernel, frozen U-Boot,
DTB, Stage-1 archive, and rebuilt root. Use separate root-owned output
directories `firefox-network-final-proxy` and `firefox-network-final-direct`.
Set `ASTERINAS_PROXY_UPSTREAM_PORT=7890` only for proxy mode.

Expected proxy result: network ready, 20 fixture requests, deterministic text
page/capabilities, and public-page platform readiness; a validated external
CAPTCHA remains a separate non-kernel classification. Expected direct result:
the same platform boundary or a retained phase-specific timeout within 900
seconds.

- [ ] **Step 4: Record exact identities and observed boundaries**

Update the evidence note with kernel/root/manifest SHA-256 values, structured
progress behavior, QEMU result paths, fixture count, capability result, and the
direct-mode terminal classification.

- [ ] **Step 5: Review, commit evidence, and push linearly**

```bash
git add docs/porting/evidence/2026-09-14-network-foundation-main-replay.md
git commit -m "Record bounded Firefox network qualification"
git fetch origin main
git merge-base --is-ancestor origin/main HEAD
git push origin HEAD:main
git fetch origin main
test "$(git rev-parse origin/main)" = "$(git rev-parse HEAD)"
git status --porcelain=v1
```

Expected: remote `main` equals local `HEAD` and the worktree is clean.
