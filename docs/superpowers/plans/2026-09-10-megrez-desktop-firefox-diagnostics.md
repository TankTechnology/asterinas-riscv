# Megrez one-command desktop and Firefox diagnostics implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Start the existing MMC-backed Megrez Debian desktop with one configured command and classify the first missing physical Firefox `WebDriver:NewSession` boundary with one bounded, evidence-rich experiment.

**Architecture:** Add a strict host-side desktop bundle and lifecycle runner that reuses `RealBootCycleOperations` and the already attested MMC artifacts.  The diagnostic action uses only the Firefox transport client and bounded snapshot collector already installed in the frozen partition-2 root, sends short acknowledged shell commands, classifies scalar transport progress, and always collects evidence and recovers.  No routine path builds, uploads, boots RockOS, writes partition 2, or changes U-Boot environment.

**Tech Stack:** Python 3 dataclasses/protocols/argparse, existing Megrez serial and MMC adapters, existing Debian guest Python tools, `unittest`, persistent Asterinas Docker container, QEMU RISC-V, physical Megrez.

---

## File structure

- Create `tools/riscv/megrez_desktop.py`: strict bundle schema, start and diagnostic lifecycles, transport classifier, publishers, real adapters, and CLI.
- Create `tools/riscv/tests/test_megrez_desktop.py`: bundle, classifier, shell-command, lifecycle, publisher, cost-budget, and CLI tests.
- Modify `Makefile`: add the focused host test target.
- Modify `tools/riscv/README.md`: document configure/start/diagnose commands, cost limits, claim boundary, and recovery behavior.
- Create generated evidence only below `target/megrez-desktop/`; never commit it.

The frozen Debian root already contains these required inputs, so this plan does
not alter or reinstall it:

```text
/usr/lib/asterinas/browser_m5_marionette_gate.py
/usr/lib/asterinas/firefox-diagnostic-snapshot
/usr/lib/asterinas/physical-graphics-gate
```

### Task 1: Strict configured desktop bundle

**Files:**
- Create: `tools/riscv/megrez_desktop.py`
- Create: `tools/riscv/tests/test_megrez_desktop.py`

- [x] **Step 1: Write failing bundle tests**

Add tests that construct temporary plan, attestation, and measurement files and
exercise this public API:

```python
bundle = desktop.configure_bundle(
    plan_path=plan_path,
    device="/dev/serial/by-id/usb-test",
    deployment_attestation_path=attestation_path,
    deployment_measurement_log_path=measurement_path,
    mmc_artifacts=MMC_ARTIFACTS,
    evidence_root=evidence_root,
    destination=destination,
)
self.assertEqual(desktop.DesktopBundle.from_path(destination), bundle)
self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
```

Require exact schema fields, canonical JSON, duplicate-key rejection, regular
non-symlink inputs, SHA-256 binding, safe MMC names, absolute stable serial
path, absolute evidence root, mode `0600`, atomic replacement, and failure
without serial I/O.  Add negative tests for a changed plan, measurement log,
attestation, relative serial path, unsafe artifact name, Boolean schema value,
unknown field, duplicate JSON key, symlink, and stale temporary file.

- [x] **Step 2: Run the bundle tests and observe RED**

