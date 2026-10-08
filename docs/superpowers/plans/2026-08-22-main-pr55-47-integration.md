# Focused Main PR Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replay, harden, test, and admit PRs #55, #46, and #47 on current `main`, then prepare PR #53 for focused decomposition.

**Architecture:** Each small PR is a separate commit on an isolated integration branch. Tests are added next to the architecture-specific logic; the devtmpfs change retains its focused first-process boundary. The DRM rollup is analyzed only after its three prerequisite fixes are represented on `main`.

**Tech Stack:** Rust 2024, OSTD kernel tests, Asterinas component tests, Python `unittest`, QEMU RISC-V, Git/GitHub CLI.

---

### Task 1: Admit PR #55 with checked DTB range validation

**Files:**
- Modify: `ostd/src/arch/riscv/boot/mod.rs`
- Test: `ostd/src/arch/riscv/boot/mod.rs`

- [ ] **Step 1: Replay the focused source commit**

Run: `git cherry-pick e62f11d96ec633b6304cac4cc6574c5a6e3b95ca`

Expected: one clean commit touching only the RISC-V boot module.

- [ ] **Step 2: Add a failing overflow regression test**

Add a `#[cfg(ktest)]` test module whose test calls a pure range predicate with
`usize::MAX`-adjacent input and expects rejection.

- [ ] **Step 3: Verify the test fails before the helper exists**

Run: `make ktest TARGET_ARCH=riscv64 SMP=4 CARGO_OSDK_TEST_ARGS='boot --scheme riscv'`

Expected: compile failure naming the missing checked range helper.

- [ ] **Step 4: Implement checked range containment**

Use `checked_add` for the DTB region end and reject empty, reversed, and
overflowing ranges before comparing them with the kernel/initramfs range.

- [ ] **Step 5: Verify and commit**

Run the focused ktest, RISC-V OSTD check, clippy, and rustfmt. Commit the
hardening separately from the replayed source commit.

### Task 2: Admit PR #46 with deterministic ordering coverage

**Files:**
- Modify: `kernel/comps/virtio/src/transport/mmio/bus/arch/riscv.rs`
- Test: `kernel/comps/virtio/src/transport/mmio/bus/arch/riscv.rs`

- [ ] **Step 1: Replay the focused source commit**

Run: `git cherry-pick 6b30d6329e67af56e76f7f0b4b85f297569ad426`

- [ ] **Step 2: Add a failing ordering test**

Create descending sample slots and assert the tested helper returns ascending
MMIO starts while preserving each slot's end and interrupt source.

- [ ] **Step 3: Isolate and implement the ordering helper**

Represent a probed slot with one private structure and sort it with
`sort_unstable_by_key` before registration.

- [ ] **Step 4: Verify and commit**

Run the focused component ktest where supported, RISC-V kernel check, clippy,
and rustfmt. Commit the regression coverage separately.

### Task 3: Admit PR #47 and verify minimal-initramfs behavior

**Files:**
- Modify: `kernel/src/device/mod.rs`
- Optionally reuse for local verification: `tools/riscv/nixos/m8/boot_m8_devfix.py`, `tools/riscv/nixos/m8/build_m8_devfix.sh`, `tools/riscv/nixos/m8/nodev_init.c`

- [ ] **Step 1: Replay the focused source commit**

Run: `git cherry-pick 5145500899e5f1027fe4d012045e378b7ec85020`

- [ ] **Step 2: Review the unresolved-path transition**

Confirm only `LookupResult::AtParent` creates `/dev`, an existing `/dev` is
reused, creation mode is `0755`, and all errors propagate before later device
initializers run.

- [ ] **Step 3: Run compile/lint checks**

Run RISC-V kernel check, clippy, rustfmt, and `git diff --check`.

- [ ] **Step 4: Run the no-`/dev` QEMU gate when assets exist**

Build a minimal initramfs without `/dev`, boot with SMP=4, and require the
`dev-is-dir`, `console-present`, `console-open`, and `init-done` markers. If a
required local U-Boot/DTB asset is absent, record the exact missing prerequisite
instead of claiming PASS.

### Task 4: Integrate and update the remote PR state

**Files:**
- Verify: repository and GitHub refs only

- [ ] **Step 1: Run combined local verification**

Run the LTP Python suite, NixOS track audit suite, `git diff --check`, RISC-V
compile/lint checks, and all focused kernel tests produced above.

- [ ] **Step 2: Fast-forward `main` with a lease-protected push**

Confirm remote `main` still equals the integration base, fast-forward the local
main worktree, and push normally. Stop if the remote moved.

- [ ] **Step 3: Close #55, #46, and #47 with exact admitted commit links**

Each comment records the current-main commit and local verification evidence;
then delete the obsolete remote topic branch with a SHA lease.

- [ ] **Step 4: Reassess PR #53**

Recompute patch equivalence against new `main`, publish the three-batch DRM
decomposition, and leave #53 open until its 2D/KMS batch is current-main clean.
