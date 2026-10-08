# EIC7700 DWMAC Uncached Alias Design

Date: 2026-08-28

## Goal

Guarantee that the Megrez DWMAC descriptor ring is actually accessed through
uncached memory on hardware that does not implement Svpbmt, while preserving
the existing PBMT_NC path on CPUs that do implement it.

## Decisive evidence

The recovery-armed DWMAC ordering run used the frozen kernel SHA-256
`8560f935cdaaec923c6acb0dcfd5659ca084327748feae20092c4d0f26fb1e39`.
It completed the 16 KiB TCP stage and then reported:

```text
tx_submitted=3  tx_reclaimed=2 tx_outstanding=1
tx_submitted=66 tx_reclaimed=2 tx_outstanding=64
```

RX continued from 42 to 153 packets while the ring remained full. The host
accepted the 64 KiB response but observed loss and retransmission, and the
guest never completed that stage. This proves that the MAC, PHY, ARP, TCP
handshake, initial TX publication, and initial TX completion all work. It also
shows that hardware transmitted frames beyond the first two descriptors while
the CPU continued to observe the oldest outstanding descriptor as DMA-owned.

The four CPU nodes in the exact board DTB advertise
`rv64imafdch_zicsr_zifencei_zba_zbb_sscofpmf`; neither the DTB nor the boot log
advertises Svpbmt. Consequently, `CachePolicy::WriteCombining` cannot encode
PBMT_NC on this board and falls back to an ordinary cacheable PTE. The prior
PBMT_NC change is correct on supporting CPUs but is not active on Megrez.

EIC7700 provides a hardware non-cacheable DRAM alias at
`0xc0_0000_0000..0xc4_0000_0000`. OSTD already uses this alias for non-coherent
USB DMA. The alias is therefore the board-supported path for the DWMAC ring.

## Alternatives

### A. Add `svpbmt` to the board DTB

Rejected. The DTB must describe implemented ISA behavior; declaring an absent
extension would permit reserved PTE bits and could fault or silently corrupt
memory semantics.

### B. Clean and invalidate every descriptor cache line

Rejected for this milestone. EIC7700 exposes a bounded L3 clean interface, but
the current OSTD API does not provide a symmetric, proven CPU invalidation
contract for device-written ownership words. Per-descriptor maintenance also
reintroduces the adjacent-descriptor cache-line race already observed.

### C. Use the EIC7700 non-cacheable physical alias (chosen)

Add one consuming `DmaCoherent` operation that guarantees uncached CPU access.
On RISC-V with Svpbmt, the existing PBMT_NC KVA is retained. On EIC7700 without
Svpbmt, OSTD first cleans the original cached KVA, then creates and retains an
`IoMem` view of the hardware alias. On a RISC-V platform with neither
mechanism, the operation fails closed. Other architectures keep their existing
uncached mapping behavior.

## Architecture and data flow

`DmaCoherent` remains the owner of the allocated DRAM frames and the original
device address. It gains an optional OSTD-owned alias view. Its reader, writer,
and single-word access path select that alias when present; `HasPaddr` and
`HasDaddr` continue to return the original backing/device address. The alias
cannot outlive the backing because both are fields of the same owning object.

The transition is consuming:

```rust
let ring = DmaCoherent::alloc(1, false)?.into_uncached()?;
```

Before installing the alias, OSTD cleans the complete original KVA so zero-fill
or allocator writes cannot later evict over device updates. After the
transition, DWMAC never accesses the cached KVA again. Splitting a DMA object
also splits the alias view, preserving the existing `Split` contract.

The DWMAC queue uses this guaranteed path only for its descriptor page. Packet
buffers retain their current direction-aware pool behavior; changing them is
not required by the observed stale ownership word and would broaden the risk.

## Failure and safety contract

- coherent allocations cannot be converted through this API;
- alias range arithmetic is checked against the exact 16 GiB Die 0 DRAM and
  alias windows;
- non-EIC7700 RISC-V systems without Svpbmt receive an allocation error rather
  than a silently cacheable descriptor ring;
- the safe kernel component does not add `unsafe`; all alias construction and
  lifetime reasoning stays inside OSTD;
- no physical board run is authorized by the implementation itself.

## Test strategy

1. Add pure RISC-V ktests for the three strategy outcomes: PBMT_NC, EIC7700
   alias, and fail-closed.
2. Add exact alias-range boundary tests, including the observed high DRAM ring
   region.
3. Add DmaCoherent tests/source contracts proving the alias view owns all CPU
   reads and writes after conversion and survives split without changing the
   device address.
4. Add a DWMAC source contract requiring `into_uncached()` before descriptor
   initialization.
5. Run only the focused host model and pinned RISC-V OSDK compile gates. Do not
   repeat the physical run in this milestone.

## Non-goals

- no DTB ISA modification;
- no generic cache-maintenance redesign;
- no packet-buffer or TCP change;
- no IRQ, tail-pointer, ring-size, or descriptor-format change;
- no claim of full browser networking until a later separately sealed board
  run completes the 64 KiB, 1 MiB, and 16 MiB stages.