Run:

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_desktop.DesktopBundleTests -v
```

Expected: import or attribute failures because `megrez_desktop` and
`DesktopBundle` do not exist.

- [x] **Step 3: Implement the minimal strict bundle**

Implement the immutable `DesktopBundle` fields exercised in Step 1, its
`from_path` and `canonical_bytes` methods, and `configure_bundle` with the
keyword-only arguments shown in the Step 1 call.  Keep all serialized paths as
absolute strings and all digests as lowercase 64-character SHA-256 values.

Reuse `safe_artifact_name`, `_read_plan`,
`_read_deployment_attestation`, `PinnedOutputDirectory`, and bounded regular
file reads rather than duplicating their contracts.  Parse JSON with an
`object_pairs_hook` that rejects duplicate keys.  Hold and hash open regular
files, write a mode-`0600` temporary file in the destination directory, fsync,
and replace atomically.

- [x] **Step 4: Run bundle tests and observe GREEN**

Run the command from Step 2.  Expected: all `DesktopBundleTests` pass.

- [x] **Step 5: Commit the bundle slice**

```bash
git add tools/riscv/megrez_desktop.py tools/riscv/tests/test_megrez_desktop.py
git commit -m "feat(riscv): configure immutable Megrez desktop bundles"
```

### Task 2: One-command desktop start lifecycle

**Files:**
- Modify: `tools/riscv/megrez_desktop.py`
- Modify: `tools/riscv/tests/test_megrez_desktop.py`

- [x] **Step 1: Write failing start lifecycle tests**

Define a fake `DesktopOperations` that records calls.  Require this exact
successful order:

```python
self.assertEqual(events, [
    "invalidate", "open", "ensure-artifacts", "boot",
    "readiness", "publish:desktop-ready", "close",
])
self.assertTrue(result.passed)
self.assertFalse(result.recovered)
self.assertNotIn("request-reboot", events)
```

Also require that start bootargs contain no
`asterinas.mmc_write_partition2`, network fixture, duplicate console,
`asterinas.reboot_after`, or probe mode.  They must retain isolated-root
systemd startup and `asterinas.klog_capture=info`.  Add failure tests proving
that a started guest attempts diagnostics, forced reboot, and fresh-U-Boot
recovery, while a pre-boot failure never sends a reboot command.  Verify that
firmware/SBI loss becomes `manual-reset-required` rather than a false pass.

- [x] **Step 2: Run the start tests and observe RED**

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_desktop.DesktopStartLifecycleTests -v
```

Expected: failures for missing `desktop_start_bootargs`, `DesktopStartConfig`,
`DesktopStartResult`, and `run_desktop_start`.

- [x] **Step 3: Implement the minimal start lifecycle**

Add the `DesktopOperations` protocol with `guest_started` and `transcript`
properties plus `open`, `ensure_artifacts`, `boot`, `prove_boot_readiness`,
`collect_diagnostics`, `request_reboot`, `await_recovery`, and `close` methods.
Add `desktop_start_bootargs(plan)` and
`run_desktop_start(plan, config, operations, publisher, clock=time.monotonic)`.
The fake in Step 1 defines the exact externally observable method contract.

Reuse `RealBootCycleOperations` for the real adapter.  On success, synchronize
the serial log, publish phase durations and readiness, close the descriptor,
and leave the guest running.  On failure after guest start, collect bounded
diagnostics and attempt recovery in `finally`-style control flow before
publishing.  Never treat recovery as desktop readiness.

- [x] **Step 4: Run start tests and the existing stability tests**

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_desktop.DesktopStartLifecycleTests \
  tools.riscv.tests.test_megrez_boot_stability.BootStabilityLifecycleTests -v
```

Expected: all tests pass.

- [x] **Step 5: Commit the start lifecycle**

```bash
git add tools/riscv/megrez_desktop.py tools/riscv/tests/test_megrez_desktop.py
git commit -m "feat(riscv): add bounded Megrez desktop startup"
```

### Task 3: NewSession transport classifier

**Files:**
- Modify: `tools/riscv/megrez_desktop.py`
- Modify: `tools/riscv/tests/test_megrez_desktop.py`

- [x] **Step 1: Write failing classifier tests using real frame semantics**

Create scalar records matching the existing
`A_WEB_MARIONETTE_TRANSPORT` format.  Cover greeting absence, successful
Status followed by no NewSession send, send complete with zero response-header
bytes, partial header, partial body, complete response, invalid request order,
duplicate terminal record, malformed JSON, oversized record, wrong command,
and truncated final serial line.

The main assertion shape is:

```python
value = desktop.classify_new_session_transcript(transcript)
self.assertEqual(value.boundary, "new-session-response-absent")
self.assertEqual(value.new_session_request_id, 1)
self.assertTrue(value.send_complete)
self.assertEqual(value.response_header_bytes, 0)
```

Add a sanitized replay representing `mmc-graphics-final-19`; because that old
run lacks transport records, it must classify as `evidence-incomplete`, never
as a TCP, poll, or Firefox deadlock.

- [x] **Step 2: Run classifier tests and observe RED**

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_desktop.FirefoxBoundaryClassifierTests -v
```

