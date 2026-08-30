# Megrez Pointer-Degraded Browser Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep one physical Megrez serial run alive through M6/M7 and automatic
recovery when the exact M4 pointer-missing outcome occurs, while publishing an
overall failing result that does not claim mouse usability.

**Architecture:** Preserve the existing strict `_MarkerTracker` and add one
schema-2-only alternative branch at the M4 marker boundary. The branch accepts
only the exact pointer diagnostic followed by the exact M4 timeout marker,
skips the M4 success milestones, continues enforcing every M6/M7 marker in
order, and turns the final terminal into a degraded failure before waiting for
fresh U-Boot recovery.

**Tech Stack:** Python 3, `unittest`, immutable Megrez debug plans, bounded
serial state machine.

---

### Task 1: Freeze the pointer-degraded serial contract

**Files:**

- Modify: `tools/riscv/tests/test_megrez_debug.py`

- [ ] **Step 1: Write the failing complete-run test**

Add a schema-2 `run_board` test whose chunks contain the existing ordered GMAC
and M5 markers, then:

```python
"DEBIAN_DESKTOP_M4_DIAGNOSTIC missing=pointer-device\n"
"DEBIAN_DESKTOP_M4_FAIL reason=desktop-timeout\n"
```

followed by the exact M6 remote/JavaScript/ready markers, all three M7 markers,
an autoboot countdown, and a fresh U-Boot prompt. Assert that every chunk is
consumed, autoboot is stopped once, and the result is:

```python
passed is False
reason == "guest-failure-recovered:browser-pass-input-missing:pointer-device"
```

- [ ] **Step 2: Add rejection subcases**

Using the same fixture, assert `guest-marker-order` for a missing pointer
diagnostic, a non-pointer M4 diagnostic, the failure marker before its
diagnostic, or M6 appearing before the complete degradation pair. Keep the
existing full M4 success test unchanged and passing.

- [ ] **Step 3: Run RED**

Run:

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_debug.MegrezDebugBoardRunnerTests -v
```

Expected: the complete-run test fails with `guest-marker-order`; all rejection
subcases and existing tests retain their current behavior.

### Task 2: Implement the exact streaming alternative

**Files:**

- Modify: `tools/riscv/megrez_debug_board.py`
- Modify: `tools/riscv/tests/test_megrez_debug.py`

- [ ] **Step 1: Add exact branch identities**

Import `DESKTOP_M4_MILESTONES` and define only these accepted physical strings:

```python
_POINTER_MISSING_DIAGNOSTIC = (
    "DEBIAN_DESKTOP_M4_DIAGNOSTIC missing=pointer-device"
)
_M4_TIMEOUT_FAILURE = "DEBIAN_DESKTOP_M4_FAIL reason=desktop-timeout"
_POINTER_MISSING_REASON = "browser-pass-input-missing:pointer-device"
```

- [ ] **Step 2: Extend `_MarkerTracker` minimally**

Give the tracker an `allow_pointer_degradation` constructor flag. When the next
expected marker is the first M4 success marker, accept the exact diagnostic,
then the exact failure marker; advance the marker index past the entire M4
milestone tuple and record `_POINTER_MISSING_REASON`. Any partial, reordered,
duplicate, or differently attributed pair raises `guest-marker-order`.

When the final M7 marker completes, construct `GuestTerminal(False,
_POINTER_MISSING_REASON)` if the degradation was recorded; otherwise retain
the existing passing terminal.

- [ ] **Step 3: Enable the branch only for the Debian browser schema**

Construct the tracker as:

```python
tracker = _MarkerTracker(
    plan.markers,
    allow_pointer_degradation=(plan.schema_version == 2),
)
```

Schema-1 TCP probe behavior must remain byte-for-byte equivalent.

- [ ] **Step 4: Run GREEN**

Run the same focused class command. Expected: all tests pass, including the
complete degraded run, full browser pass, split reads, duplicate terminal,
timeout, termination, and recovery cases.

### Task 3: Document and verify the operator boundary

**Files:**

- Modify: `tools/riscv/debian/rootfs/README.md`
- Test: `tools/riscv/tests/test_megrez_debug.py`

- [ ] **Step 1: Document result semantics**

In the Megrez physical browser section, state that exact pointer absence does
not stop M6/M7 collection, but the final result stays false with
`guest-failure-recovered:browser-pass-input-missing:pointer-device`. State that
all other M4 failures remain hard failures.

- [ ] **Step 2: Run one bounded verification set**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_debug -v
python3 -m py_compile \
  tools/riscv/megrez_debug_board.py \
  tools/riscv/tests/test_megrez_debug.py
ruff check \
  tools/riscv/megrez_debug_board.py \
  tools/riscv/tests/test_megrez_debug.py
ruff format --check \
  tools/riscv/megrez_debug_board.py \
  tools/riscv/tests/test_megrez_debug.py
git diff --check
```

Expected: all commands exit zero. Do not rebuild the rootfs, launch QEMU, or
repeat the physical boot for this host-only state-machine fix.

- [ ] **Step 3: Commit**

```bash
git add tools/riscv/megrez_debug_board.py \
  tools/riscv/tests/test_megrez_debug.py \
  tools/riscv/debian/rootfs/README.md \
  docs/superpowers/plans/2026-08-30-megrez-pointer-degraded-browser.md
git commit -m "fix(riscv): retain degraded Megrez browser evidence"
```
