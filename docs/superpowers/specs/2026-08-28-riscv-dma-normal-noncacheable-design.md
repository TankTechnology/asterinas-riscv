# RISC-V DMA Normal Non-Cacheable Mapping Design

Date: 2026-08-28

## Goal

Make non-coherent DMA allocations on RISC-V use the Svpbmt memory type for
ordinary non-cacheable main memory (`PBMT_NC`) instead of the strongly ordered
I/O type (`PBMT_IO`). This is the first, architecture-level correction derived
from the Megrez DWMAC TX-reclaim failure; it does not claim to fix the complete
descriptor protocol.

## Evidence and problem statement

The Megrez physical evidence is bound to issue #94: the DWMAC test completed a
16 KiB transfer, then stopped with `tx_submitted=64`, `tx_reclaimed=0`, and
`tx_outstanding=64`, while RX continued to make progress. Changing the
descriptor ring to `DmaCoherent::alloc(1, false)` did not restore TX reclaim.

The current mapping path is:

1. `kernel/comps/dwmac/src/queue.rs` allocates the descriptor ring with
   `DmaCoherent::alloc(1, false)`.
2. `ostd/src/mm/dma/dma_coherent.rs` sends non-coherent allocations through
   `alloc_kva`.
3. `ostd/src/mm/dma/util.rs` maps every non-coherent allocation with
   `CachePolicy::Uncacheable`.
4. `ostd/src/arch/riscv/mm/mod.rs` encodes `Uncacheable` as `PBMT_IO`.

That final step gives ordinary DRAM I/O semantics. The ratified RISC-V Svpbmt
specification defines `PBMT_NC` as non-cacheable, idempotent, weakly ordered
main memory, and `PBMT_IO` as non-cacheable, non-idempotent, strongly ordered
I/O. A descriptor ring is DMA-accessible RAM, not an MMIO register range, so
`PBMT_NC` is the correct requested memory attribute.

Linux's DMA documentation independently classifies network descriptor rings
as coherent DMA memory and warns that coherent mappings still require explicit
memory barriers. Therefore this change corrects only the mapping type; a
separate milestone must audit descriptor publication, MMIO doorbell ordering,
and reclaim barriers.

Primary references:

- RISC-V Privileged Architecture, Svpbmt 1.0:
  <https://docs.riscv.org/reference/isa/v20240411/priv/svpbmt.html>
- Linux Dynamic DMA Mapping Guide:
  <https://kernel.org/doc/html/next/core-api/dma-api-howto.html>

## Chosen design

Keep `CachePolicy::Uncacheable` dedicated to MMIO and retain its RISC-V
`PBMT_IO` encoding. Introduce a small DMA mapping-policy selector in
`ostd/src/mm/dma/util.rs`:

- cache-coherent devices continue to use `CachePolicy::Writeback`;
- non-coherent RISC-V devices use `CachePolicy::WriteCombining`, whose existing
  RISC-V mapping is `PBMT_NC` and whose PTE round trip is already lossless;
- other architectures retain the current `CachePolicy::Uncacheable` behavior.

The selector is deliberately local to DMA allocation. It does not reinterpret
all `Uncacheable` mappings and therefore cannot accidentally weaken PLIC, xHCI,
PCI BAR, or other MMIO ordering. It also avoids adding a generic
`CachePolicy::NonCacheable` variant whose exact x86 PAT and LoongArch mapping
would need a broader cross-architecture design.

The use of the existing `WriteCombining` spelling is an implementation detail
of the current generic cache-policy abstraction. On RISC-V the architecture
module already documents it as the lossless representation of `PBMT_NC`.
The DMA selector's name and comment describe the semantic intent so DMA
callers do not depend on that spelling.

## Test contract

Tests freeze three independent facts:

1. A non-coherent DMA allocation selects the RISC-V normal-memory policy.
2. That policy encodes as `PBMT_NC`, clears `PBMT_IO`, and round-trips.
3. MMIO `Uncacheable` still encodes as `PBMT_IO`, clears `PBMT_NC`, and
   round-trips.

The first test is added before production code and must initially fail to
compile because the selector does not exist. The latter two live beside the
RISC-V PTE implementation so a future mapping change cannot conflate DMA RAM
and MMIO again.

Verification is bounded to formatting, focused RISC-V OSTD ktest compilation,
and the existing host DWMAC model gate. No QEMU or physical-board run is needed
to establish the page-table contract. A later descriptor-ordering milestone
will add barriers and then authorize one high-information physical run.

## Non-goals

- no DWMAC descriptor or queue logic changes;
- no cache flush/invalidate implementation;
- no global `CachePolicy` redesign;
- no x86 or LoongArch behavior change;
- no claim that `PBMT_NC` alone fixes Megrez TX reclaim;
- no QEMU model for the EIC7700 DWMAC or cache hierarchy;
- no repeated physical reboot or board experiment.

## Follow-up milestones

After this mapping contract is green:

1. audit and test descriptor publication/reclaim barriers and MMIO doorbell
   ordering against Linux `stmmac` and the RISC-V memory model;
2. add bounded descriptor-ring stress tests with delayed/out-of-order device
   visibility;
3. encode the pure ownership/order invariants in the existing executable
   model, and only then consider a Kani proof for finite ring arithmetic;
4. run one recovery-armed Megrez experiment that distinguishes mapping,
   barrier, and device-progress failures.
