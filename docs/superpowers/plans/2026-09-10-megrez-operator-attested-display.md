# Megrez Operator-Attested Display Implementation Plan

> **Execution note:** Implement this plan in the current task with the `executing-plans` workflow. Do not delegate it to subagents.

**Goal:** Add a truthful operator-attested display mode to the Megrez physical-graphics gate, while allowing one complete interaction cycle for experimental debugging and retaining three cycles as the release-grade default.

**Architecture:** Keep the existing serial protocol, trusted DOM checks, USB input path, guest screenshot, final-state proof, recovery, and artifact identity checks. Generalize the protocol to exactly one or three cycles, then add a mutually exclusive display-evidence branch: external HDMI capture or a bounded, nonce-bound operator confirmation. Publish schema-v2 evidence that cannot confuse operator attestation with an HDMI capture.

**Tech stack:** Python 3 standard library, `unittest`, Make, Ruff, pyserial, existing persistent Asterinas development container.

**Design specification:** `docs/superpowers/specs/2026-09-10-megrez-operator-attested-display-design.md`

---

## Task 1: Generalize the physical protocol to one or three cycles

**Files:**

- Modify: `tools/riscv/megrez_physical_graphics.py`
- Modify: `tools/riscv/test_megrez_physical_graphics.py`

1. Add failing unit tests for a valid one-cycle transcript, one-cycle nonce/hash validators, dynamic final-cycle command generation, and a one-cycle completion marker. Keep the existing three-cycle tests as regression coverage.
2. Run the focused unit test in the persistent container and confirm the new tests fail for the expected hard-coded-three-cycle assumptions.
3. Replace fixed three-element tuple validation with exact validation against `cycles_requested`; derive transcript length, completion marker, final marker, prompt denominator, final-state command, and completion command from that value. Accept only `1` or `3`.
4. Run the focused test again and confirm both one-cycle and three-cycle cases pass.
5. Commit as `Support one-cycle physical interaction evidence`.

## Task 2: Model mutually exclusive display evidence

**Files:**

- Modify: `tools/riscv/megrez_physical_graphics.py`
- Modify: `tools/riscv/test_megrez_physical_graphics.py`

1. Add failing tests for `DisplayEvidenceMode`, strict `OperatorDisplayEvidence`, schema-v2 result serialization, exact cycle-count enforcement, mutually exclusive `hdmi`/`operator_display`, and separate pass reasons.
2. Confirm failures in the focused persistent-container test.
3. Add `cycles_requested` and `display_mode` to the configuration; add operator evidence to the result; validate that external mode requires only HDMI evidence and operator mode requires only nonce-bound operator evidence. Preserve `physical-graphics-pass` for external capture and use `physical-graphics-operator-attested-pass` for attestation.
4. Generalize the lifecycle to run exactly the requested cycles, prove the final requested cycle, collect only the configured display evidence, emit a matching completion marker, and classify against the same cycle count.
5. Re-run focused tests and commit as `Model operator-attested physical display evidence`.

## Task 3: Implement bounded operator confirmation and publication

**Files:**

- Modify: `tools/riscv/megrez_physical_graphics.py`
- Modify: `tools/riscv/test_megrez_physical_graphics.py`
- Modify: `Makefile`
- Modify: `tools/docker/README.md`

1. Add failing tests for the mutually exclusive CLI flags, `--cycles {1,3}` with default three, exact confirmation text `confirm-cyan-pass <last-eight-nonce-characters>`, rejection of wrong/EOF/timed-out input, optional HDMI path in operator mode, and publication without a fabricated HDMI file.
2. Confirm failures in the focused persistent-container test.
3. Implement a deadline-bounded stdin reader using `select` and monotonic time, injectable in tests. Create the attestation only after the exact newline-terminated response is received.
4. Add the CLI mutually exclusive group `--hdmi-capture PATH` / `--operator-display-attestation`, add `--cycles`, and keep external HDMI behavior compatible.
5. Write canonical `operator-display-attestation.json`, include its SHA-256 in `SHA256SUMS`, and never create or label an HDMI image in operator mode.
6. Extend the Make preparation command and documentation so cycle count, display mode, and complete pre-existing MMC artifact names can be supplied without transfer or image rebuilding.
7. Re-run focused tests and commit as `Add operator-attested Megrez display mode`.

## Task 4: Verify and review the implementation

**Files:**

- Review all files changed since `d8472f9f1`.

1. Run Ruff formatting/checking and Python bytecode compilation on the physical-graphics module and test.
2. Run the physical-graphics, desktop, boot-stability, and probe unit targets in the persistent container.
3. Exercise Make dry runs for both display modes and inspect CLI help.
4. Review the diff normally for protocol provenance, timeout behavior, truthful evidence labels, backward compatibility, and preservation of unrelated user changes. Fix and re-run checks if needed.

## Task 5: Run one information-rich physical experiment

**Files:**

- Create on success: `target/current-main-physical-graphics/physical/operator-attested-evidence/`
- Modify: `docs/porting/evidence/2026-09-08-current-main-megrez-physical-graphics.md`

1. Validate that the serial device exists, the output directory is clean, and the current frozen plan is `target/current-main-physical-graphics/physical/plan-firefox-77d7e42c.json` with semantic SHA-256 `19f4279da536a6ebd0c281fd0b2d208c00acb13e41676fde2237822e928ab28c`.
2. Start the host-side gate with `--operator-display-attestation --cycles 1` and the already-present MMC files `asterinas-77d7e42c-220572e8.Image`, `asterinas-78c4a36c-f4d9b349-stage1.cpio`, and `dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb`.
3. When prompted, ask the operator to perform the single real keyboard/mouse cycle. After the guest reaches its nonce-bound cyan final state, ask the operator whether the physical HDMI display visibly shows cyan PASS; send the exact confirmation line only after the operator explicitly confirms it.
4. Verify schema version 2, `cycles_requested: 1`, one interaction cycle, nonce-bound operator evidence, no HDMI evidence/file, MMC-only transport, stable Firefox identity, recovery, file modes, and every published hash.
5. Record the command, identities, limitations, and result in the evidence document. State explicitly that one cycle proves one complete physical path but is not three-cycle repeatability evidence.
6. Re-run the relevant checks, review the final diff, and commit the evidence. Do not push remote `main` until final branch review and user-approved integration.

