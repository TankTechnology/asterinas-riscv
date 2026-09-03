# RISC-V Desktop Integration Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a locally verified integration branch for the RISC-V Debian desktop line, including the current remote `main` and the syscall/cache fixes that directly unblock Mesa and Firefox.

**Architecture:** Keep the immutable Debian browser rootfs and its fast development overlay unchanged. Integrate independent kernel fixes as separate commits, repair only confirmed baseline-test drift, and verify each affected subsystem locally before any remote merge or push. Older stacked browser/input PRs are classified against the current implementation instead of being merged wholesale.

**Tech Stack:** Git, Rust nightly, Asterinas OSTD/kernel, Python `unittest`, C regression tests, project Docker image, QEMU RISC-V SMP=4.

---

### Task 1: Synchronize the remote main regression fix

**Files:**
- Modify: `test/initramfs/src/regression/io/epoll/epoll_err.c`

- [ ] **Step 1: Verify the remote commit is the only missing `origin/main` commit**

Run: `git log --oneline HEAD..origin/main`

Expected: one commit, `bcc018e27 test(timerfd): cover consumed epoll readiness (#95)`.

- [ ] **Step 2: Cherry-pick the exact reviewed remote commit**

Run: `git cherry-pick bcc018e27acbf1e4ca7c5854871e4b649fe47068`

Expected: a clean one-commit cherry-pick.

- [ ] **Step 3: Verify the regression source is format-clean**

Run: `clang-format --dry-run --Werror test/initramfs/src/regression/io/epoll/epoll_err.c`

Expected: exit status 0.

### Task 2: Repair the stale Megrez installer test fixture

**Files:**
- Modify: `tools/riscv/tests/test_megrez_install_workflow.py:12-70`

- [ ] **Step 1: Reproduce the existing contract error**

Run: `python3 -W error::ResourceWarning -m unittest tools.riscv.tests.test_megrez_install_workflow -v`

Expected: ten errors ending in `DebugContractError: stale Megrez argument reaches init argv`.

- [ ] **Step 2: Reuse the canonical physical bootargs builder in the schema-two fixture**

Import `physical_bootargs` from `tools.riscv.megrez_gmac_gate` and change the
fixture bootargs to:

```python
physical_bootargs(600)
```

- [ ] **Step 3: Verify the focused and aggregate installer suites**

Run: `python3 -W error::ResourceWarning -m unittest tools.riscv.tests.test_megrez_install_workflow -v`

Expected: all 10 tests pass.

Run: `make test_riscv_megrez_install_unit`

Expected: all installer/session tests pass.

- [ ] **Step 4: Commit the fixture repair**

```bash
git add tools/riscv/tests/test_megrez_install_workflow.py
git commit -m "test(riscv): refresh Megrez installer bootargs"
```

### Task 3: Integrate PR #99 for Mesa GBM descriptor duplication

**Files:**
- Modify: `kernel/src/syscall/fcntl.rs`
- Create: `test/initramfs/src/regression/io/file_io/fcntl_dupfd.c`
- Modify: `test/initramfs/src/regression/io/run_test.sh`

- [ ] **Step 1: Review commit `73514169d424` against the current branch**

Run the Asterinas review pipeline in diff mode after applying the commit, with `origin/main` as the review base, and reject the integration on any confirmed P0/P1 defect.

- [ ] **Step 2: Cherry-pick the exact PR commit**

Run: `git cherry-pick 73514169d4245b75eb940a372395d8dff709e1e7`

Expected: no conflict with the desktop/rootfs files.

- [ ] **Step 3: Verify source formatting and RISC-V kernel compilation**

Run: `clang-format --dry-run --Werror test/initramfs/src/regression/io/file_io/fcntl_dupfd.c`

Expected: exit status 0.

Run inside the project container: `make kernel TARGET_ARCH=riscv64`

Expected: exit status 0.

### Task 4: Integrate PR #100 for remote instruction-cache coherence

**Files:**
- Modify: `ostd/src/arch/riscv/irq/ipi.rs`
- Modify: `ostd/src/arch/riscv/irq/mod.rs`
- Modify: `ostd/src/arch/riscv/mod.rs`
- Modify: `ostd/src/smp.rs`

- [ ] **Step 1: Cherry-pick the exact PR commit**

Run: `git cherry-pick cde206963973d5e4b8b0842f58ccae9760568199`

Expected: a clean independent commit.

- [ ] **Step 2: Verify the RISC-V OSTD ktest configuration**

Run inside the project container:

```bash
RUSTFLAGS="--cfg ktest" cargo check -p ostd --target riscv64imac-unknown-none-elf
```

Expected: exit status 0.

- [ ] **Step 3: Verify the RISC-V kernel**

Run inside the project container: `make kernel TARGET_ARCH=riscv64`

Expected: exit status 0.

### Task 5: Integrate PR #102 for architecture-gated syscall compilation

**Files:**
- Modify: `kernel/src/syscall/mod.rs`

- [ ] **Step 1: Cherry-pick the exact PR commit**

Run: `git cherry-pick 6055d69fb6824688d2df8bed2d62e04c10646d9a`

Expected: one `cfg(target_arch = "riscv64")` gate around the RISC-V-only module.

- [ ] **Step 2: Verify both relevant build directions**

Run inside the project container:

```bash
make kernel TARGET_ARCH=x86_64
make kernel TARGET_ARCH=riscv64
```

Expected: both commands exit 0.

### Task 6: Classify older open PRs without importing obsolete stacks

**Files:**
- Create: `docs/reviews/2026-09-03-riscv-desktop-pr-triage.md`

- [ ] **Step 1: Compare PR #78 to current input support**

Record that it is an 18-commit, conflict-heavy standalone VirtIO keyboard gate; retain it only as reference while the current desktop keyboard/mouse path remains passing.

- [ ] **Step 2: Compare PRs #84 and #86 to the schema-seven browser-web line**

Record which commits are patch-equivalent and which unique schema-six/browser-m5 pieces are superseded by `browser-web`; do not cherry-pick either stacked branch wholesale.

- [ ] **Step 3: Commit the triage record**

```bash
git add docs/reviews/2026-09-03-riscv-desktop-pr-triage.md
git commit -m "docs(riscv): triage desktop-related open PRs"
```

### Task 7: Verify the integrated baseline

**Files:**
- Create: `docs/reviews/2026-09-03-riscv-desktop-integration-review.md`

- [ ] **Step 1: Run the local Python gates**

```bash
make test_riscv_debian_rootfs_unit
make test_riscv_megrez_gmac_unit
make test_riscv_megrez_install_unit
```

Expected: every suite passes.

- [ ] **Step 2: Run source checks**

```bash
git diff --check origin/main...HEAD
python3 -m compileall -q tools/riscv
```

Expected: both commands exit 0.

- [ ] **Step 3: Run the Asterinas review pipeline**

Review `origin/main...HEAD` through maintainability, development, security, hardware, and documentation personas. Verify each finding before deciding whether to merge or revise.

- [ ] **Step 4: Stop before external integration if any focused check fails**

Do not merge remote PRs or push `main` until failures are either fixed with a focused regression or documented as pre-existing and demonstrably unrelated.
