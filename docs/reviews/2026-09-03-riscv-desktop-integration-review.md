---
date: 2026-09-03
mode: diff
base: 320a9ae53
head: cbacce08a
branch: codex/riscv-desktop-integration
title: "RISC-V desktop integration baseline"
---

# Summary

The review found one merge-blocking correctness issue in PR #100:
an SBI RFENCE error is logged but cannot reach either kernel call site or the
`riscv_flush_icache` syscall result.
The PR is therefore excluded from the final integration branch pending a
synchronous fallback or another complete failure contract.

Two major comments identify errors in the first version of the implementation
plan: review order did not isolate PR #99, and `cargo check` did not execute the
new hart-mask ktests.
The plan was corrected after this review.
The remaining findings are non-blocking commit-message, qualification,
constant-naming, issue-reference, and semantic-line-break observations.
No additional security or hardware-contract finding survived verification.

## Maintainability

### `commit 2009076a9 message`

> ```diff
> [commit message]
> test(riscv): refresh Megrez installer bootargs
> ```

`imperative-subject` (nit): The subject `test(riscv): refresh Megrez installer bootargs` begins with a scope prefix instead of the required imperative verb.

**Fix.** Use a verb-first subject such as `Refresh Megrez installer boot arguments`.

### `commit 886ae13be message`

> ```diff
> [commit message]
> test(timerfd): cover consumed epoll readiness (#95)
> ```

`imperative-subject` (nit): The subject `test(timerfd): cover consumed epoll readiness (#95)` is imperative only after a conventional-commit prefix, rather than beginning with the action verb.

**Fix.** Use a verb-first subject such as `Cover consumed timerfd epoll readiness (#95)`.

### `commit cbbdb4d73 message`

> ```diff
> [commit message]
> docs(riscv): plan desktop integration baseline
> ```

`imperative-subject` (nit): The subject `docs(riscv): plan desktop integration baseline` begins with a conventional-commit scope instead of the required verb-first imperative form.

**Fix.** Rewrite the subject in verb-first form, for example `Plan RISC-V desktop integration baseline`.

### `ostd/src/arch/riscv/irq/ipi.rs` line 122

> ```diff
> mod tests {
>     use super::{HwCpuId, first_word_hart_mask, single_hart_mask};
>     use crate::prelude::ktest;
> ```

`qualified-fn-imports` (nit): `first_word_hart_mask` and `single_hart_mask` are free functions imported directly from `super` and subsequently called without module qualification.

**Fix.** Import only the `HwCpuId` type and invoke the helpers as `super::first_word_hart_mask(...)` and `super::single_hart_mask(...)`.

### `ostd/src/arch/riscv/irq/ipi.rs` line 127

> ```diff
> let hart_mask = first_word_hart_mask(&[HwCpuId(0), HwCpuId(2), HwCpuId(63)]).unwrap();
> assert!(first_word_hart_mask(&[HwCpuId(0), HwCpuId(64)]).is_none());
> assert_eq!(single_hart_mask(HwCpuId(130)).into_inner(), (1 << 2, 128));
> ```

`no-magic-number` (nit): The values `63`, `64`, `130`, `2`, and `128` encode boundaries and offsets derived from `XLEN`, but the relationship must be reconstructed manually from the test.

**Fix.** Derive the test inputs and expected base from `XLEN`, using expressions such as `XLEN - 1`, `XLEN`, `2 * XLEN + 2`, and `2 * XLEN`.

## Correctness

### `ostd/src/arch/riscv/irq/ipi.rs` line 80

> ```diff
> let ret = sbi_rt::remote_fence_i(hart_mask);
> if ret.error != 0 {
>     crate::warn!("SBI remote fence.i failed: error code {}", ret.error);
> }
> return;
> ```

