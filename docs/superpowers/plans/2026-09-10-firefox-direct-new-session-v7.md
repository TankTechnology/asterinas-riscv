# Firefox direct NewSession protocol-v7 implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the unsupported Marionette Status precondition and produce one bounded, hash-bound physical classification of the real Firefox `WebDriver:NewSession` path.

**Architecture:** Keep the stable Firefox PID/profile preflight and three process snapshots, but make the selected NewSession operation own listener connection, greeting, request, and response classification under one 300-second absolute deadline. Preserve protocol-v6 evidence through a historical classifier, publish the changed boundary shape as result schema 2, and use protocol version 7 so no consumed physical identity can be repeated.

**Tech Stack:** Python 3.11 dataclasses and `unittest`, strict JSON and serial framing, Firefox ESR 140 Marionette protocol, persistent Asterinas Docker development container, immutable MMC artifacts, U-Boot serial recovery.

---

## File map

- `tools/riscv/megrez_desktop.py`: protocol version, boundary model and classifier, guest command sequence, lifecycle, experiment identity, and real serial adapter.
- `tools/riscv/tests/test_megrez_desktop.py`: retained-record, command-contract, lifecycle, identity, schema, and adapter regressions.
- `tools/riscv/README.md`: operator-facing one-command behavior and boundary list.
- `docs/superpowers/plans/2026-09-10-megrez-desktop-firefox-diagnostics.md`: immutable v6 outcome and v7 execution record.
- `docs/superpowers/specs/2026-09-10-megrez-desktop-firefox-diagnostics-design.md`: approved architecture; no further design expansion belongs in this implementation.

The later network-stack merge is deliberately excluded.  It receives a separate plan only after the deterministic local Firefox gate passes.

### Task 1: Classify direct and historical Marionette responses accurately

**Files:**
- Modify: `tools/riscv/megrez_desktop.py:733-1228`
- Modify: `tools/riscv/tests/test_megrez_desktop.py:500-920`

- [x] **Step 1: Write failing boundary-model tests**

Remove all `status_complete` assertions from live NewSession cases.  Add a complete response-identity failure helper and these three regressions:

```python
def _complete_error_command(pid: int, command: str, start_ns: int) -> list[str]:
    return [
        _transport_record(
            pid=pid,
            monotonic_ns=start_ns,
            request_id=1,
            command=command,
            event="begin",
            stage="send",
        ),
        _transport_record(
            pid=pid,
            monotonic_ns=start_ns + 10,
            request_id=1,
            command=command,
            event="send_complete",
            stage="send",
            send_complete=True,
        ),
        _transport_record(
            pid=pid,
            monotonic_ns=start_ns + 20,
            request_id=1,
            command=command,
            event="frame_header",
            stage="response_body",
            send_complete=True,
            header_bytes=4,
            body_expected=662,
        ),
        _transport_record(
            pid=pid,
            monotonic_ns=start_ns + 30,
            request_id=1,
            command=command,
            event="failure",
            stage="response_identity",
            send_complete=True,
            header_bytes=4,
            body_expected=662,
            body_received=662,
            error_type="GateError",
        ),
    ]

def test_direct_greeting_may_precede_new_session_without_status(self) -> None:
    evidence = self.classify(
        _greeting(22, 100) + _complete_command(22, "WebDriver:NewSession", 200)
    )
    self.assertEqual(evidence.boundary, "new-session-complete")

def test_complete_new_session_error_is_rejected_not_partial(self) -> None:
    evidence = self.classify(
        _greeting(22, 100)
        + _complete_error_command(22, "WebDriver:NewSession", 200)
    )
    self.assertEqual(evidence.boundary, "new-session-rejected")
    self.assertEqual(evidence.response_body_received, 662)

def test_retained_complete_status_error_is_rejected_not_stalled(self) -> None:
    evidence = self.classify(
        _greeting(11, 100)
        + _complete_error_command(11, "WebDriver:Status", 200)
    )
    self.assertEqual(evidence.boundary, "status-command-rejected")
```

Keep the existing malformed, reordered, duplicate, partial-header, partial-body, retry-prefix, and no-record cases.  A greeting-only transcript must produce `new-session-not-sent`; a retry-only transcript must produce `listener-not-ready`; no transport records remain `evidence-incomplete`.

