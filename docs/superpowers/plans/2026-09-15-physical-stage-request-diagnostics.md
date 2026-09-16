# Physical Stage Request Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve the first local stage-request failure in the physical Firefox UART transcript.

**Architecture:** The loopback stage server records sanitized HTTP decisions; the page records bounded fetch outcomes; the guest witness reads that status only after an error. No input synthesis, success-rule change, or extra operator step is introduced.

**Tech Stack:** Python 3 `http.server`, Firefox Marionette, self-contained HTML/JS, `unittest`, persistent Docker Stage1 build, QEMU, Megrez UART.

---

### Task 1: Stage HTTP decisions

**Files:** Modify `tools/riscv/debian/rootfs/physical_graphics_gate.py`; test `tools/riscv/tests/test_physical_graphics_gate.py`.

- [ ] Write tests that pass an `emit` collector to `DomStageServer`, issue a valid key request (`nonce=9829`), a wrong-nonce key request, and a premature pointer request, and assert sanitized `status=204`, `status=400`, and `status=409` records respectively.
- [ ] Run `PYTHONPATH=. python3 -m unittest tools.riscv.tests.test_physical_graphics_gate.DomStageServerTests -v`; verify failure is the missing diagnostics.
- [ ] Add a bounded stage-request marker emitted outside `_condition` for each decision; include only fixed stages/reasons and `nonce_match=0|1`.
- [ ] Re-run the targeted tests and confirm all pass.

### Task 2: Page-side first fetch outcome

**Files:** Modify `tools/riscv/debian/rootfs/physical_graphics_interaction.html`; test `tools/riscv/tests/test_physical_graphics_gate.py`.

- [ ] Add a page test requiring a dedicated diagnostic element and bounded `stage=key` outcome text, separate from `interaction-state`.
- [ ] Run the page test; verify it fails because the element is absent.
- [ ] Record at most three sanitized stage-fetch outcomes (`http204`, `http400`, `TypeError`, etc.) without changing the existing stage-report Promise order or trusted-input state.
- [ ] Re-run the page tests; use system Chromium Playwright with fault injection to verify the first failed key request remains visible when pointer returns HTTP 409.

### Task 3: Guest failure context

**Files:** Modify `tools/riscv/debian/rootfs/physical_graphics_gate.py`; test `tools/riscv/tests/test_physical_graphics_gate.py`.

- [ ] Add a test that makes `stages.read_title` raise `physical-graphics-stage-regression` and asserts the original exception plus one marker containing raw key/pointer counts and the page's bounded fetch status.
- [ ] Run the single test; verify it fails because no diagnostic marker is emitted.
- [ ] Permit one named read-only Marionette script after READY; on stage failure read the diagnostic element with a short timeout, sanitize its fixed vocabulary, emit once, and re-raise the original error even when the query fails.
- [ ] Re-run the targeted test and all physical-graphics guest tests.

### Task 4: Deploy and diagnose

**Files:** Rebuild `target/physical-firefox-validation/stage1-stage-diagnostics/{a,b}/initramfs.cpio`; generate a new canary menu manifest and physical debug plan.

- [ ] Run fast-check, related `unittest`, Ruff, `bash -n`, and `git diff --check`.
- [ ] Build Stage1 twice in the persistent `riscv-rootfs` Docker image; assert identical SHA-256 and expected archive modes.
- [ ] Run the three-cycle QEMU physical-graphics gate with the rebuilt Stage1; require `physical=false`, `passed=true` and three evidence cycles.
- [ ] Incrementally stage only the new Stage1 on Megrez, verify CRC and fresh U-Boot recovery.
- [ ] Run one human physical cycle without `--operator-start`; relay the code only at page READY, collect the UART transcript, and report whether the first `key` HTTP request arrived and what the page recorded. Preserve fail-closed recovery and do not attest cyan without the operator.
