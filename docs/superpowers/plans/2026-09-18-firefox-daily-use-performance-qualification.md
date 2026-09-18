# Firefox Daily-Use Performance Qualification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the bounded Firefox daily-use performance profile qualify supported workloads when optional execution/media capabilities are honestly unsupported, without weakening the standalone browser functionality gate, then collect a reproducible three-run Megrez baseline suitable for selecting one kernel optimization.

**Architecture:** Put the required-versus-optional qualification rule in the pure daily-use contract and reuse it on the physical host. Add a daily-use-only fixture classifier that validates the exact document and terminal capability schema, maps owned checks to function groups, and carries the bounded limitation into the result. Keep the existing standalone web validator unchanged. Preserve the new states through failure upload, physical admission, and three-run reporting before rebuilding one immutable Stage1 and running QEMU and physical qualification.

**Tech Stack:** Python 3 standard library, `unittest`, Marionette/WebDriver protocol, Bash Stage1 packaging, Docker-based RISC-V builds, QEMU, Megrez serial/MMC boot workflow.

---

### Task 1: Define one shared functional qualification rule

**Files:**
- Modify: `tools/riscv/debian/rootfs/browser_daily_use_contract.py`
- Modify: `tools/riscv/tests/test_browser_daily_use_contract.py`

- [ ] **Step 1: Write failing contract tests for required and optional groups**

  Import the new public constants and helper:

  ```python
  REQUIRED_FUNCTION_GROUPS = (
      "document", "storage", "navigation", "download", "contexts",
  )
  OPTIONAL_FUNCTION_GROUPS = ("execution", "rendering-media")

  def function_groups_qualify(value: object) -> bool:
      ...
  ```

  Add tests proving:

  - all seven `pass` states qualify;
  - either or both optional groups may be `unsupported` with reason
    `fixture-capability-unavailable`;
  - `fail` in any group does not qualify;
  - `unsupported` in a required group does not qualify;
  - missing, extra, reordered, or malformed groups still raise
    `DailyUseContractError` instead of returning a permissive answer.

- [ ] **Step 2: Write failing tests for the bidirectional limitation rule**

  Extend `complete_result()` variants and assert:

  ```text
  optional unsupported + fixture-capabilities-incomplete => state pass
  optional unsupported + limitation absent               => contract error
  all optional pass + limitation present                  => contract error
  required fail + otherwise consistent limitation         => state fail
  ```

  Also verify that `build_daily_use_result()` derives the same top-level
  state as `validate_daily_use_result()`.

- [ ] **Step 3: Run the focused tests and observe red**

  ```bash
  python3 -m unittest \
    tools.riscv.tests.test_browser_daily_use_contract -v
  ```

  Expected: import/assertion failures for the missing constants, helper, and
  limitation semantics.

- [ ] **Step 4: Implement the minimal closed-schema rule**

  In `browser_daily_use_contract.py`:

  - add `fixture-capabilities-incomplete` to `_LIMITATION_REASONS`;
  - normalize all seven groups before qualification;
  - make `function_groups_qualify()` require every required group to be
    `pass` and every optional group to be `pass` or `unsupported`;
  - derive top-level state from that helper;
  - require the new limitation if and only if at least one optional group is
    `unsupported`;
  - keep all existing group ordering, reason, identity, metric, artifact, and
    bound checks unchanged.

  The helper must validate a caller-supplied value through
  `_normalize_function_groups()` so the guest and host cannot disagree on a
  malformed list.

- [ ] **Step 5: Run the contract tests to green**

  ```bash
  python3 -m unittest \
    tools.riscv.tests.test_browser_daily_use_contract -v
  ```

  Expected: all tests pass.

- [ ] **Step 6: Commit the contract slice**

  ```bash
  git add tools/riscv/debian/rootfs/browser_daily_use_contract.py \
    tools/riscv/tests/test_browser_daily_use_contract.py
  git commit -m "Qualify supported Firefox daily-use functions"
  ```

### Task 2: Classify terminal fixture capabilities without weakening the web gate

**Files:**
- Modify: `tools/riscv/debian/rootfs/browser_daily_use_gate.py`
- Modify: `tools/riscv/tests/test_browser_daily_use_gate.py`
- Test: `tools/riscv/tests/test_debian_browser_web.py`

