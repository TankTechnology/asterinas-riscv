# RISC-V DMA Memory-Type Contract

Date: 2026-08-28

## Result

Non-coherent DMA allocations on RISC-V request `PBMT_NC` through the existing
`CachePolicy::WriteCombining` representation when the CPU implements Svpbmt.
Megrez does not implement that extension: its four CPU nodes advertise
`rv64imafdch_zicsr_zifencei_zba_zbb_sscofpmf`. On this board the same page-table
request is therefore an ordinary cacheable mapping.

Callers that require a real uncached CPU view now consume the allocation with
`DmaCoherent::into_uncached`. The operation retains PBMT_NC when Svpbmt is
present, otherwise it cleans the original EIC7700 DRAM range and retains the
SoC's non-cacheable System Port alias. A RISC-V platform with neither mechanism
fails closed. MMIO mappings continue to request `PBMT_IO` through
`CachePolicy::Uncacheable`.

This establishes the memory-type contract. It is not yet physical evidence
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

1. `kernel/comps/dwmac/src/queue.rs` calls `DmaCoherent::alloc(1, false)` and
   immediately consumes it with `DmaCoherent::into_uncached`.
2. `ostd/src/mm/dma/dma_coherent.rs` calls `alloc_kva` for a non-coherent
   device.
3. `ostd/src/mm/dma/util.rs` now selects the normal non-cacheable DMA policy.
4. `ostd/src/arch/riscv/mm/mod.rs` either retains the PBMT_NC mapping or asks
   the EIC7700 backend for the checked
   `0xc0_0000_0000..0xc4_0000_0000` non-cacheable DRAM alias.
5. `DmaCoherent` retains that alias for the lifetime of the backing frames and
   routes all safe CPU reads and writes through it; its physical and device
   addresses remain those of the original DRAM.

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
- Svpbmt, EIC7700 alias, and fail-closed strategy selection are distinct;
- the exact observed ring range `0x2_a082_a000..0x2_a082_b000` maps to
  `0xc2_2082_a000..0xc2_2082_b000` with checked DRAM boundaries;
- coherent allocations reject `into_uncached`, while non-coherent conversion
  preserves size, physical address, device address, split behavior, and safe
  reader/writer access.

The existing host DWMAC model gate also remained green: 12 tests passed in
0.685 seconds.

## Physical motivation and correction

The preceding Megrez run completed the first 16 KiB transfer, then timed out
with:

```text
tx_submitted=64 tx_reclaimed=0 tx_outstanding=64
```

RX still reached 94 packets and the recovery path returned the board to
U-Boot without a physical reset. A later ordering-instrumented run reclaimed
only two descriptors, then filled the ring while RX continued to 153 packets.
The host had already received frames described by later entries while the CPU
still read the oldest entry as DMA-owned. That evidence, combined with the
exact no-Svpbmt ISA string, identifies a stale cacheable CPU view of the
descriptor ring rather than a missing PBMT_NC encoding on this hardware.

## Remaining assumptions and non-goals

- The DWMAC device and CPU agree on the descriptor ring's physical address.
- The EIC7700 System Port alias has the non-cacheable semantics documented by
  the platform and already used by the USB DMA backend.
- This change retains the existing descriptor publication, reclaim, and MMIO
  ordering barriers; it does not redesign them.
- This change does not model the EIC7700 cache hierarchy or DWMAC in QEMU.
- This change does not claim byte-level cache coherence on hardware.
- No QEMU or board run is part of this milestone.

The next milestone is one separately frozen, recovery-armed physical run. It
must first prove the built ring uses the alias path, then require progress
beyond 64 KiB before extending the probe. No run is part of this implementation
milestone.
