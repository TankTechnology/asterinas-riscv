# RISC-V DMA Memory-Type Contract

Date: 2026-08-28

## Result

Non-coherent DMA allocations on RISC-V request `PBMT_NC` through the existing
`CachePolicy::WriteCombining` representation when the CPU implements Svpbmt.
Megrez does not implement that extension: its four CPU nodes advertise
`rv64imafdch_zicsr_zifencei_zba_zbb_sscofpmf`. On this board the same page-table
request is therefore an ordinary cacheable mapping.

`DmaCoherent` callers that require a real uncached CPU view consume the
allocation with `DmaCoherent::into_uncached`. `DmaStream` now applies the same
guarantee automatically to bounce storage when a non-coherent device cannot
use architectural cache maintenance. Both paths retain PBMT_NC when Svpbmt is
present; otherwise they clean the original EIC7700 DRAM range and retain the
SoC's non-cacheable System Port alias. A RISC-V platform with none of Svpbmt,
Zicbom, or a supported platform alias fails closed. MMIO mappings continue to
request `PBMT_IO` through `CachePolicy::Uncacheable`.

This establishes the descriptor and packet-buffer memory-type contracts. It is
not yet physical evidence that the latest Megrez sustained-transfer failure is
fixed.

## Specification authority

The ratified RISC-V Svpbmt 1.0 specification defines:

- `PBMT_NC`: non-cacheable, idempotent, weakly ordered main memory;
- `PBMT_IO`: non-cacheable, non-idempotent, strongly ordered I/O.

Source: <https://docs.riscv.org/reference/isa/v20240411/priv/svpbmt.html>

The Linux DMA guide lists network-card descriptor rings as coherent DMA memory
and explicitly states that coherent DMA memory still requires appropriate
memory barriers when publishing descriptor fields to a device.

Source: <https://kernel.org/doc/html/next/core-api/dma-api-howto.html>

The EIC7700X TRM Part 1, Table 3-39 documents D0 DRAM
`0xc0_0000_0000..0xdf_ffff_ffff` as non-coherent System Port memory. The
Asterinas range `0xc0_0000_0000..0xc4_0000_0000` is deliberately only the
checked 16-GiB subset corresponding to the current board allocator, not the
full 128-GiB SoC window. TRM pages 295 and 299-301 document the L3 `Flush64`
write-back/invalidate mechanism used before switching CPU views.