Expected: missing classifier/type failures.

- [x] **Step 3: Implement strict record parsing and classification**

Add immutable `FirefoxBoundaryEvidence` fields for `boundary`, Status
completion, NewSession request ID, send completion, header byte count,
expected/received body bytes, and selected-command seconds.  Add
`parse_marionette_transport_records(transcript)` and
`classify_new_session_transcript(transcript)` returning that exact type.

Accept only the existing exact fields and events.  Pair `begin`,
`send_complete`, `frame_header`, `complete`/`failure` by request ID and command;
reject reordered or contradictory progress.  Derive durations only from guest
monotonic values in the records.  Do not infer scheduler, TCP, poll, or Firefox
causality from a transport boundary.

- [x] **Step 4: Run classifier and existing Marionette tests**

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_desktop.FirefoxBoundaryClassifierTests \
  tools.riscv.tests.test_marionette_diagnostics -v
```

Expected: all tests pass.

- [x] **Step 5: Commit the classifier**

```bash
git add tools/riscv/megrez_desktop.py tools/riscv/tests/test_megrez_desktop.py
git commit -m "test(riscv): classify Firefox NewSession progress"
```

### Task 4: Short guest diagnostic command protocol

**Files:**
- Modify: `tools/riscv/megrez_desktop.py`
- Modify: `tools/riscv/tests/test_megrez_desktop.py`

- [x] **Step 1: Write failing command-contract tests**

Require a tuple of individually acknowledged commands, each at most the
existing 768-byte serial-command limit.  Assert that it:

- validates the original Firefox PID and zero restart count;
- runs `WebDriver:Status` before `WebDriver:NewSession`;
- sets only `ASTERINAS_MARIONETTE_DIAGNOSTICS=1` and never
  `ASTERINAS_MARIONETTE_DEBUG_ERRORS`;
- launches one delayed during-snapshot outside the selected command process;
- captures before/during/after with the installed
  `/usr/lib/asterinas/firefox-diagnostic-snapshot`;
- uses fixed limits: three seconds, 16 processes, 128 threads, 64 fds, 8192
  bytes per file, and 262144 bytes total;
- emits nonce-bound size/SHA-256/base64 frames and removes temporary files;
- never contains credentials, page payload, a partition mount/write, package
  command, RockOS command, or U-Boot command.

- [x] **Step 2: Run command tests and observe RED**

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_desktop.FirefoxGuestCommandTests -v
```

Expected: missing `firefox_diagnostic_commands` and frame parser failures.

- [x] **Step 3: Implement commands and bounded frame parsing**

Use short `python3 -c` invocations with
`PYTHONPATH=/usr/lib/asterinas` to call the already installed
`status_once` and `_connect` functions.  Use the exact NewSession parameters:

```python
{"pageLoadStrategy": "none", "strictFileInteractability": True}
```

The selected command gets one 300-second absolute deadline.  Create the
during-snapshot worker before issuing NewSession, wait for it after the
selected command, then collect the terminal snapshot.  Frame snapshots with
fresh 16-hex nonces and verify declared size/SHA-256 on the host before JSON
parsing.  Report unsupported/disabled proc fields explicitly.

