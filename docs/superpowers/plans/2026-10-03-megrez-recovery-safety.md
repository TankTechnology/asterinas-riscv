# Megrez Recovery Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with review checkpoints.

**Goal:** Prevent an unrecovered Megrez boot from being retried blindly, require a fresh firmware epoch before any U-Boot write, and provide a tested UART/timer/MMC diagnostic boot profile.

**Architecture:** A small host-side safety module owns an atomic JSON latch and validates a fresh RockOS attestation receipt before clearing it. `megrez_board_session.py` checks the latch before opening the serial port, requires an observed OpenSBI/U-Boot/prompt epoch before loading artifacts, and marks an attempted recovery as required when the fresh epoch is absent. A RAM-only `minimal` device profile disables the two Ethernet and two USB controller nodes before `booti`.

**Tech Stack:** Python 3 standard library, `unittest`, U-Boot FDT commands, existing RockOS deployment-attestation JSON.

---

### Task 1: Add the safety latch and receipt contract

**Files:**
- Create: `tools/riscv/megrez_safety.py`
- Test: `tools/riscv/tests/test_megrez_safety.py`

- [ ] **Step 1: Write failing tests** for a missing latch, malformed latch, recovery-required latch without a receipt, stale receipt, valid schema-v2 RockOS receipt with a fresh boot ID/nonce, atomic mark-required, and mark-cleared behavior.

- [ ] **Step 2: Run the focused tests and verify they fail** because the module and contract do not exist.

  ```bash
  PYTHONPATH=.:tools/riscv python3 -m unittest tools.riscv.tests.test_megrez_safety -v
  ```

- [ ] **Step 3: Implement the minimal module.** Use `os.open(..., O_NOFOLLOW)`, bounded JSON reads, UUID-shaped `rockos_boot_id`, 32-hex `measurement_nonce`, `schema_version == 2`, `observer == "rockos-sha256sum"`, and `rockos_recovered == true`. Write state through a same-directory temporary file and `os.replace`; include `schema_version`, `status`, `reason`, `run_id`, and UTC timestamp. A receipt must be newer than the latch and contain the validated RockOS fields before clearing it.

- [ ] **Step 4: Re-run the focused tests and verify they pass.**

- [ ] **Step 5: Run `git diff --check` for the new module and tests.**

### Task 2: Gate the board session on fresh firmware and recovery state

**Files:**
- Modify: `tools/riscv/megrez_board_session.py`
- Modify: `tools/riscv/tests/test_megrez_board_session.py`

- [ ] **Step 1: Write failing tests** for `validate_initial_uboot_epoch` accepting `OpenSBI v...` followed by `U-Boot ...` and `=> `, rejecting an old `=> ` without a firmware banner, and for `main` refusing to call `boot_loaded_artifacts` when that validation fails.

- [ ] **Step 2: Run those tests and verify the old implementation proceeds from the stale prompt.**

- [ ] **Step 3: Add CLI options** `--safety-state` (default stable workspace path `target/megrez-board-runs/.recovery-safety.json`) and `--recovery-receipt`; check the latch before constructing `BoardSession`, validate the initial firmware epoch before any load, and never send an empty line. Track `BoardSession.boot_attempt_started` and `recovery_observed` in `main`; on a required-recovery run that reaches the boot transaction but fails to observe a fresh epoch, atomically mark the latch and return the existing nonzero result. A valid newer RockOS receipt clears the latch before the new session begins.

- [ ] **Step 4: Add tests** proving a recovery-required latch blocks serial opening, a valid newer receipt permits it, a missing recovery marks the latch, and a successful fresh epoch does not leave a latch.

- [ ] **Step 5: Run the complete board-session suite.**

  ```bash
  PYTHONPATH=.:tools/riscv python3 tools/riscv/tests/test_megrez_board_session.py
  ```

### Task 3: Add the RAM-only minimal diagnostic device profile

**Files:**
- Modify: `tools/riscv/megrez_board_session.py`
- Modify: `tools/riscv/tests/test_megrez_board_session.py`
- Modify: `tools/riscv/README.md`

- [ ] **Step 1: Write failing tests** for `--device-profile minimal`, asserting the generated command sequence disables `/soc/ethernet@50400000`, `/soc/ethernet@50410000`, `/soc/usb0@50480000/dwc3@50480000`, and `/soc/usb1@50490000/dwc3@50490000`, and does not add the `/chosen asterinas,usb-host` property. The `full` profile must retain the current behavior.

- [ ] **Step 2: Run the focused tests and verify they fail** because the parser and command selection do not exist.

- [ ] **Step 3: Implement the profile as volatile FDT edits** performed after `fdt resize` and before `booti`; do not save the environment or alter MMC. Keep `full` as the default for compatibility.

- [ ] **Step 4: Run the focused tests and the full board-session suite.**

- [ ] **Step 5: Document the safe first physical command** using `--final-profile basic-console --device-profile minimal --require-recovery` and state that this profile proves only UART/timer/MMC recovery, not networking or desktop.

### Task 4: Verify, publish evidence, and stop before hardware use

**Files:**
- Modify: `docs/porting/evidence/2026-10-03-megrez-basic-console-hard-hang.md`
- Modify: `docs/porting/evidence/megrez-history-index.md`

- [ ] **Step 1: Run focused safety, board-session, and parser tests together.**

- [ ] **Step 2: Run `python3 -m py_compile` on the changed Python files and `git diff --check`.**

- [ ] **Step 3: Record the new latch/receipt behavior and the exact no-hardware verification boundary in the incident page.**

- [ ] **Step 4: Do not launch a physical run in this change.** The first hardware use requires a separate operator reset, a fresh RockOS attestation receipt, and review of the generated command transcript.