Primary source: [EIC7700X TRM release
`v1.0.0-20250103`](https://github.com/eswincomputing/EIC7700X-SoC-Technical-Reference-Manual/releases/tag/v1.0.0-20250103),
Part 1 SHA-256
`f1d7adef279fae2c83cca7c27e31226180bdfbf01c42384c01bf6d369e195361`.

Pinned ESWIN Linux commit
[`fc6038c00e006226e3bd504d2679c534eabf5503`](https://github.com/eswincomputing/linux-stable/tree/fc6038c00e006226e3bd504d2679c534eabf5503)
independently implements the same distinction: streaming DMA performs cache
maintenance, while `arch_dma_set_uncached` converts D0 Memory Port addresses
to the D0 System Port before mapping the CPU view uncached.

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

The Megrez network packet-buffer path follows a second, direction-aware path:

1. `kernel/comps/network/src/dma_pool.rs` maps each page through
   `DmaStream::map(..., false)`.
2. Without Zicbom, `DmaStream` allocates bounce storage for the device-facing
   bytes.
3. Without Svpbmt, OSTD attaches the same checked EIC7700 System Port alias to
   that bounce storage before preparing the DMA mapping.
4. `sync_from_device` copies from the alias into the packet segment, while
   `sync_to_device` copies from the segment into the alias. Direct readers and
   writers for allocated streams also use the alias.
5. Splitting a stream splits the alias while preserving backing physical and
   device addresses.

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
- non-coherent streaming DMA without cache maintenance requires a guaranteed
  uncached CPU view, and stream splitting preserves that view's identity.

The existing host DWMAC model gate also remained green.

The streaming-DMA correction followed a separate RED/GREEN cycle. The first
pinned RISC-V ktest compile exited 101 because `DmaStream` had neither a
guaranteed-view selector nor retained alias identity:

```text
error[E0432]: no `requires_uncached_alias` in `mm::dma::dma_stream`
error[E0599]: no method named `uncached_alias_paddr` found for `DmaStream`
```

After the minimal alias ownership and access routing were implemented, the
same `cargo osdk check --ktests -p ostd` command exited zero. The broader final
gates also exited zero:

- RISC-V ktest compilation for `ostd` and `aster-kernel`;
- x86-64 ktest compilation for `ostd`, preserving the non-RISC-V path;
- `-Dwarnings` RISC-V Clippy for `aster-network` and `aster-dwmac`;
- 18 DWMAC model/source tests and 22 Megrez GMAC contract/gate tests.

A filtered QEMU runtime attempt built the test kernel but did not enter it:
OSDK resolved the root manifest's relative test-disk paths from its generated
base-crate directory. Three bounded fixture attempts were stopped without
changing code or disk images. Runtime QEMU success is therefore not claimed by
this milestone; the compile, model, and static contracts above are the
available preboard evidence.

## Document-driven preboard address contract

The 2026-08-28 hardware-source audit narrowed the address relationship that a
physical run is allowed to assume. The shipped Megrez DT marks both GMACs
`dma-noncoherent` and activates neither `iommus` nor `dma-ranges`. Identity
`paddr == daddr` is therefore valid for this frozen configuration, but is not a
universal EIC7700 guarantee.

Commit `921a84458` makes both the offline DT inspector and the in-kernel exact
Megrez parser reject either translation property before MMIO programming or
DMA allocation. A future translated DT now fails closed until the DWMAC driver
implements its address domain deliberately.

Commit `81608c626` adds a read-only `DmaCoherent::uncached_alias_paddr`
diagnostic. The queue initialization snapshot now distinguishes:

- the descriptor backing physical address (`ring_paddr`);
- the address programmed into DWMAC (`ring_daddr`);
- the optional EIC7700 CPU uncached alias (`ring_cpu_alias`).

The one-shot `ASTERINAS_GMAC_DMA_CONTRACT` marker also includes DWMAC revision,
TX/RX descriptor bases, and initial tails. It does not change address
selection, cache policy, or queue state.

Fresh preboard verification at implementation HEAD `81608c626` produced:

```text
Megrez host contracts: 34 passed
DWMAC host/model contracts: 14 passed
RISC-V cargo osdk check --ktests: exit 0 in 18.72s
```

The RISC-V compile included `ostd`, `aster-dwmac`, `aster-network`, and
`aster-kernel`. Existing kernel warnings remained non-fatal. No QEMU or physical
board run was performed, so this section records a software contract rather
than hardware success.

## Physical motivation and correction

An earlier Megrez run completed the first 16 KiB transfer, then timed out with:

```text
tx_submitted=64 tx_reclaimed=0 tx_outstanding=64
```

RX still reached 94 packets and the recovery path returned the board to
U-Boot without a physical reset. A later ordering-instrumented run reclaimed
only two descriptors, then filled the ring while RX continued to 153 packets.
The host had already received frames described by later entries while the CPU
still read the oldest entry as DMA-owned. That evidence, combined with the
exact no-Svpbmt ISA string, makes a stale cacheable CPU view the leading
diagnosis rather than a missing PBMT_NC encoding on this hardware. It does not
prove that diagnosis until a post-alias board run observes ownership progress
or a more specific fault.

## Remaining assumptions and non-goals

- The DWMAC device and CPU agree on the descriptor ring's physical address.
- The checked EIC7700 System Port subset has the non-cacheable semantics
  documented in the pinned TRM and mirrored by pinned vendor Linux.
- This change retains the existing descriptor publication, reclaim, and MMIO
  ordering barriers; it does not redesign them.
- This change does not model the EIC7700 cache hierarchy or DWMAC in QEMU.
- This change does not claim byte-level cache coherence on hardware.
- No QEMU or board run is part of this milestone.

Later recovery-armed evidence proved the descriptor alias and TX reclaim were
healthy, but the 16-MiB receive stage still accumulated retransmissions after
RX-buffer reuse. The board advertises neither Zicbom nor Svpbmt, while the old
`DmaStream` bounce path treated its `WriteCombining` KVA as uncached and copied
from it without invalidation. The static contract therefore admitted stale
payload bytes even though MAC, MTL, descriptor, and TX counters remained clean.

The next milestone is one separately frozen, recovery-armed physical run after
the streaming-DMA correction passes simulation and compile gates. It should
repeat the existing ordered 16-KiB through 16-MiB transfer without adding more
diagnostic branches. No run is part of this implementation milestone.
