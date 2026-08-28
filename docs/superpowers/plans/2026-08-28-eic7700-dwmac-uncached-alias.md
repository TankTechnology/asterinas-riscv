# EIC7700 DWMAC Uncached Alias Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Megrez DWMAC descriptor page provably uncached even though the board does not implement Svpbmt.

**Architecture:** Keep ownership and unsafe alias construction in OSTD. A consuming `DmaCoherent::into_uncached` operation retains PBMT_NC where available, otherwise attaches the EIC7700 hardware DRAM alias after cleaning the original KVA, and fails closed when neither mechanism exists. DWMAC opts its descriptor page into this guarantee without changing packet buffers, IRQs, or ring protocol.

**Tech Stack:** Rust `no_std`, OSTD page/DMA abstractions, RISC-V Svpbmt detection, EIC7700 System Port alias, Asterinas ktests, Python source-contract tests, cargo-osdk RISC-V checks.

---

### Task 1: Freeze the strategy and alias arithmetic with RED tests

**Files:**
- Modify: `ostd/src/arch/riscv/mm/eic7700_cache.rs`
- Modify: `ostd/src/arch/riscv/mm/mod.rs`

- [ ] **Step 1: Add pure failing strategy tests**

Add tests requiring that `(svpbmt=true, alias=false)` selects the page-table
path, `(false, true)` selects the platform alias, and `(false, false)` fails.
Add exact range tests mapping `0x2_a082_a000..0x2_a082_b000` into the EIC7700
alias while rejecting empty, below-DRAM, and beyond-16-GiB ranges.

- [ ] **Step 2: Run RED**

```bash
cargo test -p ostd --lib arch::riscv::mm --no-default-features
```

If host execution cannot compile the architecture module, run the existing
Python source gate that compiles the focused RISC-V ktests. Expected RED is a
missing strategy/alias-range function, not a toolchain or import failure.

- [ ] **Step 3: Implement the minimum architecture helper**

Add a private strategy enum and checked EIC7700 alias arithmetic. Add one
crate-visible helper that returns no alias for a real Svpbmt mapping, creates
an OSTD-owned `IoMem` alias for EIC7700 after cleaning the cached KVA, and
returns `Error::AccessDenied` when neither path is valid.

- [ ] **Step 4: Run focused GREEN**

Run the exact RED command or its pinned RISC-V equivalent and require all new
strategy/range cases to pass.

### Task 2: Make `DmaCoherent` retain an uncached CPU view

**Files:**
- Modify: `ostd/src/mm/dma/dma_coherent.rs`
- Modify: `ostd/src/mm/dma/test.rs`

- [ ] **Step 1: Add failing ownership/access tests**

Freeze the public consuming API name `into_uncached`. Require rejection of a
coherent allocation, unchanged paddr/daddr/size after conversion, alias-backed
reader/writer selection, and alias slicing across `Split`.

- [ ] **Step 2: Run RED**

Run the focused OSTD DMA ktest compilation. Expected RED is the absent method
or absent alias field.

- [ ] **Step 3: Implement the owning alias field**

Add `Option<IoMem>` to `DmaCoherent`; initialize it to `None` in existing
constructors, route pointer/reader/writer access through it when present, and
slice it in `Split`. Implement `into_uncached` with the architecture helper.
Do not change the behavior of callers that do not opt in.

- [ ] **Step 4: Run GREEN and static checks**

Require focused DMA tests, rustfmt, and RISC-V ktest compilation to pass.

### Task 3: Opt the DWMAC descriptor ring into the guarantee

**Files:**
- Modify: `kernel/comps/dwmac/src/queue.rs`
- Modify: `tools/riscv/tests/test_dwmac_rx_liveness_model.py`
- Modify: `docs/porting/evidence/riscv-dma-memory-type-contract.md`
- Modify: `docs/porting/evidence/megrez-dwmac-rx-liveness-contract.md`

- [ ] **Step 1: Add the DWMAC integration RED**

Extend the existing source contract to require
`DmaCoherent::alloc(1, false)?.into_uncached()?` before the first descriptor
write. It must reject a ring that only requests `WriteCombining`/PBMT_NC.

- [ ] **Step 2: Run RED**

```bash
make test_riscv_dwmac_rx_model
```

Expected: the new integration assertion fails while the existing model cases
remain green.

- [ ] **Step 3: Apply the one-line DWMAC opt-in and correct the evidence docs**

Convert only the ring allocation. Record that the previous PBMT contract is
conditional on detected Svpbmt and that the Megrez run used the platform alias
because its exact ISA string lacks the extension. Record the sealed run as
`tx-reclaim-partial/stale-cpu-view`: 16 KiB passed, two descriptors reclaimed,
then the 64-entry ring filled while RX continued.

- [ ] **Step 4: Run bounded GREEN gates**

```bash
make test_riscv_dwmac_rx_model
cargo fmt --check -- kernel/comps/dwmac/src/queue.rs \
  ostd/src/mm/dma/dma_coherent.rs ostd/src/arch/riscv/mm/mod.rs \
  ostd/src/arch/riscv/mm/eic7700_cache.rs
```

Then run one pinned-container command:

```bash
cargo osdk check --ktests -p ostd -p aster-dwmac -p aster-network \
  -p aster-kernel --target riscv64imac-unknown-none-elf
```

Expected: all focused host tests and the RISC-V compile gate pass. Do not run
QEMU or the board; the next physical discriminator requires a newly frozen
kernel and separate recovery evidence.

- [ ] **Step 5: Commit**

```bash
git add ostd/src/arch/riscv/mm/eic7700_cache.rs \
  ostd/src/arch/riscv/mm/mod.rs ostd/src/mm/dma/dma_coherent.rs \
  ostd/src/mm/dma/test.rs kernel/comps/dwmac/src/queue.rs \
  tools/riscv/tests/test_dwmac_rx_liveness_model.py \
  docs/porting/evidence/riscv-dma-memory-type-contract.md \
  docs/porting/evidence/megrez-dwmac-rx-liveness-contract.md
git commit -m "fix(riscv): use EIC7700 uncached DMA alias"
```
