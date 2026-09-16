# Human-Friendly Physical Input Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a physical operator confirm and type one four-digit decimal code without changing the 180-second real-input bound.

**Architecture:** The host generates a fresh decimal code and optionally waits for its exact host-stdin confirmation after graphical readiness. The Stage1-carried guest and HTML bind their trusted keyboard, pointer, click, screenshot, and DOM evidence to the same code. Raw evdev accepts extra well-formed clicks but never unmatched button releases.

**Tech Stack:** Python `unittest`, Stage1 Bash/CPIO, Firefox HTML/JavaScript, QEMU RISC-V gate, Megrez UART/MMC physical gate.

---

### Task 1: Four-digit code contract

**Files:**
- Modify: `tools/riscv/megrez_physical_graphics.py`
- Modify: `tools/riscv/debian/rootfs/physical_graphics_gate.py`
- Modify: `tools/riscv/debian/rootfs/physical_graphics_interaction.html`
- Modify: `tools/riscv/debian/rootfs/physical_graphics_control.sh`
- Test: `tools/riscv/tests/test_megrez_physical_graphics.py`
- Test: `tools/riscv/tests/test_physical_graphics_gate.py`
- Test: `tools/riscv/tests/test_debian_rootfs.py`

- [ ] Add focused failing tests accepting `"0427"`, rejecting `"042"`, `"04270"`, `"abcd"`, and requiring page query `nonce_length=4` and four physical key-downs.
- [ ] Run `python3 -m unittest tools.riscv.tests.test_megrez_physical_graphics tools.riscv.tests.test_physical_graphics_gate` and record the expected contract failures.
- [ ] Set host `_NONCE = re.compile(r"[0-9]{4}")`, guest `NONCE_PATTERN = re.compile(r"^[0-9]{4}$")`, page `noncePattern = /^[0-9]{4}$/`, `maxlength="4"`, `nonce_length=4`, and control-shell `is_nonce` to `^[0-9]{4}$`. Replace the host-generated code with `f"{secrets.randbelow(10000):04d}"` and resample duplicates in three-cycle runs.
- [ ] Update only protocol-bound test fixtures and assertions from 16-key evidence to four-key evidence. Keep unrelated literal `16` values (latencies, package versions, buffer bounds) unchanged.
- [ ] Run the related host, guest, Stage1, and QEMU orchestration unit-test modules; require pass.

### Task 2: Exact operator-start barrier

**Files:**
- Modify: `tools/riscv/megrez_physical_graphics.py`
- Test: `tools/riscv/tests/test_megrez_physical_graphics.py`

- [ ] Add tests for a `--operator-start` CLI flag, a valid `"0427\n"` confirmation, wrong/missing confirmations, and no prompt in automated mode.
- [ ] Run the focused tests and require failures before implementation.
- [ ] Implement one exact-line reader bounded to 180 seconds. Add an optional `operator_start: Callable[[str], None]` to `run_physical_graphics`; invoke it after `prove_graphical_readiness` and before each `run_cycle`. The production callback prints the four-digit challenge and reads host stdin. Preserve all existing recovery and publication paths on timeout/mismatch.
- [ ] Keep `cycle_timeout=180.0`; the guest READY marker, after page load/focus, remains the only start of the interaction deadline. Adapt cyan attestation to use the same four-digit code, and require an explicit human cyan confirmation before relaying it.
- [ ] Run `python3 -m unittest tools.riscv.tests.test_megrez_physical_graphics` and require pass.

### Task 3: Mouse-click evidence tolerates ordinary focusing

**Files:**
- Modify: `tools/riscv/debian/rootfs/physical_graphics_gate.py`
- Test: `tools/riscv/tests/test_physical_graphics_gate.py`

- [ ] Add a failing `EvdevCycle` test with two ordered left-down/up pairs and a passing `left_click_complete`; keep failing tests for button-up-before-down and repeated down without intervening up.
- [ ] Run the focused tests and require the two-click case to fail before implementation.
- [ ] Track the current button-down state separately from completed click pairs. Permit multiple completed raw pairs; retain the page contract of exactly one trusted amber-button click and cyan final screenshot.
- [ ] Run `python3 -m unittest tools.riscv.tests.test_physical_graphics_gate` and require pass.

### Task 4: Deterministic Stage1 and synchronized physical proof

**Files:**
- Modify: `docs/superpowers/plans/2026-09-14-stage1-physical-helper.md`
- Test output: `target/current-main-physical-graphics/physical/stage1-helper/`

- [ ] Run `bash -n` on changed shell, `git diff --check`, Ruff on changed Python, the related 351 + 59 test suites, Firefox fast check, and menu tests.
- [ ] Build Stage1 twice through the persistent Docker launcher and require byte-identical archives; verify the Stage1 helper/page bytes and executable modes.
- [ ] Run three complete QEMU interaction cycles with the pinned Release kernel, prepared DTB, partition-2 image, and rebuilt Stage1. Retain result and serial evidence.
- [ ] Stage only the changed Stage1 to MMC partition 1 through the existing menu staging path, preserving partition 2 and cached kernel/DTB.
- [ ] Start one physical cycle with `--operator-start --operator-display-attestation --cycles 1`. When the host prints its four-digit start code, ask the operator to reply with that code; relay exactly that reply to host stdin. At guest READY tell the operator to type the same code, move the pointer, and click the amber button. Require raw evdev, ordered trusted DOM, cyan page, no Firefox restart, and fresh U-Boot recovery.
- [ ] Review the diff for rootfs fallback, network-stack/DRM edits, or partition-2 writes; commit only scoped files after `superpowers:verification-before-completion`, then use `superpowers:finishing-a-development-branch` before remote integration.