- [ ] **Step 1: Replace the all-capabilities adapter test with failing ownership tests**

  Split
  `test_fixture_rejects_each_missing_capability_and_foreign_resource` into
  explicit cases:

  - `localStorage`, `sessionStorage`, `cookie`, or `indexedDb` false produces
    `storage=fail` with `fixture-capability-failed`;
  - `wasm`, `worker`, or `fetch` false produces
    `execution=unsupported` with `fixture-capability-unavailable`;
  - `canvas` or `audio` false produces
    `rendering-media=unsupported` with the same reason;
  - optional unsupported states carry exactly
    `fixture-capabilities-incomplete`;
  - all checks true preserve the five fixture-owned pass groups and add no
    fixture limitation;
  - a foreign resource remains fatal.

  Extend fixture fakes for `state=error`, a non-empty bounded `error`, missing
  and extra check keys, non-boolean check values, `state=running`, and
  inconsistent `complete`/`error` combinations.

- [ ] **Step 2: Add failing orchestration tests for limitation merging**

  Extend `FixtureCapture` with:

  ```python
  limitations: dict[str, object]
  ```

  Update fake captures and assert that `run_daily_use_gate()` merges fixture
  and composite limitation items, rejects malformed/duplicate inputs through
  the contract, and publishes a passing result when only the two optional
  groups are unsupported.

  Verify that a required storage failure reaches a validated checkpoint but
  cannot publish `browser-daily-use-result.json` as a pass.

- [ ] **Step 3: Run the focused gate tests and observe red**

  ```bash
  python3 -m unittest \
    tools.riscv.tests.test_browser_daily_use_gate -v
  ```

  Expected: failures because the adapter still calls the strict
  `probe_fixture_home()` validator and `FixtureCapture` has no limitation
  field.

- [ ] **Step 4: Implement a daily-use-only fixture classifier**

  Add a private helper in `browser_daily_use_gate.py` that:

  1. uses `browser_web_marionette_gate._probe_mapping()` for the exact probe
     shape and DOM-field shape;
  2. independently requires the exact URL, title, `readyState=complete`,
     `jsComplete=true`, Latin/CJK body text, and all three fixture DOM flags;
  3. requires the exact capability fields and the exact nine check names;
  4. accepts only terminal `complete` or `error` reports with boolean checks;
  5. requires `complete` to mean all checks true with `error=None`, and
     `error` to mean at least one false check with a non-empty bounded string;
  6. maps storage, execution, and rendering/media checks to their owned group
     verdicts without changing the strict web validator.

  Use this helper as the `_wait_for_probe()` predicate so a `running` report
  remains bounded retry evidence. Run it again on the full snapshot and
  require the probe and snapshot group verdicts to agree before validating
  local resources and the controlled download.

- [ ] **Step 5: Merge fixture limitations into the final result**

  Combine `fixture.limitations["items"]` and
  `composite.limitations["items"]` into one sorted, unique list immediately
  before `build_daily_use_result()`. Do not relax any timeout, sampler,
  cleanup, identity, download, navigation, or metric threshold.

- [ ] **Step 6: Prove the standalone web gate remains strict**

  ```bash
  python3 -m unittest \
    tools.riscv.tests.test_browser_daily_use_gate \
    tools.riscv.tests.test_debian_browser_web -v
  ```

  Expected: daily-use optional failures become unsupported, while the existing
  `_validate_fixture_capabilities()` tests still reject every false check.

- [ ] **Step 7: Commit the fixture slice**

  ```bash
  git add tools/riscv/debian/rootfs/browser_daily_use_gate.py \
    tools/riscv/tests/test_browser_daily_use_gate.py
  git commit -m "Report optional Firefox fixture capabilities"
  ```

### Task 3: Preserve unsupported states in failure evidence

**Files:**
- Modify: `tools/riscv/debian/rootfs/browser_daily_use_upload.py`
- Modify: `tools/riscv/tests/test_browser_daily_use_upload.py`

- [ ] **Step 1: Write failing checkpoint-upload tests**

  Build a failure checkpoint containing ordered fixture groups where
  `execution` and `rendering-media` are `unsupported` with the canonical
  reason. Assert that `build_bundle(..., outcome="fail")` and `parse_bundle()`
  preserve those entries.

  Add negative cases for unsupported required groups with an invalid reason,
  passing groups with a reason, unsupported groups without a reason, and
  unknown states/reasons.

