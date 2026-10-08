# Megrez DWMAC Descriptor Ordering Contract

Date: 2026-08-28

## Scope and authority

This milestone fixes the CPU-to-DWMAC descriptor handoff that remains after
mapping the noncoherent descriptor ring as RISC-V `PBMT_NC`. The primary
software references are the ESWIN Linux 6.6 tree pinned at
`fc6038c00e006226e3bd504d2679c534eabf5503`, upstream Linux stmmac, the Linux
DMA API guide, and the RISC-V Linux barrier definitions.

Linux publishes descriptor contents before `OWN`, executes `wmb()` before the
tail-pointer MMIO write, and executes `dma_rmb()` after observing cleared
`OWN` before consuming completion fields. On RISC-V, Linux's device write
barrier is `fence ow,ow`. Asterinas uses the existing conservative safe
`ostd::arch::device::io_mem::fence`, which emits `fence iorw,iorw`.

## Defect and implemented protocol

The old Release fences ran while building a stack-local `Descriptor`. Every
real descriptor-ring store happened later when `DmaQueue::write_descriptor`
copied all 16 bytes, so the fences did not order DMA-visible body stores before
the `OWN` store. The completion path had the inverse defect: it copied the
body before testing `OWN`, then executed an Acquire fence too late to order
those reads. Tail MMIO writes had no explicit device barrier.

The queue now implements this protocol:

1. write descriptor words 0-2 into the uncached ring;
2. execute `dma_write_barrier`;
3. publish word 3, including `OWN`, with one `VmIoOnce::write_once` access;
4. execute `dma_write_barrier` before every initial, TX, RX-refill, and RBU
   tail-pointer MMIO write;
5. read word 3 with one `VmIoOnce::read_once` access;
6. while `OWN` is set, return without reading words 0-2;
7. after observing cleared `OWN`, execute `dma_read_barrier`, then read the
   completion body.

The RISC-V barrier wrappers call OSTD's full I/O fence. The non-RISC-V module
keeps the component buildable with conservative sequentially consistent
fences; no generic OSTD DMA-barrier API was introduced.

## Executable evidence

TDD first extended the reduced host model. Its initial production-binding run
failed because `DmaQueue` had no staged body/control access. A second focused
RED showed that the first implementation still used bulk `VmIo` for the OWN
word. The final source contract requires the staged order, single-access OWN
read/write, barriers before every tail write, and no fence in the stack-local
descriptor encoder.

The final bounded checks were:

```text
make test_riscv_dwmac_rx_model
  12 tests passed

cargo osdk check --ktests -p aster-dwmac \
  --target riscv64imac-unknown-none-elf
  Finished dev profile; exit 0

rustfmt --edition 2024 --check tools/riscv/dwmac_tx_cacheline_model.rs
ruff check tools/riscv/tests/test_dwmac_rx_liveness_model.py
ruff format --check tools/riscv/tests/test_dwmac_rx_liveness_model.py
cargo fmt --package aster-dwmac -- --check
git diff --check
  all exit 0
```

The RISC-V build used the pinned
`asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached` image, the pinned
nightly toolchain, host Cargo caches mounted read-only, networking disabled,
and an ephemeral target directory. It completed in 11 seconds after the final
`VmIoOnce` change.

## Claim boundary

This milestone proves that the implementation is structurally bound to the
Linux descriptor-ordering protocol and compiles for the target architecture.
The finite-state model excludes the incomplete visible states that either
missing publication barrier would admit. It does not prove EIC7700 cache or
DWMAC behavior, and it does not claim that ordering is the only remaining
cause of TX reclaim stalls. QEMU cannot reproduce this board-specific DWMAC
and cache hierarchy. A later single recovery-armed Megrez run remains the
hardware discriminator; no board reset or QEMU run was used here.