- [x] **Step 4: Run guest-command, snapshot, and debug-console tests**

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_desktop.FirefoxGuestCommandTests \
  tools.riscv.tests.test_firefox_diagnostic_snapshot \
  tools.riscv.tests.test_debian_debug_console.DebugConsoleProtocolTests -v
```

Expected: all tests pass.

- [x] **Step 5: Commit the guest protocol**

```bash
git add tools/riscv/megrez_desktop.py tools/riscv/tests/test_megrez_desktop.py
git commit -m "feat(riscv): collect bounded physical Firefox evidence"
```

### Task 5: Cost-gated diagnostic lifecycle and publisher

**Files:**
- Modify: `tools/riscv/megrez_desktop.py`
- Modify: `tools/riscv/tests/test_megrez_desktop.py`

- [x] **Step 1: Write failing diagnostic lifecycle tests**

Use fake operations to require this order on success and every post-boot
failure:

```python
["invalidate", "open", "ensure-artifacts", "boot", "readiness",
 "firefox-preflight", "status", "snapshot-before", "new-session",
 "snapshot-during", "snapshot-after", "diagnostics", "request-reboot",
 "recovery", "publish", "close"]
```

Require a result to contain the tested hypothesis, contrary result, all input
hashes, transfer byte count, QEMU run count, physical boot count, total host
seconds, selected-command guest seconds, boundary evidence, three snapshot
hashes, diagnostic hash, serial hash, Firefox PID identity, restart count, and
recovery state.  Add tests that reject a second run ledger entry with the same
experiment identity, reject any unchanged-identity transfer byte count above
zero, fail closed on missing/invalid snapshot or transport evidence, and still
attempt recovery after timeout, malformed output, interruption, or diagnostic
collection failure.

- [x] **Step 2: Run lifecycle tests and observe RED**

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_desktop.FirefoxDiagnosticLifecycleTests -v
```

Expected: missing lifecycle/result/publisher failures.

- [x] **Step 3: Implement one-run admission, lifecycle, and publication**

Add immutable `FirefoxDiagnosticConfig` fields `hypothesis`,
`contrary_outcome`, `selected_command_timeout=300.0`, and
`total_timeout=900.0`.  Add
`experiment_identity(bundle, plan, config)` and
`run_firefox_diagnosis(plan, config, operations, publisher,
clock=time.monotonic)`.

Store a mode-`0600` admission ledger under the configured private evidence
root.  Lock it for the admission check and final append.  A failed tool run is
still an executed identity and cannot be silently retried.  Publisher output
directories are mode `0700`, must be empty/new, and receive `result.json` last.
Reuse boot diagnostics and recovery from `RealBootCycleOperations`; add only
the short Firefox command/frame exchange methods.

- [x] **Step 4: Run all focused desktop tests**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_desktop -v
```

Expected: all tests pass without warnings.

- [x] **Step 5: Commit the diagnostic lifecycle**

```bash
git add tools/riscv/megrez_desktop.py tools/riscv/tests/test_megrez_desktop.py
git commit -m "feat(riscv): bound Megrez Firefox diagnosis"
```

### Task 6: CLI, focused Make target, and operator documentation

**Files:**
- Modify: `tools/riscv/megrez_desktop.py`
- Modify: `tools/riscv/tests/test_megrez_desktop.py`
- Modify: `Makefile`
- Modify: `tools/riscv/README.md`

- [x] **Step 1: Write failing CLI tests**

Require these public forms and reject mixed/missing action arguments:

```text
python3 -m tools.riscv.megrez_desktop configure \
  --plan /work/plan.json --device /dev/serial/by-id/usb-test \
  --deployment-attestation /work/deployment-attestation.json \
  --deployment-measurement-log /work/deployment-measurement.serial.log \
  --mmc-kernel asterinas-current.Image \
  --mmc-initramfs asterinas-current-stage1.cpio \
  --mmc-dtb dtbs/linux/eswin/eic7700-milkv-megrez.dtb \
  --evidence-root /work/evidence --output /work/current.json