- [ ] **Step 2: Run the upload tests and observe red**

  ```bash
  python3 -m unittest \
    tools.riscv.tests.test_browser_daily_use_upload -v
  ```

  Expected: the current checkpoint validator rejects `unsupported`.

- [ ] **Step 3: Reuse the closed group validator for checkpoints**

  Import the existing `_normalize_function_groups()` helper in both the
  installed and repository import branches. Preserve the rule that checkpoint
  groups are a unique ordered subset of `FUNCTION_GROUPS`, fill absent names
  with temporary passing entries, normalize the complete list, and then retain
  only the supplied entries. Do not allow unknown reasons or a reason on a
  passing group.

- [ ] **Step 4: Run the upload tests to green**

  ```bash
  python3 -m unittest \
    tools.riscv.tests.test_browser_daily_use_upload -v
  ```

  Expected: all tests pass.

- [ ] **Step 5: Commit the failure-evidence slice**

  ```bash
  git add tools/riscv/debian/rootfs/browser_daily_use_upload.py \
    tools/riscv/tests/test_browser_daily_use_upload.py
  git commit -m "Preserve unsupported daily-use checkpoints"
  ```

### Task 4: Make physical admission use the guest's qualification rule

**Files:**
- Modify: `tools/riscv/megrez_firefox_daily_use.py`
- Modify: `tools/riscv/tests/test_megrez_firefox_daily_use.py`

- [ ] **Step 1: Write failing host-admission tests**

  Add bundle variants proving that the physical runner:

  - accepts optional unsupported groups only with the required limitation;
  - rejects a failed required group;
  - rejects a failed optional group;
  - rejects missing or stale `fixture-capabilities-incomplete` evidence;
  - continues rejecting identity drift, incomplete attribution, sampler gaps,
    gate-run mismatch, failed upload, or failed recovery.

- [ ] **Step 2: Run the physical-runner tests and observe red**

  ```bash
  python3 -m unittest \
    tools.riscv.tests.test_megrez_firefox_daily_use -v
  ```

  Expected: the `function-groups-pass` predicate is false because it currently
  requires all seven states to equal `pass`.

- [ ] **Step 3: Replace the duplicated host rule**

  Import `function_groups_qualify()` beside `validate_daily_use_result()` and
  compute the existing `function-groups-pass` predicate with that helper.
  Keep the predicate key for run-result compatibility; its meaning becomes
  “all required groups pass and optional groups are pass/unsupported under the
  validated contract.”

- [ ] **Step 4: Run the physical-runner tests to green**

  ```bash
  python3 -m unittest \
    tools.riscv.tests.test_megrez_firefox_daily_use -v
  ```

  Expected: all tests pass.

- [ ] **Step 5: Commit the physical-admission slice**

  ```bash
  git add tools/riscv/megrez_firefox_daily_use.py \
    tools/riscv/tests/test_megrez_firefox_daily_use.py
  git commit -m "Align Megrez daily-use qualification"
  ```

### Task 5: Retain capability limitations in the three-run report

**Files:**
- Modify: `tools/riscv/megrez_firefox_daily_use_report.py`
- Modify: `tools/riscv/tests/test_megrez_firefox_daily_use_report.py`

- [ ] **Step 1: Write failing report-retention tests**

  Let `make_run()` create a valid result with both optional groups unsupported
  and `fixture-capabilities-incomplete`. Assert that each `report["runs"]`
  entry retains detached, verbatim `functionGroups` and `dailyUseLimitations`
  fields and that Markdown lists the unsupported groups and limitation.

  Add a mixed-capability three-run case and require report admission to reject
  it as mixed immutable workload coverage. This prevents one baseline from
  silently comparing a different browser capability surface.

- [ ] **Step 2: Run the report tests and observe red**

  ```bash
  python3 -m unittest \
    tools.riscv.tests.test_megrez_firefox_daily_use_report -v
  ```

  Expected: retention assertions fail because `_load_run()` currently drops
  function groups and daily-use limitations.

- [ ] **Step 3: Preserve and admit one exact capability surface**

  Add the normalized `daily_result["functionGroups"]` and
  `daily_result["limitations"]` to each loaded run. Require all three runs to
  have identical values before metric aggregation. Render the common coverage
  and limitation in Markdown, while retaining the report's existing
  interpretation limitations separately.

- [ ] **Step 4: Run the report tests to green**

  ```bash
  python3 -m unittest \
    tools.riscv.tests.test_megrez_firefox_daily_use_report -v
  ```

  Expected: all tests pass.

