# RISC-V Streaming DMA Uncached Alias Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Guarantee fresh CPU reads from reused non-coherent `DmaStream` bounce buffers on EIC7700.

**Architecture:** Keep the existing KVA as backing ownership and add one optional OSTD `IoMem` alias for CPU access. Reuse the architecture's checked uncached-alias constructor, route bounce copies and direct stream readers/writers through the selected view, split the view with the stream, and fail closed if RISC-V has neither Zicbom, Svpbmt, nor a platform alias.

**Tech Stack:** Rust `no_std`, OSTD DMA and `IoMem`, RISC-V ktests, cargo-osdk, existing Python DWMAC/network host gates.

---

### Task 1: Freeze the streaming-DMA access policy

**Files:**
- Modify: `ostd/src/mm/dma/dma_stream.rs`
- Modify: `ostd/src/mm/dma/test.rs`

- [ ] **Step 1: Write the failing policy and identity tests**

Add a pure policy test requiring an alias only for
`!is_cache_coherent && !can_sync_dma`. Add a stream test that records
`paddr`, `daddr`, size, and optional alias, splits the stream, and requires all
identities and alias offsets to remain exact.

- [ ] **Step 2: Run RED**

```bash
cargo osdk check --ktests -p ostd --target riscv64imac-unknown-none-elf
```

Expected: compilation fails because the policy/accessor and retained stream
alias do not exist.

- [ ] **Step 3: Commit the RED test only after recording the failure**

```bash
git add ostd/src/mm/dma/test.rs
git commit -m "test(riscv): expose streaming DMA cache contract"
```

### Task 2: Route bounce access through a guaranteed CPU view

**Files:**
- Modify: `ostd/src/mm/dma/dma_stream.rs`
- Modify: `ostd/src/arch/riscv/mm/mod.rs`

- [ ] **Step 1: Retain the optional alias**

Add `uncached_alias: Option<IoMem>` to `DmaStream`. During both `alloc_uninit`
and `map`, call `create_uncached_dma_alias` only for the non-coherent,
non-cache-maintainable RISC-V bounce path. Construct the alias before
`prepare_dma`, so failure leaves no prepared mapping.

- [ ] **Step 2: Use the alias for every CPU bounce access**

Select the alias pointer in `sync_via_copying`, `reader`, and `writer` when it
exists. Keep PBMT_NC and non-RISC-V KVA behavior unchanged. Split the alias at
the same page-aligned offset as the KVA and segment.

- [ ] **Step 3: Generalize the architecture helper's ownership comment**

State that the DMA object, rather than specifically `DmaCoherent`, retains the
backing frames for the lifetime of the alias. Do not change alias arithmetic or
hardware register behavior.

- [ ] **Step 4: Run focused GREEN**

```bash
cargo osdk check --ktests -p ostd --target riscv64imac-unknown-none-elf
```

Expected: all OSTD DMA ktests compile, including the new policy and split
identity cases.

### Task 3: Correct evidence and run bounded regression gates

**Files:**
- Modify: `docs/porting/evidence/riscv-dma-memory-type-contract.md`
- Modify: `docs/porting/evidence/megrez-network-hardware-source-ledger.md`

- [ ] **Step 1: Correct the packet-buffer claim**

Record that the descriptor ring used the alias first, while packet-buffer
streaming DMA remained vulnerable until this change. Preserve the distinction
between proven static contract, high-confidence physical diagnosis, and pending
post-fix board validation.

- [ ] **Step 2: Run focused host and static gates**

```bash
make test_riscv_dwmac_rx_model
python3 -m unittest tools.riscv.tests.test_megrez_dwmac -v
cargo fmt --check -- ostd/src/mm/dma/dma_stream.rs \
  ostd/src/arch/riscv/mm/mod.rs ostd/src/mm/dma/test.rs
```

Expected: all focused tests pass and formatting is clean.

- [ ] **Step 3: Run the bounded RISC-V compile and Clippy gates**

```bash
cargo osdk check --ktests -p ostd -p aster-kernel \
  --target riscv64imac-unknown-none-elf
RUSTFLAGS=-Dwarnings cargo clippy -p aster-network -p aster-dwmac \
  --target riscv64imac-unknown-none-elf --no-deps
```

Expected: both commands exit zero. Do not launch QEMU or the board in this
task.

- [ ] **Step 4: Commit the implementation and evidence**

```bash
git add ostd/src/mm/dma/dma_stream.rs ostd/src/arch/riscv/mm/mod.rs \
  docs/porting/evidence/riscv-dma-memory-type-contract.md \
  docs/porting/evidence/megrez-network-hardware-source-ledger.md
git commit -m "fix(riscv): use uncached streaming DMA bounce buffers"
```

