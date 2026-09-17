# Firefox Daily-Use Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one bounded command that validates ordinary Firefox functions and reports startup, input, scroll, navigation, and context-switch performance without closing the running browser session.

**Architecture:** Keep schema validation in a pure contract module and browser control in a small orchestrator. Reuse the existing local fixture validators, interaction capture, composite workload, and procfs samplers; the new code owns only ordered orchestration, safe cleanup, artifact identity, and the final verdict.

**Tech Stack:** Python 3 standard library, Marionette/WebDriver protocol, `unittest`, Bash Stage1 packaging.

---

### Task 1: Define the finite result contract

**Files:**
- Create: `tools/riscv/debian/rootfs/browser_daily_use_contract.py`
- Create: `tools/riscv/tests/test_browser_daily_use_contract.py`

- [ ] **Step 1: Write failing tests for the closed schema**

  Add tests that construct a complete result with the exact functional groups
  `document`, `storage`, `execution`, `rendering-media`, `navigation`,
  `download`, and `contexts`, plus the exact performance categories `startup`,
  `input`, `scroll`, `navigation`, and `context-switch`. Verify that missing,
  extra, reordered, non-finite, negative, oversized, identity-changing, and
  cross-clock values raise `DailyUseContractError`.

- [ ] **Step 2: Run the contract test and observe the missing-module failure**

  Run:

  ```bash
  python3 -m unittest tools.riscv.tests.test_browser_daily_use_contract -v
  ```

  Expected: import failure because `browser_daily_use_contract.py` does not yet
  exist.

- [ ] **Step 3: Implement the minimal pure validator**

  Define:

  ```python
  FUNCTION_GROUPS = (
      "document", "storage", "execution", "rendering-media",
      "navigation", "download", "contexts",
  )
  PERFORMANCE_CATEGORIES = (
      "startup", "input", "scroll", "navigation", "context-switch",
  )

  class DailyUseContractError(ValueError):
      pass

  def validate_daily_use_result(value: object) -> dict[str, object]:
      ...

  def build_daily_use_result(... ) -> dict[str, object]:
      ...
  ```

  Accept only schema version 1, a 32-character lowercase-hex run ID, immutable
  Firefox/Xorg PID and start-time identities, finite bounded durations, the
  enumerated group/category states, canonical relative artifact names with
  SHA-256 and byte size, and explicit clock domains. Derive `slow_count` from
  the fixed 100 ms input/scroll, 2 s navigation, and 500 ms context-switch
  thresholds rather than accepting a caller-supplied count.

- [ ] **Step 4: Run the contract tests to green**

  Run the command from Step 2. Expected: all tests pass.

- [ ] **Step 5: Commit the contract slice**

  ```bash
  git add tools/riscv/debian/rootfs/browser_daily_use_contract.py \
    tools/riscv/tests/test_browser_daily_use_contract.py
  git commit -m "Define Firefox daily-use evidence contract"
  ```

### Task 2: Add ordered one-session orchestration

**Files:**
- Create: `tools/riscv/debian/rootfs/browser_daily_use_gate.py`
- Create: `tools/riscv/tests/test_browser_daily_use_gate.py`

- [ ] **Step 1: Write failing orchestration tests**

  Use a fake Marionette client and injected phase functions. Verify exact phase
  order, one `WebDriver:NewSession`, no `WebDriver:DeleteSession`, selection of
  the original window, bounded second-tab open/select/return/close, sampler
  initial/final handshakes, stable Firefox/Xorg identities, and one-window
  cleanup on both success and failure. Verify failures leave a private
  checkpoint but never publish `browser-daily-use-result.json`.

- [ ] **Step 2: Run the orchestration test and observe the missing-module failure**

  ```bash
  python3 -m unittest tools.riscv.tests.test_browser_daily_use_gate -v
  ```

  Expected: import failure because `browser_daily_use_gate.py` does not exist.

- [ ] **Step 3: Implement safe orchestration and publication**

  Implement `run_daily_use_gate(...)` with dependency injection for the
  fixture, local timing, composite workload, sampler, identity, and clock
  operations. The concrete sequence is:

  ```text
  validate inputs/identities -> NewSession -> select original window
  -> start samplers -> document/capability/download checks
  -> local input/scroll/navigation capture -> context-switch capture
  -> composite workload -> stop samplers -> cleanup extra windows
  -> validate identities/coverage -> atomically publish result
  ```

  Publish files with `O_EXCL|O_NOFOLLOW`, mode `0600`, an `fsync`, and atomic
  replacement. Use unique staging names containing the run ID. On error,
  publish only the bounded checkpoint, close the transport, and preserve the
  Firefox session.

- [ ] **Step 4: Run orchestration tests to green**

  Run the command from Step 2. Expected: all tests pass.

- [ ] **Step 5: Commit the orchestrator slice**

  ```bash
  git add tools/riscv/debian/rootfs/browser_daily_use_gate.py \
    tools/riscv/tests/test_browser_daily_use_gate.py
  git commit -m "Add bounded Firefox daily-use orchestrator"
  ```