- [ ] **Step 5: Commit the reporting slice**

  ```bash
  git add tools/riscv/megrez_firefox_daily_use_report.py \
    tools/riscv/tests/test_megrez_firefox_daily_use_report.py
  git commit -m "Retain Firefox capability coverage in reports"
  ```

### Task 6: Document the qualified meaning and run host verification

**Files:**
- Modify: `tools/riscv/debian/rootfs/README.md`
- Modify: `docs/performance/2026-09-17-firefox-daily-use-gate.md`

- [ ] **Step 1: Update operator-facing semantics**

  Document the five required groups, the two optional groups, the exact
  limitation, and the distinction between a passing performance profile and
  the unchanged strict browser functionality gate. State explicitly that
  `execution=unsupported` does not claim WebAssembly, workers, or `fetch`, and
  `rendering-media=unsupported` does not claim audio or canvas coverage beyond
  the individual recorded checks.

- [ ] **Step 2: Run all daily-use unit targets**

  ```bash
  tools/docker/run_dev_container.sh --workspace "$PWD" -- \
    make test_riscv_firefox_daily_use_physical_unit
  ```

  Expected: all contract, gate, upload, fixture, physical runner, and report
  tests pass in the pinned development container.

- [ ] **Step 3: Run the complete Firefox host check**

  ```bash
  tools/docker/run_dev_container.sh --workspace "$PWD" -- \
    tools/riscv/firefox_fast_check.sh
  ```

  Expected terminal line: `FIREFOX_FAST_CHECK_PASS`.

- [ ] **Step 4: Review the implementation against the approved design**

  Check every design bullet against code and tests, search for duplicated
  all-pass assumptions, and verify no metric or timeout threshold changed:

  ```bash
  rg -n 'all\(.*state.*pass|function-groups-pass|fixture-capabilities-incomplete|unsupported' \
    tools/riscv/debian/rootfs tools/riscv/megrez_firefox_daily_use.py \
    tools/riscv/megrez_firefox_daily_use_report.py tools/riscv/tests
  git diff --check
  git status --short
  ```

- [ ] **Step 5: Commit documentation and any review corrections**

  ```bash
  git add tools/riscv/debian/rootfs/README.md \
    docs/performance/2026-09-17-firefox-daily-use-gate.md
  git commit -m "Document Firefox performance qualification"
  ```

### Task 7: Rebuild immutable inputs and exercise QEMU

**Files:**
- Verify: `tools/riscv/debian/rootfs/build_stage1.sh`
- Verify: `target/osdk/aster-kernel/aster-kernel-osdk-bin.Image`
- Create: `target/firefox-daily-use-physical/stage1-qualification-a/initramfs.cpio`
- Create: `target/firefox-daily-use-physical/stage1-qualification-b/initramfs.cpio`
- Create: `target/firefox-daily-use-physical/qemu-qualification/`

- [ ] **Step 1: Build the current Sv39/SMP=4 kernel in the persistent container**

  ```bash
  tools/docker/run_dev_container.sh --workspace "$PWD" -- \
    make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
  ```

  Expected: the kernel target succeeds and produces
  `target/osdk/aster-kernel/aster-kernel-osdk-bin.Image`.

- [ ] **Step 2: Build Stage1 twice and prove determinism**

  ```bash
  tools/docker/run_dev_container.sh --workspace "$PWD" -- \
    tools/riscv/debian/rootfs/build_stage1.sh \
      target/firefox-daily-use-physical/stage1-qualification-a/initramfs.cpio
  tools/docker/run_dev_container.sh --workspace "$PWD" -- \
    tools/riscv/debian/rootfs/build_stage1.sh \
      target/firefox-daily-use-physical/stage1-qualification-b/initramfs.cpio
  sha256sum \
    target/firefox-daily-use-physical/stage1-qualification-a/initramfs.cpio \
    target/firefox-daily-use-physical/stage1-qualification-b/initramfs.cpio
  cmp \
    target/firefox-daily-use-physical/stage1-qualification-a/initramfs.cpio \
    target/firefox-daily-use-physical/stage1-qualification-b/initramfs.cpio
  ```

  Expected: equal SHA-256 values and `cmp` exits zero.