python3 -m tools.riscv.megrez_desktop start --bundle /work/current.json
python3 -m tools.riscv.megrez_desktop diagnose-firefox \
  --hypothesis "Status completes and NewSession returns no header" \
  --contrary-outcome "Status fails, send fails, response begins, or call completes" \
  --bundle /work/current.json
```

The default bundle is `target/megrez-desktop/current.json`.  The default run
directory is a fresh nonce-suffixed child of the bundle's evidence root.
`start` and `diagnose-firefox` must not expose deployment, root-image, MMC-write,
or credential flags.  Patch real adapters in CLI tests; opening a real serial
device is forbidden.

- [x] **Step 2: Run CLI tests and observe RED**

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_desktop.DesktopCliTests -v
```

Expected: parser/action failures.

- [x] **Step 3: Implement CLI and Make target**

Add a strict subparser per action, instantiate real publishers/adapters only
after every local input validates, print the single canonical result to stdout,
and return 0 only for the action's exact pass contract.  Add:

```make
.PHONY: test_riscv_megrez_desktop_unit
test_riscv_megrez_desktop_unit:
	python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_megrez_desktop -v
```

- [x] **Step 4: Document exact stable commands**

Document one initial configure command using the existing
`plan-isolated-resolved.json`, attestation, measurement log, stable FTDI path,
and three versioned MMC names.  Document the one-word `start` and bounded
`diagnose-firefox` actions, the fresh-profile limitation, 15-minute cap,
one-run identity ledger, private evidence location, no-transfer guarantee, and
manual-reset boundary.

- [x] **Step 5: Run unit targets and formatting checks**

```bash
tools/docker/run_dev_container.sh -- \
  make test_riscv_megrez_desktop_unit
tools/docker/run_dev_container.sh -- \
  make test_riscv_megrez_boot_stability_unit \
       test_riscv_physical_graphics_unit \
       test_riscv_megrez_probe_unit
python3 -m py_compile tools/riscv/megrez_desktop.py \
  tools/riscv/tests/test_megrez_desktop.py
ruff check tools/riscv/megrez_desktop.py tools/riscv/tests/test_megrez_desktop.py
ruff format --check tools/riscv/megrez_desktop.py \
  tools/riscv/tests/test_megrez_desktop.py
git diff --check
```

Expected: every command exits zero and the persistent container is reused
without installing or downloading anything.

- [x] **Step 6: Commit the operator surface**

```bash
git add Makefile tools/riscv/megrez_desktop.py \
  tools/riscv/tests/test_megrez_desktop.py tools/riscv/README.md
git commit -m "docs(riscv): add one-command Megrez desktop workflow"
```

### Task 7: Replay, exact-current QEMU control, and normal review

**Files:**
- Modify only if a failing regression identifies a defect in Task 1-6 files.
- Evidence only: `target/megrez-desktop/replay-*`,
  `target/megrez-desktop/qemu-*`

- [ ] **Step 1: Replay the retained physical failure without booting**

Run the classifier against
`target/current-main-physical-graphics/physical/mmc-graphics-final-19/physical.serial.log`.
Expected: `evidence-incomplete`, because that immutable run predates the
transport records.  Record elapsed time below two minutes and zero guest runs.

- [ ] **Step 2: Build no artifacts unless identity checks require it**

Verify the current kernel, old immutable root, Stage1, DTB, U-Boot, and package
metadata hashes.  If the current tracked source kernel is not already built,
use only the persistent container and offline build:

```bash
tools/docker/run_dev_container.sh --offline -- \
  make kernel TARGET_ARCH=riscv64 SMP=4
```

Do not rebuild the rootfs and do not run apt, Nix downloads, Cargo installation,
or Docker image/container deletion.

- [ ] **Step 3: Run one exact-current QEMU control**

