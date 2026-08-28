# RISC-V DMA Memory-Type Contract

Date: 2026-08-28

## Result

Non-coherent DMA allocations on RISC-V now request `PBMT_NC` through the
existing `CachePolicy::WriteCombining` representation. MMIO mappings continue
to request `PBMT_IO` through `CachePolicy::Uncacheable`.

This change establishes the page-table memory-type contract. It is not evidence
that the Megrez DWMAC TX-reclaim failure is fixed.

## Specification authority

The ratified RISC-V Svpbmt 1.0 specification defines:

- `PBMT_NC`: non-cacheable, idempotent, weakly ordered main memory;
- `PBMT_IO`: non-cacheable, non-idempotent, strongly ordered I/O.

Source: <https://docs.riscv.org/reference/isa/v20240411/priv/svpbmt.html>

The Linux DMA guide lists network-card descriptor rings as coherent DMA memory
and explicitly states that coherent DMA memory still requires appropriate
memory barriers when publishing descriptor fields to a device.

Source: <https://kernel.org/doc/html/next/core-api/dma-api-howto.html>

## Asterinas mapping path

The Megrez descriptor ring follows this path:

1. `kernel/comps/dwmac/src/queue.rs` calls
   `DmaCoherent::alloc(1, false)`.
2. `ostd/src/mm/dma/dma_coherent.rs` calls `alloc_kva` for a non-coherent
   device.
3. `ostd/src/mm/dma/util.rs` now selects the normal non-cacheable DMA policy.
4. `ostd/src/arch/riscv/mm/mod.rs` losslessly encodes that policy as
   `PBMT_NC` when Svpbmt is present.

The selector is local to DMA allocations. Existing PLIC, xHCI, PCI BAR, and
other `IoMem` users retain `CachePolicy::Uncacheable`, which continues to
encode as `PBMT_IO`.

Only the RISC-V branch changes. x86-64 and LoongArch retain their previous
non-coherent DMA selection and page-table encodings.

## TDD evidence

The selector test was added before production code. The first pinned RISC-V
ktest compile exited 101 with:

```text
error[E0432]: unresolved import `super::dma_cache_policy`
```

After the minimal selector was implemented, the same command completed in
11.11 seconds with exit 0:

```bash
docker run --rm --network=host \
  -v /home/ubuntu/.rustup:/root/.rustup:ro \
  -v /home/ubuntu/.cargo/bin/cargo-osdk:/root/.cargo/bin/cargo-osdk:ro \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  cargo osdk check --ktests -p ostd \
    --target riscv64imac-unknown-none-elf
```

The focused contracts now require:

- coherent DMA selects `Writeback`;
- non-coherent RISC-V DMA selects the `PBMT_NC` representation;
- `WriteCombining` encodes `PBMT_NC`, clears `PBMT_IO`, and round-trips;
- MMIO `Uncacheable` encodes `PBMT_IO`, clears `PBMT_NC`, and round-trips.

The existing host DWMAC model gate also remained green: 11 tests passed in
0.632 seconds.

## Physical motivation

The preceding Megrez run completed the first 16 KiB transfer, then timed out
with:

```text
tx_submitted=64 tx_reclaimed=0 tx_outstanding=64
```

RX still reached 94 packets and the recovery path returned the board to
U-Boot without a physical reset. This evidence is consistent with a TX
descriptor visibility or completion problem, but it does not distinguish
memory type, ordering barrier, tail-pointer protocol, or hardware behavior.

## Remaining assumptions and non-goals

- The DWMAC device and CPU agree on the descriptor ring's physical address.
- The running RISC-V system advertises and correctly implements Svpbmt.
- `PBMT_NC` provides the intended uncached alias for Megrez DRAM.
- This change does not add descriptor publication or reclaim barriers.
- This change does not order descriptor stores against the MMIO tail write.
- This change does not model the EIC7700 cache hierarchy or DWMAC in QEMU.
- This change does not claim byte-level cache coherence on hardware.
- No QEMU or board run is part of this milestone.

The next milestone must audit descriptor construction, ownership transfer,
MMIO doorbell ordering, and reclaim reads against Linux `stmmac` and the
RISC-V memory model. Its tests should inject delayed and reordered device
visibility before authorizing one recovery-armed physical run.