- [ ] **Step 3: Run the applicable four-hart QEMU graphics/control gate**

  ```bash
  tools/docker/run_dev_container.sh --workspace "$PWD" -- \
    make test_riscv_physical_graphics_qemu_gate \
      DEBIAN_KERNEL="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
      DEBIAN_UBOOT="$PWD/target/firefox-daily-use-physical/qemu-inputs/u-boot" \
      DEBIAN_DTB="$PWD/target/firefox-daily-use-physical/qemu-inputs/qemu-virt-smp4.dtb" \
      DEBIAN_STAGE1_INITRAMFS="$PWD/target/firefox-daily-use-physical/stage1-qualification-a/initramfs.cpio" \
      DEBIAN_ROOT_IMAGE="$PWD/target/firefox-daily-use-physical/qemu-inputs/debian-root.ext2" \
      DEBIAN_ROOT_MANIFEST="$PWD/target/firefox-daily-use-physical/qemu-inputs/rootfs-manifest.json" \
      DEBIAN_PACKAGES_LOCK="$PWD/target/firefox-daily-use-physical/qemu-inputs/packages.lock" \
      DEBIAN_PACKAGE_CHECKSUMS="$PWD/target/firefox-daily-use-physical/qemu-inputs/package-checksums" \
      RISCV_PHYSICAL_GRAPHICS_QEMU_GATE_OUTPUT="$PWD/target/firefox-daily-use-physical/qemu-qualification"
  ```

  Expected: the rebuilt Stage1, Firefox/Marionette readiness, keyboard/tablet
  interaction, capture, and bounded cleanup path pass in QEMU. The daily-use
  classifier itself is covered by Tasks 1--6; this QEMU result is surrounding
  compatibility evidence, not a physical performance baseline.

- [ ] **Step 4: Record immutable input hashes**

  ```bash
  sha256sum \
    target/osdk/aster-kernel/aster-kernel-osdk-bin.Image \
    target/firefox-daily-use-physical/stage1-qualification-a/initramfs.cpio \
    target/firefox-daily-use-physical/inputs/eic7700-milkv-megrez-prepared.dtb \
    target/debian-riscv/browser-web/rootfs/rootfs-manifest.json \
    target/debian-riscv/browser-web/rootfs/packages.lock
  ```

  Store the output beside the physical run notes; do not edit generated target
  artifacts into Git.

### Task 8: Collect the same-board current-main physical baseline

**Files:**
- Create: `target/firefox-daily-use-physical/plan-qualification.json`
- Create: `target/firefox-daily-use-physical/baseline-q1/`
- Create: `target/firefox-daily-use-physical/baseline-q2/`
- Create: `target/firefox-daily-use-physical/baseline-q3/`
- Create: `target/firefox-daily-use-physical/baseline-report.json`
- Create: `target/firefox-daily-use-physical/baseline-report.md`

- [ ] **Step 1: Prepare a plan bound to the rebuilt inputs**

  ```bash
  make prepare_riscv_megrez_firefox_daily_use \
    MEGREZ_FIREFOX_DAILY_USE_PLAN="$PWD/target/firefox-daily-use-physical/plan-qualification.json" \
    MEGREZ_FIREFOX_DAILY_USE_DEVICE=/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0 \
    MEGREZ_FIREFOX_DAILY_USE_OUTPUT="$PWD/target/firefox-daily-use-physical/baseline-q1" \
    MEGREZ_FIREFOX_DAILY_USE_MMC_KERNEL="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
    MEGREZ_FIREFOX_DAILY_USE_MMC_INITRAMFS="$PWD/target/firefox-daily-use-physical/stage1-qualification-a/initramfs.cpio" \
    MEGREZ_FIREFOX_DAILY_USE_MMC_DTB="$PWD/target/firefox-daily-use-physical/inputs/eic7700-milkv-megrez-prepared.dtb"
  ```

  Expected: a schema-valid plan whose embedded SHA-256 identities match Step
  7.4. Do not reuse `plan-v3.json`, because it names an older Stage1.