Use the existing physical-graphics QEMU gate with the current kernel and the
frozen browser-web root.  Set one 15-minute host deadline and a new private
output directory.  Expected: either the existing three-cycle QEMU pass or one
retained first-failure result; do not repeat unchanged inputs.

- [ ] **Step 4: Perform normal review**

Review `git diff "$(git merge-base origin/main HEAD)" HEAD` for lifecycle cleanup, fail-open result
paths, unbounded reads/waits, path traversal, secret leakage, ambiguous clock
domains, repeated-run admission races, and accidental partition/network/build
operations.  Add a failing regression before correcting each confirmed defect.
Do not invoke the deleted repository review skill.

- [ ] **Step 5: Rerun only affected focused tests and commit fixes**

Run the Task 6 checks plus any directly affected existing test module.
Expected: all pass.  Commit confirmed fixes as one normal-review hardening
commit; make no commit when review finds no defect.

### Task 8: One information-rich physical diagnosis and causal next step

**Files:**
- Evidence only until a minimal Linux/Asterinas reproducer proves a kernel bug.
- Possible later regression: the exact kernel subsystem selected by the
  `diagnose-firefox` boundary; do not choose it in advance.

- [ ] **Step 1: Configure the exact deployed bundle once**

Run `configure` with the current plan, stable FTDI path, RockOS attestation and
measurement log, versioned MMC kernel/Stage1/DTB names, and a private evidence
root.  Expected: local validation and a mode-`0600` bundle; no serial I/O.

- [ ] **Step 2: Stage only changed boot artifacts if needed**

If and only if the current plan names a new kernel or Stage1 identity, boot
RockOS once, transfer just those versioned files over the existing network
path, verify size and SHA-256, publish a fresh attestation, and reboot.  Do not
touch partition 2.  If identities already match, transfer zero bytes and skip
RockOS.

- [ ] **Step 3: Run exactly one physical diagnostic identity**

Use the hypothesis:

```text
The physical Firefox Marionette listener and Status command complete, but
WebDriver:NewSession is fully sent and produces no response-header byte before
the fixed 300-second deadline.
```

Use the contrary outcome:

```text
Status does not complete, NewSession is not fully sent, a response frame is
partial, or NewSession returns a valid session.
```

Run `diagnose-firefox` once.  Expected: a complete boundary classification,
three bounded snapshots, full hashes, physical boot count 1, QEMU run count 0,
unchanged-identity transfer bytes 0, and fresh-U-Boot recovery.  Do not rerun
the same experiment identity.

- [ ] **Step 4: Select the next smallest experiment from the result**

- `new-session-response-absent` plus one stable outstanding syscall: write one
  Linux/Asterinas microtest for that exact wait/wakeup path.
- continuing thread/transport progress beyond the deadline: measure cold
  profile initialization with a nonpersistent tmpfs cache control; do not call
  it a kernel correctness bug.
- Status failure: test the greeting/Status transport path without NewSession.
- partial response: test only the exact framing/read path.
- `new-session-complete`: stop diagnosis and run the existing physical input
  and HDMI acceptance gate.
- `evidence-incomplete`: repair the observer with a failing retained-data
  regression; do not boot Firefox again.

- [ ] **Step 5: Implement a fix only after causal proof**

For a kernel result, first run one common executable on Linux and Asterinas;
require Linux PASS and Asterinas FAIL.  Add the permanent regression, implement
one semantic fix, run the regression to GREEN, then run the relevant subsystem
suite.  For a Firefox-specific or performance result, keep kernel semantics
unchanged and implement only the measured startup correction.

- [ ] **Step 6: Run final physical acceptance and publish**

After the causal fix, run the exact three-cycle physical nonce, USB keyboard,
relative mouse, DOM, guest screenshot, HDMI capture, stable Firefox PID, zero
restart, and recovery gate.  Verify `result.json`, all evidence hashes, source
commit, artifact identities, file modes, and clean worktree.  Only then push a
fast-forward update to `asterinas-riscv` remote `main`.