- [x] **Step 2: Run the classifier tests and verify RED**

Run:

```bash
tools/docker/run_dev_container.sh -- python3 -m unittest \
  tools.riscv.tests.test_megrez_desktop.FirefoxBoundaryClassifierTests
```

Expected: direct NewSession is rejected by the old Status ordering rule, complete errors are reported as stalled/partial, and the obsolete `status_complete` field remains present.

- [x] **Step 3: Implement the minimal boundary and ordering change**

Change `FirefoxBoundaryEvidence` to:

```python
@dataclass(frozen=True)
class FirefoxBoundaryEvidence:
    boundary: str
    new_session_request_id: int | None
    send_complete: bool
    response_header_bytes: int
    response_body_expected: int | None
    response_body_received: int
    selected_command_seconds: float | None
```

Allow exactly these boundary names:

```python
{
    "listener-not-ready",
    "status-command-stalled",
    "status-command-rejected",
    "new-session-not-sent",
    "new-session-response-absent",
    "new-session-response-partial",
    "new-session-rejected",
    "new-session-complete",
    "evidence-incomplete",
}
```

In `_validate_transport_sequence`, remove only the rule that requires a Status key whenever NewSession exists.  Retain the legacy rule that, when both exist, Status must complete before NewSession, and retain the same-PID greeting-before-command validation for every command.

Replace `classify_new_session_transcript` with the following decision order:

```python
records = parse_marionette_transport_records(transcript)
if not records:
    return _boundary_evidence("evidence-incomplete")

status = tuple(r for r in records if r.command == "WebDriver:Status")
selected = tuple(r for r in records if r.command == "WebDriver:NewSession")
if status and not selected:
    last = status[-1]
    if (
        last.event == "failure"
        and last.stage == "response_identity"
        and last.body_expected is not None
        and last.body_received == last.body_expected
    ):
        return _boundary_evidence("status-command-rejected")
    if last.event != "complete":
        return _boundary_evidence("status-command-stalled")
    return _boundary_evidence("new-session-not-sent")
if not selected:
    if any(r.command == "greeting" and r.event == "frame_header" for r in records):
        return _boundary_evidence("new-session-not-sent")
    return _boundary_evidence("listener-not-ready")

last = selected[-1]
if last.event == "complete":
    boundary = "new-session-complete"
elif (
    last.event == "failure"
    and last.stage == "response_identity"
    and last.body_expected is not None
    and last.body_received == last.body_expected
):
    boundary = "new-session-rejected"
elif not any(r.send_complete for r in selected):
    boundary = "new-session-not-sent"
elif max(r.header_bytes for r in selected) == 0:
    boundary = "new-session-response-absent"
else:
    boundary = "new-session-response-partial"
return _boundary_evidence(boundary, records=selected)
```

Remove the `status_complete` parameter and assignments from `_boundary_evidence`.

- [x] **Step 4: Run the classifier tests and retained-v6 replay**

Run the classifier test from Step 2, then:

```bash
python3 - <<'PY'
from pathlib import Path
from tools.riscv.megrez_desktop import classify_new_session_transcript

path = Path(
    "target/megrez-desktop/evidence-77d7e42c/"
    "firefox-0bd432822f91a094/physical.serial.log"
)
evidence = classify_new_session_transcript(path.read_bytes())
print(evidence.boundary)
PY
```

Expected: all classifier tests pass and retained replay prints exactly `status-command-rejected`.

- [ ] **Step 5: Commit the classifier**

```bash
git add tools/riscv/megrez_desktop.py tools/riscv/tests/test_megrez_desktop.py
git commit -m "Classify direct Firefox NewSession responses"
```

### Task 2: Replace the invalid Status gate with protocol v7

**Files:**
- Modify: `tools/riscv/megrez_desktop.py:45-60, 1274-1385, 1501-1562, 1611-1800, 1827-2045, 2260-2440`
- Modify: `tools/riscv/tests/test_megrez_desktop.py:930-1110, 1180-1668, 1735-1880`

- [x] **Step 1: Write failing command, lifecycle, identity, and schema tests**

Update `FirefoxGuestCommandTests` to require:

```python
commands = desktop.firefox_diagnostic_commands(41, self.NONCES)
joined = "\n".join(commands)
self.assertNotIn("WebDriver:Status", joined)
self.assertNotIn("status_once", joined)
self.assertEqual(joined.count("ASTERINAS_MARIONETTE_DIAGNOSTICS=1"), 1)
self.assertEqual(joined.count("ASTERINAS_MARIONETTE_DEBUG_ERRORS=1"), 1)
self.assertLess(joined.index("_asterinas_firefox_snapshot"), joined.index("WebDriver:NewSession"))
self.assertTrue(
    all(
        len(
            (
                f"{command}; printf '__ASTERINAS_FIREFOX_COMMAND__ "
                f"nonce={'4' * 16} step={index} done=1\\n'\n"
            ).encode()
        )
        <= desktop.MAX_SERIAL_COMMAND_BYTES
        for index, command in enumerate(commands)
    )
)
```

Delete the Status-operation test.  Change the real adapter expectations to preflight indexes `0..3`, before `(4, 5)`, NewSession `(6, 7)`, during `(8, 9)`, and after `(10, 11, 12, 13, 14, 15)`.

Change lifecycle `EXPECTED_EVENTS` to omit `status`; delete `status_fail` from `_DiagnosticOperations`; require `snapshot-before` and `new-session` to follow `firefox-preflight` directly.  Add:

```python
def test_protocol_v7_removes_status_and_uses_schema_two(self) -> None:
    result, events, _publisher = self.run_diagnosis()
    self.assertEqual(desktop.FIREFOX_DIAGNOSTIC_PROTOCOL_VERSION, 7)
    self.assertEqual(result.schema_version, 2)
    self.assertNotIn("status", events)
    self.assertEqual(desktop.FirefoxDiagnosticConfig(
        hypothesis="one hypothesis",
        contrary_outcome="one contrary outcome",
    ).diagnostics_timeout, 90.0)
```

Update `_diagnostic_transcript` so every live boundary begins with `_greeting(22, 300)` and then the NewSession records; retain a separate Status transcript only in Task 1's historical classifier test.

- [x] **Step 2: Run focused tests and verify RED**

Run:

```bash
tools/docker/run_dev_container.sh -- python3 -m unittest \
  tools.riscv.tests.test_megrez_desktop.FirefoxGuestCommandTests \
  tools.riscv.tests.test_megrez_desktop.FirefoxDiagnosticLifecycleTests \
  tools.riscv.tests.test_megrez_desktop.FirefoxDiagnosticPublisherTests
```

Expected: failures show the old Status command/event, protocol version 6, result schema 1, old adapter indexes, and 60-second diagnostic budget.

- [x] **Step 3: Implement the protocol-v7 command sequence**

Set:

```python
FIREFOX_DIAGNOSTIC_PROTOCOL_VERSION = 7
```

Delete `FIREFOX_STATUS_GUEST_TIMEOUT_SECONDS` and `FIREFOX_STATUS_HOST_GRACE_SECONDS`.  In `firefox_diagnostic_commands`, remove the Status command and use:

```python
environment = (
    "env -i PATH=/usr/bin:/bin PYTHONPATH=/usr/lib/asterinas "
    "ASTERINAS_MARIONETTE_DIAGNOSTICS=1 "
    "ASTERINAS_MARIONETTE_DEBUG_ERRORS=1"
)
```

Keep all remaining shell commands byte-for-byte except for the one-position index shift caused by removing Status.  Update `RealFirefoxDiagnosticOperations` to the exact index mapping asserted in Step 1.  Delete `_FIREFOX_STATUS_MARKER` and `run_firefox_status`.

- [x] **Step 4: Implement the lifecycle, config, identity, and schema change**

Delete `status_timeout` from `FirefoxDiagnosticConfig`, its deadline tuple, and `experiment_identity`.  Set:

```python
diagnostics_timeout: float = 90.0
```

Set the identity environment to:

```python
"diagnostic_environment": [
    "ASTERINAS_MARIONETTE_DIAGNOSTICS=1",
    "ASTERINAS_MARIONETTE_DEBUG_ERRORS=1",
],
```

Remove `run_firefox_status` from `FirefoxDiagnosticOperations`.  In `_run_firefox_diagnosis`, delete the entire `status_complete` block and change the selected path guard to:

```python
if firefox is not None and interruption is None:
```

The guarded sequence remains snapshot-before, NewSession, snapshot-during, snapshot-after.  Construct `FirefoxDiagnosticResult(schema_version=2, ...)` and require version 2 in its validator.

