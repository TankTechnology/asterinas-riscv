# Stage1-Carried Physical Firefox Helper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the physical Firefox gate execute its experimental guest helpers from the booted Stage1, so a proven partition-2 root can be reused without rewriting it.

**Architecture:** Extend the existing deterministic Stage1 `/usr/lib/asterinas` payload with the quiesce and physical-interaction helpers. Stage1 already bind-mounts that directory into the Debian root at `/run/asterinas-tools`; the host gate will invoke only those paths and will fail closed when the mounted helpers are unavailable. This also binds the one- or three-cycle host contract to a matching guest implementation instead of the older copy in partition 2.

**Tech Stack:** Bash Stage1 builder, Python orchestration and `unittest`, CPIO archive inspection, QEMU RISC-V gate, Megrez serial/U-Boot physical gate.

---

### Task 1: Specify the Stage1 archive contract

**Files:**
- Modify: `tools/riscv/tests/test_debian_rootfs.py`

- [x] Add `usr/lib/asterinas/physical-external-services-quiesce` to the exact `--print-entries` expectation.
- [x] Extend the archive-content test to require the helper path, executable mode, and bytes matching `tools/riscv/debian/rootfs/physical_external_services_quiesce.sh`.
- [x] Run the focused `DebianStage1Tests` and confirm the new assertions fail because the builder does not yet carry the helper.

### Task 2: Carry the helper in deterministic Stage1 builds

**Files:**
- Modify: `tools/riscv/debian/rootfs/build_stage1.sh`
- Test: `tools/riscv/tests/test_debian_rootfs.py`

- [x] Add a source variable for `physical_external_services_quiesce.sh`.
- [x] Install the source as `usr/lib/asterinas/physical-external-services-quiesce` with mode `0755`.
- [x] Add the path to timestamp normalization, CPIO input order, `--print-entries`, and the exact post-build archive validation string.
- [x] Re-run the focused Stage1 builder tests and require them to pass.

### Task 3: Make the physical gate use only the Stage1-bound path

**Files:**
- Modify: `tools/riscv/tests/test_megrez_physical_graphics.py`
- Modify: `tools/riscv/megrez_physical_graphics.py`

- [x] Change the unit-test contract to require exactly `/run/asterinas-tools/physical-external-services-quiesce`, reject `/usr/lib/asterinas`, and reject fallback shell syntax.
- [x] Run the focused test and confirm it fails with the current rootfs path.
- [x] Change `physical_external_services_quiesce_command()` to return the Stage1-bound path only.
- [x] Re-run the focused test and the complete physical-graphics unit-test module.

### Task 3a: Bind the interaction helper to the same Stage1 generation

**Files:**
- Modify: `tools/riscv/debian/rootfs/build_stage1.sh`
- Modify: `tools/riscv/debian/rootfs/physical_graphics_gate.py`
- Modify: `tools/riscv/megrez_physical_graphics.py`
- Test: `tools/riscv/tests/test_debian_rootfs.py`
- Test: `tools/riscv/tests/test_physical_graphics_gate.py`
- Test: `tools/riscv/tests/test_megrez_physical_graphics.py`

- [x] Reproduce that the reused partition-2 guest gate rejects the supported one-cycle final-state contract.
- [x] Carry `physical-graphics-gate` in Stage1 and require both host commands to use `/run/asterinas-tools` without a root-image fallback.
- [x] Accept only the host-supported terminal cycles 1 and 3 in the guest final-state verifier.
- [x] Prove the focused tests fail before the change and pass afterward, then run all four related modules.
- [x] Rebuild Stage1 twice and verify identical archives, executable modes, and source-bound helper hashes.

### Task 4: Prove deterministic payload identity and software regressions

**Files:**
- Build output: `target/physical-firefox-validation/stage1-helper/`

- [x] Build Stage1 twice from the same cached cross-toolchain inputs and compare whole-archive SHA-256 values.
- [x] Extract/list the archive, check the exact helper path and executable mode, and compare helper SHA-256 with the source.
- [x] Run `bash -n` on changed shell code and the Stage1, physical-graphics, and QEMU orchestration unit-test modules.
- [x] Run the existing Firefox fast check and require all checks to pass.
- [x] Review the diff for accidental network-stack, DRM, rootfs-fallback, or partition-2 changes.

### Task 5: Validate three QEMU interaction cycles

**Files:**
- Test output: `target/physical-firefox-validation/qemu-stage1-helper/`

- [x] Run `make test_riscv_physical_graphics_qemu_gate` with the rebuilt Stage1 and the pinned current-main kernel, DTB, root image, manifest, lock, and checksum inputs.
- [x] Require three successful cycles and verify the result records `physical: false`.
- [x] Retain result JSON, serial logs, and exact artifact hashes.

### Task 6: Validate the real board and finish the branch

**Files:**
- Test output: `target/physical-firefox-validation/physical-stage1-helper/`

- [ ] Boot the same kernel, rebuilt Stage1, and Megrez DTB while reusing the installed partition-2 root.
- [ ] Require graphical readiness and the physical keyboard/mouse interaction evidence; do not substitute QEMU evidence.
- [ ] Require the recovery timer to return the board to a fresh U-Boot prompt and retain serial/result/artifact identities.
- [ ] Run final targeted verification, review the complete diff against the approved design, and commit the implementation and evidence-facing documentation without touching unrelated worktree changes.
- [ ] Use `superpowers:verification-before-completion`, then `superpowers:finishing-a-development-branch` before reporting completion or integrating.