- [ ] **Step 2: Run three fresh one-profile-per-boot samples**

  For `q1`, `q2`, and `q3`, use a fresh output directory and run:

  ```bash
  PYTHONPATH="$PWD" python3 -m tools.riscv.megrez_firefox_daily_use \
    /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0 \
    --plan "$PWD/target/firefox-daily-use-physical/plan-qualification.json" \
    --output-directory "$PWD/target/firefox-daily-use-physical/baseline-qN" \
    --fixture-bind 10.100.19.216 --fixture-port 17894 \
    --board-peer 10.100.19.200 \
    --mmc-kernel "$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
    --mmc-initramfs "$PWD/target/firefox-daily-use-physical/stage1-qualification-a/initramfs.cpio" \
    --mmc-dtb "$PWD/target/firefox-daily-use-physical/inputs/eic7700-milkv-megrez-prepared.dtb" \
    --profile-timeout 120 --open-timeout 60 --artifact-timeout 300 \
    --boot-timeout 300 --upload-timeout 30 --recovery-timeout 930
  ```

  Replace only `N` with `1`, `2`, and `3`. Between runs, verify the previous
  `run-result.json` says `qualified=true` and `recovered=true`, and verify the
  board is back at the U-Boot prompt. Do not substitute the historical D1--D5
  diagnostics for baseline samples.

- [ ] **Step 3: Verify each run's evidence boundary**

  ```bash
  for run in baseline-q1 baseline-q2 baseline-q3; do
    python3 -m json.tool \
      "target/firefox-daily-use-physical/$run/run-result.json" >/dev/null
    sha256sum -c \
      <(python3 -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p+"/sha256-manifest.json")); [print(x["sha256"]+"  "+p+"/"+x["name"]) for x in d["files"]]' \
        "target/firefox-daily-use-physical/$run")
  done
  ```

  Expected: every digest passes; every daily-use result has required groups
  `pass`, optional groups matching the same capability surface, complete
  sampler coverage, stable identities, successful upload, and automatic
  recovery.

- [ ] **Step 4: Build the conservative three-run report**

  ```bash
  PYTHONPATH="$PWD" python3 -m tools.riscv.megrez_firefox_daily_use_report \
    target/firefox-daily-use-physical/baseline-q1 \
    target/firefox-daily-use-physical/baseline-q2 \
    target/firefox-daily-use-physical/baseline-q3 \
    --json-output target/firefox-daily-use-physical/baseline-report.json \
    --markdown-output target/firefox-daily-use-physical/baseline-report.md
  ```

  Expected: exactly three immutable, qualified runs are admitted, with raw
  input/scroll/navigation/context values, CPU/runtime/runqueue attribution,
  identical function-group coverage, and explicit limitations.

- [ ] **Step 5: Select the next single-variable optimization from evidence**

  Use the report's three-run mechanism classification:

  - `runnable-delayed`: inspect scheduler/runqueue paths and formulate one
    scheduler-side change;
  - `executing`: inspect the hottest Firefox/Xorg kernel interaction and
    formulate one CPU-path change;
  - `sleeping-blocking`: inspect the repeated blocking/wakeup path and
    formulate one wait-path change;
  - `mixed`: collect more evidence and do not modify kernel behavior yet.

  Write a separate design and implementation plan for the specific candidate,
  preserve this baseline unchanged, then compare at least three before and
  three after runs with only that variable changed. No speedup claim is valid
  until that A/B exists.

### Task 9: Final verification and handoff

**Files:**
- Verify: all files changed by Tasks 1--6
- Verify: all evidence created by Tasks 7--8

- [ ] **Step 1: Run the complete verification suite again**

  ```bash
  tools/docker/run_dev_container.sh --workspace "$PWD" -- \
    make test_riscv_firefox_daily_use_physical_unit
  tools/docker/run_dev_container.sh --workspace "$PWD" -- \
    tools/riscv/firefox_fast_check.sh
  git diff --check
  ```

  Expected: both test commands pass and `git diff --check` is silent.

- [ ] **Step 2: Inspect scope and commits**

  ```bash
  git status --short
  git log --oneline --decorate -12
  git diff 91bd4d968..HEAD --stat
  ```

  Expected: source changes are limited to the contract, daily-use adapter,
  upload, physical admission/reporting, tests, and documentation. Generated
  QEMU/physical evidence remains untracked under `target/`.

- [ ] **Step 3: Report evidence, not conclusions beyond it**

  Hand off:

  - exact test and build commands with terminal status;
  - immutable kernel/Stage1/DTB/root/package hashes;
  - three run-directory manifest hashes;
  - capability states and limitation;
  - per-run and median primary metrics;
  - attribution classification and its stated limitations;
  - recovery status for every run;
  - the evidence-driven next optimization candidate, or the reason the data is
    still mixed.

  Do not describe unsupported Firefox APIs as working, QEMU as physical
  performance evidence, or a mechanism class as proof of a specific kernel
  function.
