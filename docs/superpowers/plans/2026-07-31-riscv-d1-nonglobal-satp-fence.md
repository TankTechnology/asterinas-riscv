# RISC-V D1 Non-Global SATP Fence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:executing-plans` to implement this plan task-by-task. Do not
> commit, rebase, push, or create a PR before the user reviews the resulting
> diff and test evidence.

**Goal:** Preserve global kernel translations while invalidating every
non-global ASID-0 translation after a RISC-V `satp` switch.

**Architecture:** Correct the existing
`tlb_flush_all_excluding_global()` primitive so it encodes
`SFENCE.VMA x0, <non-x0 register containing ASID 0>`, then use that primitive
from `activate_page_table()`. Retain the existing global flush after first
kernel-page-table activation.

**Tech Stack:** Rust 2024, RISC-V privileged ISA, inline assembly, OSDK kernel
builds/tests, LLVM `rust-objdump`.

---

### Task 1: Establish the failing instruction contract

**Files:**

- Inspect: `ostd/src/arch/riscv/mm/mod.rs`
- Evidence only: `/tmp/riscv-d1-before.dis`

- [ ] **Step 1: Build the current D1 OSTD image**

Run:

```bash
docker exec codex-riscv-d1 bash -lc '
  cd /root/asterinas &&
  TARGET_ARCH=riscv64 SMP=4 make ktest
'
```

Expected: the current OSTD kernel-test run completes; the existing branch
still calls `tlb_flush_all_including_global()` after `satp::set`.

- [ ] **Step 2: Capture the current excluding-global helper**

Run:

```bash
docker exec codex-riscv-d1 bash -lc '
  cd /root/asterinas &&
  rust-objdump -d --demangle \
    --disassemble-symbols="ostd::arch::mm::tlb_flush_all_excluding_global" \
    target/riscv64imac-unknown-none-elf/debug/ostd-osdk-bin
' | tee /tmp/riscv-d1-before.dis
```

Expected: the helper contains a bare `sfence.vma`, whose encoding is
`0x12000073` (`rs1=x0`, `rs2=x0`).

- [ ] **Step 3: Run the new contract assertion and observe RED**

Run:

```bash
grep -Eq 'sfence\.vma[[:space:]]+zero,[[:space:]]*[a-z][a-z0-9]*' \
  /tmp/riscv-d1-before.dis
```

Expected: exit status is nonzero because the current helper flushes global
translations and has no non-`x0` ASID operand.

### Task 2: Implement the exact non-global fence

**Files:**

- Modify: `ostd/src/arch/riscv/mm/mod.rs`

- [ ] **Step 1: Replace the incorrect excluding-global helper**

Replace:

```rust
pub(crate) fn tlb_flush_all_excluding_global() {
    riscv::asm::sfence_vma_all()
}
```

with:

```rust
/// Flushes every non-global translation for ASID 0.
pub(crate) fn tlb_flush_all_excluding_global() {
    let asid = 0usize;

    // `rs1 = x0` selects all virtual addresses. Encoding `rs2` as a
    // non-`x0` register containing zero selects ASID 0 without flushing
    // global translations.
    // SAFETY: `SFENCE.VMA` only orders page-table accesses and invalidates
    // address-translation cache entries on the current hart.
    unsafe {
        core::arch::asm!(
            "sfence.vma x0, {asid}",
            asid = in(reg) asid,
            options(nostack)
        );
    }
}
```

- [ ] **Step 2: Use it after the `satp` write**

Change the end of `activate_page_table()` to:

```rust
    // All address spaces currently use ASID 0. A `satp` write need not
    // invalidate translation caches, so invalidate the old non-global ASID-0
    // translations before using the new page table.
    // Reference: <https://docs.riscv.org/reference/isa/priv/supervisor.html>.
    tlb_flush_all_excluding_global();
```

Do not change `activate_kernel_page_table()` or
`tlb_flush_all_including_global()`.

- [ ] **Step 3: Format the changed file**

Run:

```bash
docker exec codex-riscv-d1 bash -lc '
  cd /root/asterinas && cargo fmt --all
'
```

Expected: only `ostd/src/arch/riscv/mm/mod.rs` is modified.

### Task 3: Verify GREEN at the machine-code boundary

**Files:**

- Evidence only: `/tmp/riscv-d1-after.dis`

- [ ] **Step 1: Rebuild the RISC-V OSTD image**

Run Task 1 Step 1 again.

Expected: the RISC-V OSTD suite passes.

- [ ] **Step 2: Capture the corrected helper**

Run:

```bash
docker exec codex-riscv-d1 bash -lc '
  cd /root/asterinas &&
  rust-objdump -d --demangle \
    --disassemble-symbols="ostd::arch::mm::tlb_flush_all_excluding_global" \
    target/riscv64imac-unknown-none-elf/debug/ostd-osdk-bin
' | tee /tmp/riscv-d1-after.dis
```

- [ ] **Step 3: Require the exact operand shape**

Run:

```bash
grep -Eq 'sfence\.vma[[:space:]]+zero,[[:space:]]*[a-z][a-z0-9]*' \
  /tmp/riscv-d1-after.dis
! grep -Eq '^[[:space:][:xdigit:]]+:[[:space:][:xdigit:]]+.*sfence\.vma[[:space:]]*$' \
  /tmp/riscv-d1-after.dis
```

Expected: both commands succeed. The instruction uses `rs1=x0` and a named
non-`x0` register for `rs2`; the helper no longer contains the global
`sfence.vma x0,x0` encoding.

- [ ] **Step 4: Verify the activation call target**

Disassemble `ostd::arch::mm::activate_page_table` and verify that the call
immediately after `riscv::register::satp::set` targets
`tlb_flush_all_excluding_global`.

### Task 4: Run the D1 verification matrix

**Files:**

- No additional source files

- [ ] **Step 1: Run alternate and unaffected architecture builds**

Run:

```bash
docker exec codex-riscv-d1 bash -lc '
  cd /root/asterinas &&
  TARGET_ARCH=riscv64 FEATURES=riscv_sv39_mode make kernel &&
  TARGET_ARCH=x86_64 make kernel &&
  TARGET_ARCH=loongarch64 make kernel
'
```

Expected: all three builds exit zero.

- [ ] **Step 2: Run source-quality checks**

Run:

```bash
docker exec codex-riscv-d1 bash -lc '
  cd /root/asterinas &&
  cargo fmt --all -- --check &&
  git diff --check &&
  make check
'
```

Expected: all checks exit zero.

- [ ] **Step 3: Review checkpoint**

Run the Asterinas maintainability, development, security, and hardware review
against the uncommitted D1 diff. Present:

- the exact diff;
- before/after disassembly;
- OSTD test counts;
- cross-architecture build results; and
- any remaining finding.

Do not commit. Wait for explicit user approval.