`propagate-errors` (major): `remote_fence_i_all_online_harts()` treats every nonzero `SbiRet::error` as a warning and returns normally. On firmware where the optional `SBI RFENCE` extension is unavailable, `remote_fence_i()` returns `SBI_ERR_NOT_SUPPORTED`, but `sys_riscv_flush_icache()` still returns `0`; another hart can consequently execute stale JIT instructions. SBI extensions are optional according to the [SBI specification](https://github.com/riscv-non-isa/riscv-sbi-doc/blob/v3.0/src/intro.adoc).

**Fix.** Probe the `SBI RFENCE` extension during RISC-V initialization and install a synchronous IPI-based `fence.i` fallback, or fail initialization when SMP requires it. At minimum, return a `Result` from the helper and `flush_icache()` so callers cannot report success after any target hart was not fenced.

### `ostd/src/arch/riscv/irq/ipi.rs` line 125

> ```diff
> #[cfg(ktest)]
> mod tests {
>     use super::{HwCpuId, first_word_hart_mask, single_hart_mask};
> 
>     #[ktest]
>     fn combines_sparse_harts_in_first_mask_word() {
> ```

`add-regression-tests` (minor): The mask-construction regression tests contain no source comment referencing issue `#98`, so their connection to the rejected all-ones hart mask is not recoverable from the test module itself.

**Fix.** Add a comment above the test module or first test identifying issue `#98` and the invalid-hart-mask failure it guards against.

### `test/initramfs/src/regression/io/file_io/fcntl_dupfd.c` line 16

> ```diff
> FN_TEST(dupfd_accepts_source_fd_as_minimum)
> {
>     int duplicated_fd = TEST_RES(fcntl(fd, F_DUPFD, fd), _ret > fd);
> ```

`add-regression-tests` (minor): The new `F_DUPFD` and `F_DUPFD_CLOEXEC` regression tests do not contain the required reference to issue `#97`; the reference exists only in the commit message and will be lost when readers encounter the test later.

**Fix.** Add a nearby source comment such as `// Regression test for issue #97.` explaining the equality case covered by both tests.

## Documentation

### `docs/superpowers/plans/2026-09-03-riscv-desktop-integration-baseline.md` line 3

> ```diff
> > **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> 
> **Architecture:** Keep the immutable Debian browser rootfs and its fast development overlay unchanged. Integrate independent kernel fixes as separate commits, repair only confirmed baseline-test drift, and verify each affected subsystem locally before any remote merge or push. Older stacked browser/input PRs are classified against the current implementation instead of being merged wholesale.
> ```

`semantic-line-breaks` (nit): `docs/superpowers/plans/2026-09-03-riscv-desktop-integration-baseline.md` repeatedly places multiple sentences and independent clauses on one physical line, notably lines `3` and `7`; line `49` also splits the noun phrase “the fixture bootargs” across lines rather than breaking at a semantic boundary.

**Fix.** Reflow prose so each sentence occupies its own line and longer sentences break at clause boundaries; move the line `49` break before `and change` or make it a separate sentence.

### `docs/superpowers/plans/2026-09-03-riscv-desktop-integration-baseline.md` line 82

> ```diff
> - [ ] **Step 1: Review commit `73514169d424` against the current branch**
> 
> Run the Asterinas review pipeline in diff mode after applying the commit, with `origin/main` as the review base, and reject the integration on any confirmed P0/P1 defect.
> 
> - [ ] **Step 2: Cherry-pick the exact PR commit**
> ```

Incorrect procedure (major): Task `3`, Step `1` requires reviewing commit `73514169d424` “after applying” it, but the cherry-pick does not occur until Step `2`. Following the checklist literally therefore reviews the pre-change `HEAD`; deferring the review and using `origin/main` as the base also reviews the entire divergent integration series rather than isolating PR `#99`. The `P0`/`P1` rejection terminology additionally does not match the pipeline's `critical`/`major` severities.

**Fix.** Swap Steps `1` and `2`. After the cherry-pick, review with the saved pre-pick commit or `HEAD^` as the diff base, and reject confirmed `critical` or `major` findings.

### `docs/superpowers/plans/2026-09-03-riscv-desktop-integration-baseline.md` line 119

> ```diff
> - [ ] **Step 2: Verify the RISC-V OSTD ktest configuration**
> 
> ```bash
> RUSTFLAGS="--cfg ktest" cargo check -p ostd --target riscv64imac-unknown-none-elf
> ```
> 
> Expected: exit status 0.
> ```

Incomplete verification (major): Task `4` presents `RUSTFLAGS="--cfg ktest" cargo check ...` as verification of the newly added `#[ktest]` cases, but `cargo check` only type-checks them and never executes their assertions. An incorrect hart mask can therefore pass every prescribed baseline step.

**Fix.** Keep the compile check if desired, but also run `TARGET_ARCH=riscv64 SMP=4 make ktest` or an equivalent RISC-V `cargo osdk test` command. Rebuild with `make kernel TARGET_ARCH=riscv64` afterward because the kernel-test build replaces the normal artifact.
