# RISC-V SATP TLB Flush Implementation Plan

**Goal:** Ensure a RISC-V page-table activation cannot reuse a translation cached for the previous ASID-zero address space.

**Architecture:** Make `activate_page_table` issue the existing global RISC-V
TLB flush immediately after writing `satp`. Keep the change inside the current
RISC-V activation path; do not add another activation API or a test-only
hardware abstraction.

**Test constraint:** The RISC-V ISA permits an implementation to invalidate
translations eagerly on a `satp` write. QEMU does so, which makes a
two-`VmSpace` behavioral test pass even before this fix. The deterministic
regression evidence is therefore before/after disassembly, supplemented by
existing OSTD kernel tests and real-hardware validation.

---

### Task 1: Establish the failing hardware contract

- [x] Confirm from the official RISC-V privileged ISA that a `satp` write does
  not imply address-translation-cache invalidation and that software using
  ASID zero should execute `SFENCE.VMA` after each write.
- [x] Build the unmodified RISC-V OSTD test image and disassemble
  `activate_page_table`.
- [x] Verify that the unmodified function returns immediately after
  `satp::set`, without reaching an `sfence.vma`.
- [x] Try the production-path two-`VmSpace` behavioral test. Record that it
  passes before the fix under QEMU, and remove it rather than adding a test that
  cannot detect this regression.

### Task 2: Flush after changing `satp`

**Files:**
- Modify: `ostd/src/arch/riscv/mm/mod.rs`

- [x] **Step 1: Add the minimal architecture fix**

Immediately after the existing `satp::set` block in `activate_page_table`, add:

```rust
    // All address spaces currently use ASID 0. The RISC-V privileged ISA says
    // a `satp` write need not invalidate translation caches, so flush before
    // using the new page table.
    // Reference: <https://docs.riscv.org/reference/isa/priv/supervisor.html>.
    tlb_flush_all_including_global();
```

- [x] **Step 2: Verify the final instruction sequence**

Rebuild the RISC-V OSTD test image and disassemble
`activate_page_table` and `tlb_flush_all_including_global`.

Expected: `satp::set` returns to a call to
`tlb_flush_all_including_global`; that function executes `sfence.vma` before
returning.

- [x] **Step 3: Run the required architecture matrix**

Run:

```bash
TARGET_ARCH=riscv64 SMP=4 make ktest
TARGET_ARCH=riscv64 FEATURES=riscv_sv39_mode make kernel
TARGET_ARCH=x86_64 make kernel
TARGET_ARCH=loongarch64 make kernel
```

Expected: the default Sv48 OSTD suite reports 202 passed and zero failed. The
Sv39, x86-64, and LoongArch kernel builds exit zero.

- [x] **Step 4: Run source-quality checks**

Run:

```bash
cargo fmt --all -- --check
git diff --check
make check
```

Expected: all commands exit zero.

- [x] **Step 5: Review before commit**

Run the Asterinas maintainability, development, hardware, and security review
against the exact changed lines. Inspect every reported defect and present the
uncommitted diff, the test-coverage limitation, and fresh evidence to the user.

- [ ] **Step 6: Commit only after user approval**

After explicit approval:

```bash
git add ostd/src/arch/riscv/mm/mod.rs
git commit -m "Flush RISC-V TLBs after page table activation"
```

Expected: one atomic commit containing only the RISC-V activation fix.