### Task 3: Wire existing fixture and performance components

**Files:**
- Modify: `tools/riscv/debian/rootfs/browser_daily_use_gate.py`
- Modify: `tools/riscv/tests/test_browser_daily_use_gate.py`

- [ ] **Step 1: Add failing adapter tests**

  Verify that the default adapter reuses `_navigate`, `_wait_for_probe`,
  `_snapshot`, `_validate_fixture_capabilities`, `capture_local`, and
  `capture_composite`; converts their output into the seven function groups
  and five performance categories; records the exact download SHA-256; and
  never invokes a public URL in local mode.

- [ ] **Step 2: Observe the adapter tests fail for missing behavior**

  ```bash
  python3 -m unittest tools.riscv.tests.test_browser_daily_use_gate -v
  ```

  Expected: assertion failures for missing default adapter calls and evidence.

- [ ] **Step 3: Implement the default local adapter**

  Reuse the fixture home/search capability evidence for document, storage,
  execution, rendering/media, and navigation verdicts; reuse the existing
  strict download verifier; run `capture_local` for input, scroll, and local
  navigation; measure the controlled second-window lifecycle using guest
  monotonic time; and run the composite workload in the existing session.
  Retain negative `fetchStart` as invalid evidence instead of clamping it.

- [ ] **Step 4: Run adapter and existing component tests**

  ```bash
  python3 -m unittest \
    tools.riscv.tests.test_browser_daily_use_gate \
    tools.riscv.tests.test_browser_interaction_perf \
    tools.riscv.tests.test_browser_composite_capture \
    tools.riscv.tests.test_browser_system_time -v
  ```

  Expected: all tests pass.

- [ ] **Step 5: Commit the integration slice**

  ```bash
  git add tools/riscv/debian/rootfs/browser_daily_use_gate.py \
    tools/riscv/tests/test_browser_daily_use_gate.py
  git commit -m "Reuse Firefox performance components in daily-use gate"
  ```

### Task 4: Package the command into Stage1

**Files:**
- Modify: `tools/riscv/debian/rootfs/build_stage1.sh`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`
- Modify: `tools/riscv/firefox_fast_check.sh`

- [ ] **Step 1: Add failing packaging assertions**

  Require `browser_daily_use_contract.py` and executable
  `browser-daily-use-gate` in `--print-entries`, the generated archive, the
  deterministic entry list, and the fast-check Python compile/test lists.

- [ ] **Step 2: Observe the packaging test fail**

  ```bash
  python3 -m unittest \
    tools.riscv.tests.test_debian_rootfs.DebianStage1Tests.test_builder_declares_exact_tools_and_entries -v
  ```

  Expected: failure because the new files are not packaged.

- [ ] **Step 3: Install the two files with fixed modes**

  Add source variables and `install` calls to `build_stage1.sh`; install the
  contract as `0644` and gate as `0755`. Add both paths to timestamp
  normalization, archive construction, and exact-entry validation.

- [ ] **Step 4: Run packaging and syntax checks**

  ```bash
  python3 -m unittest tools.riscv.tests.test_debian_rootfs -v
  bash -n tools/riscv/debian/rootfs/build_stage1.sh
  ```

  Expected: all tests pass and Bash syntax exits zero.

- [ ] **Step 5: Commit the package slice**

  ```bash
  git add tools/riscv/debian/rootfs/build_stage1.sh \
    tools/riscv/tests/test_debian_rootfs.py tools/riscv/firefox_fast_check.sh
  git commit -m "Package Firefox daily-use gate in Stage1"
  ```

### Task 5: Document and qualify the host-side milestone

**Files:**
- Modify: `tools/riscv/debian/rootfs/README.md`
- Create: `docs/performance/2026-09-17-firefox-daily-use-gate.md`

- [ ] **Step 1: Document the exact command and evidence boundary**

  Document local `smoke` and physical `profile` invocations, the one terminal
  PASS/FAIL line, artifact names, five category thresholds, unsupported HDMI
  limitation, no-DeleteSession guarantee, and the fact that host qualification
  is not a speedup claim.

- [ ] **Step 2: Run the complete fast check**

  ```bash
  tools/riscv/firefox_fast_check.sh
  ```

  Expected terminal line: `FIREFOX_FAST_CHECK_PASS`.

- [ ] **Step 3: Check formatting and the final diff**

  ```bash
  git diff --check
  git status --short
  ```

  Expected: no whitespace errors; only planned files are modified.

- [ ] **Step 4: Commit the qualification record**

  ```bash
  git add tools/riscv/debian/rootfs/README.md \
    docs/performance/2026-09-17-firefox-daily-use-gate.md
  git commit -m "Qualify Firefox daily-use gate host checks"
  ```

- [ ] **Step 5: Preserve the next hardware gate**

  Record that QEMU smoke and a physical profile remain required before claiming
  the complete milestone or selecting a kernel optimization. Do not report a
  speedup until the same qualified workload has a controlled before/after A/B.
