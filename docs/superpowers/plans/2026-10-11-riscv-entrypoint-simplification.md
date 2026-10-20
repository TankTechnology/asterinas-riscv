# RISC-V Tool Entrypoint Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with checkpoints. Do not touch physical hardware.

**Goal:** Make the Megrez/QEMU simulation entrypoints converge on one reusable QEMU run recipe without changing boot arguments, serial behavior, receipts, or test meaning.

**Architecture:** `Makefile` will own one parameterized `run_riscv_uboot_booti` recipe. The existing test target will add its unit tests and depend on that recipe. `verify_megrez_sim.sh` will remain a compatibility command that builds its marker initramfs, passes its existing profile and artifact paths to the canonical recipe, and prints identities after the runner succeeds.

**Tech Stack:** GNU Make, Bash, Python 3, the existing `qemu_uboot_booti.py` runner, and unittest.

---

### Task 1: Add one parameterized QEMU run target

**Files:**
- Modify: `Makefile:330-333,1116-1134`
- Test: `tools/riscv/tests/test_qemu_uboot_booti.py`

- [ ] Add contract assertions for `QEMU_UBOOT_PROFILE ?= generic-sv39`, a
  `.PHONY: run_riscv_uboot_booti` target, and the dependency
  `test_riscv_uboot_booti: test_riscv_uboot_booti_unit run_riscv_uboot_booti`.
- [ ] Run the focused Makefile contract test and confirm it fails before the
  target exists.
- [ ] Extract the current prepare/run recipe into `run_riscv_uboot_booti`.
  It must pass `QEMU_UBOOT_PROFILE`, `ASTERINAS_RISCV_BOOTI`,
  `ASTERINAS_INITRAMFS`, `QEMU_UBOOT_OUT_DIR`, and `QEMU_UBOOT_BUILD_DIR` to
  `prepare_qemu_uboot_booti.sh`, then invoke `qemu_uboot_booti.py run` once.
- [ ] Make `test_riscv_uboot_booti` depend on the new target while retaining
  the existing unit-test dependency and the `generic-sv39` default.
- [ ] Run the focused contract test and `make -n run_riscv_uboot_booti` with
  a temporary kernel/initramfs path. Confirm the dry run prints one prepare
  command and one QEMU runner command.

### Task 2: Make `verify_megrez_sim.sh` a compatibility wrapper

**Files:**
- Modify: `tools/riscv/verify_megrez_sim.sh`
- Create: `tools/riscv/tests/test_verify_megrez_sim.py`

- [ ] Add static tests that require the wrapper to call
  `make --no-print-directory run_riscv_uboot_booti`, preserve the existing
  `megrez-sv48-svade-fast`, `current-megrez`, and
  `u-boot-build-megrez` defaults, and keep `set -euo pipefail`.
- [ ] Run those tests and confirm the delegation assertion fails before the
  wrapper is changed.
- [ ] Keep marker-initramfs creation and the existing environment overrides,
  then replace the duplicated prepare/run commands with one `make` invocation.
  Leave artifact hashing and the existing output directory names intact.
- [ ] Remove the wrapper's private boot/audit implementation. The canonical
  QEMU runner must own serial parsing, failure classification, and exit status;
  the wrapper may only print identities after successful completion.
- [ ] Run `bash -n tools/riscv/verify_megrez_sim.sh` and the wrapper tests.
  This phase must not start QEMU or access `/dev/serial/by-id`.

### Task 3: Run the bounded regression set

**Files:**
- Test: `tools/riscv/tests/test_verify_megrez_sim.py`
- Test: `tools/riscv/tests/test_qemu_uboot_booti.py`

- [ ] Run:

```bash
python3 -m unittest \
  tools.riscv.tests.test_verify_megrez_sim \
  tools.riscv.tests.test_qemu_uboot_booti \
  tools.riscv.tests.test_qemu_uboot_contracts \
  tools.riscv.tests.test_qemu_fallback_receipt \
  tools.riscv.tests.test_qemu_goal_audit
```

- [ ] Run `git diff --check` over only Makefile, the wrapper, and the two test
  files. Confirm no changes outside the planned files.
- [ ] Report that no physical serial, board MMIO, reboot, deletion, or cleanup
  was performed.
