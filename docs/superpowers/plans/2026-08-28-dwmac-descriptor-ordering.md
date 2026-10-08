# DWMAC Descriptor Ordering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce body-before-OWN, OWN-before-tail, and OWN-clear-before-body ordering for the Megrez DWMAC descriptor ring.

**Architecture:** Keep descriptor encoding pure, split DMA-ring I/O into body and control-word operations, and call DWMAC-local read/write barriers at each CPU/device ownership boundary. Extend the existing host cache-line model and source-contract gate before changing production code.

**Tech Stack:** Rust 2024, Python unittest, Asterinas OSTD/DWMAC ktests, RISC-V device fences, Linux stmmac reference.

---

### Task 1: Establish the ordering RED

**Files:**
- Modify: `tools/riscv/dwmac_tx_cacheline_model.rs`
- Modify: `tools/riscv/tests/test_dwmac_rx_liveness_model.py`

- [ ] **Step 1: Extend the visibility model**

Add tests that enumerate visibility of descriptor body, OWN, and tail. The
unbarriered protocol must exhibit a state with tail and OWN visible but body
absent; the staged protocol must reject it. Add the inverse completion case in
which OWN-clear is visible before the updated body.

- [ ] **Step 2: Bind production to the model**

Add a source-contract test requiring, in order:

```text
write body -> dma_write_barrier -> write control
queue send/refill -> dma_write_barrier -> tail MMIO
read control -> dma_read_barrier -> read body
```

Also require that `descriptor.rs` no longer contains local atomic fences.

- [ ] **Step 3: Run RED**

Run:

```bash
make test_riscv_dwmac_rx_model
```

Expected: the visibility-model tests pass, but the production source-contract
test fails because the staged I/O and barrier calls do not exist.

### Task 2: Implement staged descriptor I/O

**Files:**
- Modify: `kernel/comps/dwmac/src/descriptor.rs`
- Modify: `kernel/comps/dwmac/src/queue.rs`
- Modify: `kernel/comps/dwmac/src/arch/riscv.rs`
- Modify: `kernel/comps/dwmac/src/arch/other.rs`
- Modify: `kernel/comps/dwmac/src/device.rs`

- [ ] **Step 1: Make descriptor encoding pure**

Remove local atomic fences. Add crate-private accessors for `[u32; 3]` body
words, the control word, reconstruction from those parts, and ownership
testing from a control word.

- [ ] **Step 2: Add platform barrier wrappers**

RISC-V `dma_write_barrier` and `dma_read_barrier` call
`ostd::arch::device::io_mem::fence()`. Other architectures use a SeqCst fence
to preserve compilation.

- [ ] **Step 3: Stage DMA-ring writes and reads**

`write_descriptor` writes the body, calls `dma_write_barrier`, then writes the
control word. `read_descriptor` reads control first, returns immediately for
DMA-owned entries, otherwise calls `dma_read_barrier`, reads the body, and
reconstructs the descriptor.

- [ ] **Step 4: Order every tail notification**

Call `dma_write_barrier` before initial TX/RX tails, RX refill/resume tails,
and TX submission tails.

- [ ] **Step 5: Run focused GREEN**

Run:

```bash
make test_riscv_dwmac_rx_model
cargo fmt --package aster-dwmac -- --check
docker run --rm --network=host \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  cargo osdk check --ktests -p aster-dwmac \
    --target riscv64imac-unknown-none-elf
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit**

```bash
git add tools/riscv/dwmac_tx_cacheline_model.rs \
  tools/riscv/tests/test_dwmac_rx_liveness_model.py \
  kernel/comps/dwmac/src/descriptor.rs kernel/comps/dwmac/src/queue.rs \
  kernel/comps/dwmac/src/arch/riscv.rs kernel/comps/dwmac/src/arch/other.rs \
  kernel/comps/dwmac/src/device.rs
git commit -m "fix(riscv): order DWMAC descriptor handoff"
```

### Task 3: Record evidence and run the final bounded gate

**Files:**
- Create: `docs/porting/evidence/megrez-dwmac-descriptor-ordering.md`

- [ ] **Step 1: Record source authority, RED/GREEN, and limits**

Document the Linux sequence, the prior misplaced fences, the new production
sequence, model results, compile evidence, and the fact that hardware remains
unverified.

- [ ] **Step 2: Commit the evidence**

```bash
git add docs/porting/evidence/megrez-dwmac-descriptor-ordering.md
git commit -m "docs(riscv): record DWMAC ordering contract"
```

- [ ] **Step 3: Run one final bounded gate**

Run the host model, DWMAC package formatting, pinned RISC-V OSDK ktest compile,
`git diff --check`, and `git status --short --branch` once. Do not run QEMU or
the physical board.
