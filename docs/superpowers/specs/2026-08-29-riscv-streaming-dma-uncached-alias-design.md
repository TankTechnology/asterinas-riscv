# RISC-V Streaming DMA Uncached Alias Design

Date: 2026-08-29

## Goal

Make `DmaStream` bounce buffers correct on non-coherent RISC-V systems that
provide neither Zicbom cache maintenance nor Svpbmt page attributes, while
reusing the EIC7700 uncached DRAM alias already owned by OSTD.

## Decisive evidence

The Megrez CPU advertises neither Zicbom nor Svpbmt. `DmaStream::map` therefore
allocates a bounce `KVirtArea`, and `sync_from_device` copies from that area
without invalidating it because the implementation assumes it is uncached.
However, the RISC-V page-table encoder cannot represent the requested
`WriteCombining` policy without Svpbmt, so that KVA is ordinarily cacheable.

The physical transfer evidence matches repeated stale payload reads: the first
small transfers pass, retransmissions appear after the 64-entry RX ring wraps,
and long transfers collapse while MAC overflow, DMA buffer-unavailable, RX
descriptor-error, and TX-health counters remain clean.

## Chosen design

Retain the existing bounce KVA as the owner of its frames and device address.
Add an optional `IoMem` CPU alias to `DmaStream`, using the same
`create_uncached_dma_alias` helper as `DmaCoherent` when all of the following
are true:

- the device is not cache coherent;
- architecture cache maintenance is unavailable;
- the architecture is RISC-V.

On Svpbmt systems the existing PBMT_NC KVA remains sufficient. On Zicbom
systems the existing direction-aware cache maintenance remains sufficient. On
EIC7700 without either extension, all safe CPU access to the bounce storage and
all bounce copies use the platform alias. Other unsupported RISC-V systems fail
closed during `DmaStream` construction.

The backing physical address, DMA address, direction semantics, and number of
copies do not change. Splitting a stream also splits its alias so that the alias
cannot outlive or cover memory outside its backing object.

## Alternatives

Declaring Svpbmt in the DTB is invalid because the CPU does not implement it.
Adding DWMAC-specific cache operations would duplicate a generic streaming-DMA
responsibility and cannot provide the missing invalidate operation. Replacing
all network packet buffers with `DmaCoherent` would discard the direction-aware
streaming API and broaden the change unnecessarily.

## Failure and safety contract

- unsupported non-coherent RISC-V hardware fails allocation instead of using a
  silently cacheable bounce view;
- checked EIC7700 range translation remains inside the existing architecture
  helper;
- alias construction and all new `unsafe` access remain inside OSTD;
- the safe network component and DWMAC driver are unchanged;
- a physical run is validation after the fix, not part of root-cause discovery.

## Test strategy

Add a RED OSTD ktest that freezes the alias-required policy and stream split
identity. Keep the existing pure EIC7700 path/range tests as the hardware
contract. Compile all OSTD ktests for RISC-V, run the network/DWMAC host tests,
and require warning-free `aster-network`/`aster-dwmac` Clippy. Only after these
gates pass should one recovery-armed board run repeat the 16 MiB transfer.