- [x] **Step 5: Run focused tests to GREEN**

Run the exact command from Step 2.

Expected: all focused tests pass; no generated guest command contains `WebDriver:Status`; all combined commands remain at or below 768 bytes.

- [ ] **Step 6: Commit protocol v7**

```bash
git add tools/riscv/megrez_desktop.py tools/riscv/tests/test_megrez_desktop.py
git commit -m "Run Firefox NewSession without an invalid Status gate"
```

### Task 3: Update the operator contract and historical record

**Files:**
- Modify: `tools/riscv/README.md:580-610`
- Modify: `docs/superpowers/plans/2026-09-10-megrez-desktop-firefox-diagnostics.md:240-270, 680-715`

- [x] **Step 1: Replace the current README workflow**

Use this operator-facing sequence:

```text
The diagnostic records a stable Firefox identity, captures the before
snapshot, then lets the one selected WebDriver:NewSession operation perform
the loopback connection, Marionette greeting, request, and response under one
300-second absolute deadline. It does not send WebDriver:Status because that
command is not part of Firefox ESR 140's direct Marionette command table.
```

List the v7 boundaries: listener unavailable, NewSession not sent, response absent, response partial, response rejected, response complete, and evidence incomplete.  State that exact offline NewSession error output is enabled only for this bounded diagnostic.

- [x] **Step 2: Add Step 4f to the investigation plan**

Record protocol-v6 identity `1fe5e884...`, one physical boot, 391.518 seconds, zero transfers, complete 662-byte Status error response, the upstream unsupported-command finding, the 43,068-byte diagnostic-frame timeout, and fresh-U-Boot recovery.  Add a pending protocol-v7 step with the direct NewSession sequence, result schema 2, 90-second diagnostic budget, zero-transfer requirement, and one-identity rule.  Keep Steps 4c-4e as immutable historical records.

- [x] **Step 3: Check and commit documentation**

```bash
git diff --check
rg -n "WebDriver:Status|protocol v7|schema version 2|90-second" \
  tools/riscv/README.md \
  docs/superpowers/plans/2026-09-10-megrez-desktop-firefox-diagnostics.md
git add tools/riscv/README.md \
  docs/superpowers/plans/2026-09-10-megrez-desktop-firefox-diagnostics.md
git commit -m "Document the direct Firefox diagnostic protocol"
```

Expected: Status appears only in historical explanations and the explicit statement that v7 does not send it.

### Task 4: Run all host gates and perform normal review

**Files:**
- Review only; do not use the deleted `aster-code-review` skill.

- [x] **Step 1: Run formatting, lint, syntax, and diff checks**

```bash
/home/ubuntu/miniconda3/envs/mcp_robot/bin/ruff format \
  tools/riscv/megrez_desktop.py tools/riscv/tests/test_megrez_desktop.py
/home/ubuntu/miniconda3/envs/mcp_robot/bin/ruff check \
  tools/riscv/megrez_desktop.py tools/riscv/tests/test_megrez_desktop.py
python3 -m py_compile \
  tools/riscv/megrez_desktop.py tools/riscv/tests/test_megrez_desktop.py
git diff --check
```

Expected: all commands exit 0 without installing or downloading anything.

- [x] **Step 2: Run the complete related suites in the persistent container**

```bash
tools/docker/run_dev_container.sh -- make \
  test_riscv_megrez_desktop_unit \
  test_riscv_megrez_boot_stability_unit \
  test_riscv_physical_graphics_unit \
  test_riscv_megrez_probe_unit
```

Expected: all desktop, stability/physical, and probe/Stage1 tests pass with the existing persistent Cargo, Rustup, and Nix caches; no image is deleted and no dependency is downloaded.

- [x] **Step 3: Review the final diff against the approved design**

Verify directly that:

- only the diagnostic action enables the two Marionette diagnostic variables;
- normal Firefox launch and the immutable MMC/root inputs are unchanged;
- no Status command or Status phase remains in live protocol v7;
- the selected-command deadline remains 300 seconds and total budget remains 900 seconds;
- the diagnostic phase alone is 90 seconds;
- the result shape is schema 2 and the experiment identity is protocol 7;
- historical v6 replay remains `status-command-rejected`;
- all failure paths still publish and recover.

