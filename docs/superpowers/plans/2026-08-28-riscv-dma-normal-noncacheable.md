# RISC-V DMA Normal Non-Cacheable Mapping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Map non-coherent RISC-V DMA allocations as normal non-cacheable main memory (`PBMT_NC`) while preserving `PBMT_IO` for MMIO.

**Architecture:** Add one private DMA policy selector in `ostd/src/mm/dma/util.rs`; RISC-V selects the existing `WriteCombining` representation of `PBMT_NC`, while other architectures preserve their current behavior. Add focused ktests at the selector and RISC-V PTE boundaries, then verify with compile-only RISC-V OSDK checks and the existing host DWMAC model.

**Tech Stack:** Rust 2024, Asterinas OSTD ktests, cargo-osdk 0.18.0, RISC-V Svpbmt.

---

## File map

- Modify `ostd/src/mm/dma/util.rs`: select the architecture-appropriate cache
  policy for DMA allocations and test the RISC-V selection.
- Modify `ostd/src/arch/riscv/mm/mod.rs`: freeze the independent PBMT encodings
  for normal non-cacheable RAM and MMIO.
- Create `docs/porting/evidence/riscv-dma-memory-type-contract.md`: record the
  specification mapping, exact change, test evidence, and remaining ordering
  assumptions.

### Task 1: Freeze the DMA policy and PBMT distinction

**Files:**
- Modify: `ostd/src/mm/dma/util.rs`
- Modify: `ostd/src/arch/riscv/mm/mod.rs`

- [ ] **Step 1: Add the failing selector test**

Add a RISC-V-only ktest beside `alloc_kva` that calls a not-yet-existing
`dma_cache_policy(false)` and expects `CachePolicy::WriteCombining`:

```rust
#[cfg(all(ktest, target_arch = "riscv64"))]
mod tests {
    use super::{CachePolicy, dma_cache_policy};
    use crate::prelude::ktest;

    #[ktest]
    fn noncoherent_dma_uses_normal_noncacheable_memory() {
        assert_eq!(dma_cache_policy(false), CachePolicy::WriteCombining);
        assert_eq!(dma_cache_policy(true), CachePolicy::Writeback);
    }
}
```

- [ ] **Step 2: Run the RISC-V ktest compile and record RED**

Run the pinned local container command used by the existing RISC-V gate:

```bash
docker run --rm --network=host \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  cargo osdk check --ktests -p ostd \
    --target riscv64imac-unknown-none-elf
```

Expected: nonzero with an unresolved import for `dma_cache_policy`, while the
new test itself parses.

- [ ] **Step 3: Add the minimal selector and use it**

In `ostd/src/mm/dma/util.rs`, add:

```rust
const fn dma_cache_policy(is_cache_coherent: bool) -> CachePolicy {
    if is_cache_coherent {
        return CachePolicy::Writeback;
    }

    #[cfg(target_arch = "riscv64")]
    return CachePolicy::WriteCombining;

    #[cfg(not(target_arch = "riscv64"))]
    return CachePolicy::Uncacheable;
}
```

Replace the inline `if is_cache_coherent` expression in `alloc_kva` with
`dma_cache_policy(is_cache_coherent)`. Document that the RISC-V selection is
normal, idempotent, weakly ordered DMA RAM, not MMIO.

- [ ] **Step 4: Freeze the MMIO side of the PTE contract**

Keep the existing `write_combining_encodes_as_pbmt_nc_and_round_trips` test and
add this independent test in `ostd/src/arch/riscv/mm/mod.rs`:

```rust
#[ktest]
fn uncacheable_encodes_as_pbmt_io_and_round_trips() {
    let prop = PageProperty {
        flags: PageFlags::R | PageFlags::W,
        cache: CachePolicy::Uncacheable,
        priv_flags: PrivFlags::USER,
    };
    let pte = PageTableEntry::new_page(0x8000_0000, 1 as PagingLevel, prop);
    assert_ne!(pte.0 & PteFlags::PBMT_IO.bits(), 0);
    assert_eq!(pte.0 & PteFlags::PBMT_NC.bits(), 0);

    let PteScalar::Mapped(_, back) = pte.to_repr(1 as PagingLevel) else {
        panic!("PTE must remain mapped");
    };
    assert_eq!(back.cache, CachePolicy::Uncacheable);
}
```

- [ ] **Step 5: Run focused GREEN checks**

Run:

```bash
cargo fmt --all -- --check
docker run --rm --network=host \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  cargo osdk check --ktests -p ostd \
    --target riscv64imac-unknown-none-elf
make test_riscv_dwmac_rx_model
git diff --check
```

Expected: all commands exit 0; the host DWMAC model remains 11/11 green.

- [ ] **Step 6: Commit the implementation**

```bash
git add ostd/src/mm/dma/util.rs ostd/src/arch/riscv/mm/mod.rs
git commit -m "fix(riscv): map DMA RAM as PBMT NC"
```

### Task 2: Record the verified contract and remaining assumptions

**Files:**
- Create: `docs/porting/evidence/riscv-dma-memory-type-contract.md`

- [ ] **Step 1: Write the evidence note**

Record:

- the ratified Svpbmt definitions for `PBMT_NC` and `PBMT_IO`;
- the exact `DmaCoherent(false)` to PTE mapping path;
- the RED and GREEN command outcomes;
- confirmation that MMIO remains `PBMT_IO`;
- confirmation that x86 and LoongArch behavior is unchanged;
- the physical TX-reclaim evidence that motivated, but does not prove, the
  change;
- the unresolved need for descriptor and MMIO ordering barriers.

- [ ] **Step 2: Verify the documentation and final diff**

Run:

```bash
rg -n "PBMT_NC|PBMT_IO|descriptor|barrier|non-goal" \
  docs/porting/evidence/riscv-dma-memory-type-contract.md
git diff --check
git status --short
```

Expected: the evidence note contains every required boundary, diff check exits
0, and only the evidence note is uncommitted.

- [ ] **Step 3: Commit the evidence note**

```bash
git add docs/porting/evidence/riscv-dma-memory-type-contract.md
git commit -m "docs(riscv): record DMA memory type contract"
```

### Task 3: Final bounded verification

**Files:**
- Verify only; no production edits expected.

- [ ] **Step 1: Run the final bounded gate once**

Run exactly once after both commits:

```bash
cargo fmt --all -- --check
docker run --rm --network=host \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  cargo osdk check --ktests -p ostd \
    --target riscv64imac-unknown-none-elf
make test_riscv_dwmac_rx_model
git diff --check
git status --short --branch
```

Expected: every command exits 0 and the worktree is clean. Do not run QEMU or
the physical board in this milestone.