- [x] **Step 4: Commit formatting or review corrections**

If Step 1 changed formatting, commit only those verified corrections:

```bash
git add tools/riscv/megrez_desktop.py tools/riscv/tests/test_megrez_desktop.py
git commit -m "Polish Firefox protocol-v7 diagnostics"
```

If the worktree is already clean, record that no additional commit is required.

### Task 5: Run exactly one protocol-v7 physical classification

**Files:**
- Read: `target/megrez-desktop/current.json`
- Read/write evidence only: `target/megrez-desktop/evidence-77d7e42c/`
- Modify after result: `docs/superpowers/plans/2026-09-10-megrez-desktop-firefox-diagnostics.md`

- [x] **Step 1: Compute and admit the new identity without touching the board**

```bash
python3 - <<'PY'
from pathlib import Path
from tools.riscv.megrez_desktop import (
    DesktopBundle,
    FirefoxDiagnosticConfig,
    _read_plan,
    experiment_identity,
)

bundle = DesktopBundle.from_path(Path("target/megrez-desktop/current.json"))
plan = _read_plan(Path(bundle.plan_path))
config = FirefoxDiagnosticConfig(
    hypothesis=(
        "The physical Firefox Marionette greeting completes and "
        "WebDriver:NewSession is fully sent but produces no response-header "
        "byte before the fixed 300-second deadline."
    ),
    contrary_outcome=(
        "The listener does not become ready, NewSession is not fully sent, "
        "a response is partial or rejected, or NewSession returns a valid session."
    ),
)
identity = experiment_identity(bundle, plan, config)
ledger = Path(bundle.evidence_root) / "experiments.jsonl"
print(identity)
print(sum(identity in line for line in ledger.read_text().splitlines()))
print(Path(bundle.device).exists())
PY
```

Expected: a protocol-v7 identity different from `1fe5e884...`, ledger count `0`, and device state `True`.

- [x] **Step 2: Execute the identity once**

```bash
python3 -m tools.riscv.megrez_desktop diagnose-firefox \
  --bundle target/megrez-desktop/current.json \
  --hypothesis 'The physical Firefox Marionette greeting completes and WebDriver:NewSession is fully sent but produces no response-header byte before the fixed 300-second deadline.' \
  --contrary-outcome 'The listener does not become ready, NewSession is not fully sent, a response is partial or rejected, or NewSession returns a valid session.'
```

Expected invariants: one physical boot, zero QEMU runs, zero artifact-transfer bytes, stable Firefox PID/profile with zero restarts, three complete snapshots, one classified NewSession boundary, and fresh-U-Boot recovery.  Do not repeat this identity.

- [x] **Step 3: Verify immutable evidence**

Within the newly printed evidence directory, run:

```bash
sha256sum -c sha256sums.txt
stat -c '%a %n' bundle.json physical.serial.log diagnostics.log \
  snapshot-before.json snapshot-during.json snapshot-after.json result.json
```

Expected: every hash reports success and every listed file has mode `600`; the directory has mode `700`.

- [x] **Step 4: Apply the stopping rule**

- `new-session-complete`: stop diagnostics and prepare the existing physical interaction/HDMI acceptance gate; do not change the kernel.
- `new-session-rejected`: use the retained bounded Marionette error to select one source-level or Linux-reference test; do not reboot Firefox first.
- `new-session-response-absent` or `new-session-response-partial`: use the three snapshots to select one Linux/Asterinas syscall wait/wakeup microtest.
- `listener-not-ready` or `new-session-not-sent`: repair only the demonstrated command/listener boundary against retained data.
- `evidence-incomplete`: repair the observer with a retained-data RED/GREEN test; do not repeat the physical identity.

- [x] **Step 5: Record and commit the physical result**

Mark Step 4f complete in `docs/superpowers/plans/2026-09-10-megrez-desktop-firefox-diagnostics.md`.  Record the full identity, duration, classified boundary, PID/restart identity, snapshot completeness, transfer/QEMU/physical counts, evidence directory, hash verification, and recovery state.

```bash
git add docs/superpowers/plans/2026-09-10-megrez-desktop-firefox-diagnostics.md
git commit -m "Record Firefox protocol-v7 physical boundary"
git status --short
```

Expected: the evidence directory remains ignored and immutable; the tracked worktree is clean.
