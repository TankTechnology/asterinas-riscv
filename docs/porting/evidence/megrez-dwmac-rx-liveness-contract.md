# Megrez DWMAC RX Liveness Contract

Date: 2026-08-28

## Source identity

The hardware-facing authority for this audit is ESWIN's Linux 6.6 branch at
commit `fc6038c00e006226e3bd504d2679c534eabf5503`. The branch identity was read
from `refs/heads/linux-6.6.18-EIC7X`; the audit used raw files addressed by the
commit rather than the mutable ref.

The inspected primary sources are:

- [`stmmac_main.c`](https://github.com/eswincomputing/linux-stable/blob/fc6038c00e006226e3bd504d2679c534eabf5503/drivers/net/ethernet/stmicro/stmmac/stmmac_main.c),
  SHA-256 `bed987608cdb21b1c48dfdfd454b11781cf6ce2ec9a3a47e266f1a13c64b127e`;
- [`dwmac4_lib.c`](https://github.com/eswincomputing/linux-stable/blob/fc6038c00e006226e3bd504d2679c534eabf5503/drivers/net/ethernet/stmicro/stmmac/dwmac4_lib.c),
  SHA-256 `2543cf88e08e3798f31810b8104c71a1d6d8730776c6a07c8dc4860835f0e72b`;
- [`dwmac4_dma.c`](https://github.com/eswincomputing/linux-stable/blob/fc6038c00e006226e3bd504d2679c534eabf5503/drivers/net/ethernet/stmicro/stmmac/dwmac4_dma.c),
  SHA-256 `60e2b0bc9dd46e1df80fb886c47a02a0915611d4ec634435cd4cc219d76e0b0c`;
- [`dwmac4_dma.h`](https://github.com/eswincomputing/linux-stable/blob/fc6038c00e006226e3bd504d2679c534eabf5503/drivers/net/ethernet/stmicro/stmmac/dwmac4_dma.h),
  SHA-256 `0a9e02abe4851da783f188bf95877ed480830246f744599219e4977801586ea6`;
- [`dwmac4_descs.c`](https://github.com/eswincomputing/linux-stable/blob/fc6038c00e006226e3bd504d2679c534eabf5503/drivers/net/ethernet/stmicro/stmmac/dwmac4_descs.c),
  SHA-256 `557b8e9defa482166a2eeb2767a7ae5dc405d978cc2a080e036b2a3855dd0f3e`;
- [`dwmac-win2030.c`](https://github.com/eswincomputing/linux-stable/blob/fc6038c00e006226e3bd504d2679c534eabf5503/drivers/net/ethernet/stmicro/stmmac/dwmac-win2030.c),
  SHA-256 `6abf28f712ddcd9a4d9eb7a108033c76e8079de9dc79c3529575c2968d835f34`.

`dwmac-win2030.c::dwc_eth_dwmac_probe` supplies EIC7700 resources and platform
configuration, then delegates the operational datapath to
`stmmac_dvr_probe`. It does not replace the generic RX polling, descriptor, or
DMA interrupt protocol. The generic ESWIN-branch `stmmac` sources are therefore
the applicable software reference for this board.

## Verified RX budget and NAPI completion rules

`stmmac_main.c::stmmac_rx` receives a `limit` from NAPI, caps it at one less
than the RX ring size, and walks descriptors only while the completed-frame
count remains below that limit. It returns the bounded count after refilling
the descriptors consumed by that invocation.

`stmmac_main.c::stmmac_napi_poll_rx` passes the NAPI `budget` to `stmmac_rx`.
It calls `napi_complete_done` and reenables the channel's RX DMA interrupts
only when `work_done < budget`. Reaching the budget returns without completing
NAPI and without reenabling RX DMA interrupts, so the already scheduled poll
continues while the hardware source stays masked. The combined RX/TX poll in
`stmmac_napi_poll_rxtx` applies the same rule: either direction reaching the
budget keeps polling and does not reenable DMA interrupts.

This is the authoritative working pattern for the Asterinas model:

- one RX callback must have finite work;
- budget exhaustion is not an empty-queue observation;
- budget exhaustion preserves scheduled RX work and keeps the interrupt
  source masked;
- the source is reenabled only after a poll observes less work than its
  budget and completes the deferred operation.

## Verified descriptor ownership and tail-pointer rules

`dwmac4_descs.c::dwmac4_set_rx_owner` publishes an RX descriptor by setting
the DWMAC ownership and valid-buffer bits. `stmmac_main.c::stmmac_rx_refill`
sets buffer addresses, executes `dma_wmb`, publishes ownership, advances
`dirty_rx`, then updates the RX tail. The ordering contract is therefore:
descriptor contents and buffers become visible before ownership, and ownership
becomes visible before the tail advertises newly refilled work.

After refill, Linux computes the tail as the descriptor-ring base plus
`dirty_rx * sizeof(struct dma_desc)`. `dirty_rx` is the next ring position
after the descriptors returned to DMA. At initial channel setup, the tail is
the base plus the number of allocated descriptors, which is the one-past-ring
initial boundary. `dwmac4_lib.c::dwmac4_set_rx_tail_ptr` writes that byte
address directly to `DMA_CHAN_RX_END_ADDR`.

Asterinas currently initializes the tail one-past-ring and advances it to the
next descriptor position after every refill. That shape agrees with the Linux
reference, including wrap to the ring base. The audit does not identify the
tail index calculation as the first root-cause candidate. The production-fix
plan must still preserve sync-to-device before the ownership/tail writes.

## Verified TX publication and completion rules

`stmmac_main.c::stmmac_xmit` reserves the ring entries, maps and fills every
buffer, prepares the descriptors, and advances `cur_tx` to the next free
entry. It publishes the first descriptor's ownership last through
`dwmac4_descs.c::dwmac4_set_tx_owner`, then calls
`stmmac_flush_tx_descriptors`. The latter executes `wmb()` before computing
the byte-address tail from `cur_tx` and calling
`dwmac4_lib.c::dwmac4_set_tx_tail_ptr`. That function writes the tail directly
to `DMA_CHAN_TX_END_ADDR`. Thus Linux makes the payload and complete descriptor
chain visible before `OWN`, and makes `OWN` visible before the MMIO tail
notification.

`stmmac_main.c::stmmac_tx_clean` walks from `dirty_tx` toward `cur_tx` until it
encounters a descriptor still owned by DMA. After observing cleared ownership
it executes `dma_rmb()` before reading completion fields, unmaps the DMA
buffer, clears the software entry, and advances `dirty_tx`. Both TX NAPI entry
points pass an explicit budget to this function. This provides a bounded
completion loop and prevents a buffer from being reused before the ownership
transition is observed.

Asterinas has the same ring-state shape. `TxBuffer::build` synchronizes the
payload to the device before `DmaQueue::send` publishes its descriptor;
the descriptor ring uses an uncached `DmaCoherent` mapping on noncoherent
targets, so `DmaQueue::write_descriptor` publishes the complete descriptor
without cache-line writeback before `DwmacDevice::send` writes the tail.
Reclaim reads the uncached descriptor, observes cleared ownership with an
acquire fence, then drops the buffer and advances the consumer.

There is nevertheless a documented boundary difference: Linux has an
explicit `wmb()` at the final ownership-to-tail handoff, while Asterinas relies
on the descriptor release fence before a later volatile MMIO write. The staged
board experiment therefore records both TX publication/reclaim progress and
the DMA channel status. The first physical result below selected descriptor
cache-line ownership as a candidate rather than silently adding another MMIO
barrier. The follow-up run tests whether an uncached ring is sufficient.

## Verified DMA status clear and RBU restart rules

`dwmac4_dma.h` defines channel receive interrupt, receive-buffer-unavailable,
normal-summary, and abnormal-summary status bits. In
`dwmac4_lib.c::dwmac4_dma_interrupt`, Linux reads channel status and the enabled
interrupt mask, classifies receive-buffer-unavailable for statistics, and
writes the enabled observed bits back to the status register. This is the
driver's write-one-to-clear operation.

The Linux code does not use a separate RBU-only restart command in this path.
It refills descriptors and writes the new RX tail, while the interrupt handler
clears the observed enabled status. Asterinas likewise must not clear status
and unmask the source while completed descriptors are left without scheduled
poll work. The model represents the ordering as separate clear and rearm
transitions so a DMA completion may occur between them.

Linux masks the DWMAC channel's RX interrupt enable bit when scheduling NAPI.
Asterinas currently masks the mapped PLIC source instead. Those mechanisms are
not identical, but both require the same deferred-work invariant: a source
masked for polling is released only after the receiver has either drained its
work or preserved another scheduled poll.

## Mapping to the Asterinas model

The executable model maps Linux/Asterinas concepts as follows:

| Model state or transition | Driver concept |
|---|---|
| `Owner::Dma` | descriptor has DWMAC `OWN` and may be filled |
| `Owner::CpuComplete` | hardware cleared `OWN`; CPU may consume it |
| `DmaComplete` | frame completion plus asserted RX channel status |
| `DeliverIrq` | deferred mapped IRQ masks the PLIC source and raises softirq |
| `PollConsume` | receive, refill, ownership publication, and tail advance |
| finite `budget_left` | Linux NAPI budget / proposed bounded Asterinas poll |
| `ClearStatus` | write-one-to-clear of known DWMAC channel status |
| `Rearm` | either preserve masked scheduled work or unmask an empty receiver |
| `RaiseTx` / `RaiseTimer` | work becoming pending during nonpreemptible RX poll |

For a two-entry ring, the current unbounded protocol reaches a lasso after:

1. DMA completion;
2. IRQ delivery;
3. TX service;
4. RX poll start;
5. TX and timer work becoming pending.

The repeating cycle is two alternating DMA completions and poll consumes. It
returns to the same state while TX and timer remain pending. The finite-budget
protocol breaks that cycle, keeps the source masked when completed descriptors
remain, and verifies all reachable states for reduced rings of size two,
three, and four.

## Unproved EIC7700 assumptions

The software model and Linux comparison do not prove these hardware facts:

- EIC7700 implements the referenced DWMAC4/5 register semantics without a
  relevant erratum;
- Asterinas cache synchronization matches the board's noncoherent DMA
  requirements;
- the PLIC source has the level behavior represented by the board DTB and OSTD
  mapping;
- OSTD MMIO operations provide the ordering required between descriptor sync,
  status clear, tail update, and interrupt unmask;
- the PHY and GMAC do not require an undocumented EIC7700-specific recovery
  sequence after prolonged RX-buffer-unavailable status.

These assumptions require one final physical run. They are not reasons to use
the board for software scheduler exploration.

## Consequence for the production-fix plan

The evidence supports a narrowly scoped production plan around bounded ingress
polling and explicit poll completion. The plan must distinguish:

- `drained`: fewer packets than the budget; clear status and rearm;
- `budget exhausted`: preserve/re-raise RX work and keep the source masked;
- `fatal`: stop the queue and do not reschedule or rearm it.

It must test TX/timer progress, RX arrival during clear/rearm, ring wrap, and
the exact masked/rescheduled state before changing the real driver. It must not
bundle MMC deployment, xHCI, desktop, browser, PHY-selection, or unrelated
network-stack work. QEMU remains regression evidence only; one Linux-staged
Megrez run is the final hardware check.

## Implemented bounded protocol and staged physical discriminator

The production implementation is recorded by these commits:

- `b6c709f5d`: dependency-free 32-packet poll-budget state machine and exact
  host tests;
- `57504e1bb`: `DwmacDevice` integration, including the post-status descriptor
  recheck, masked rescheduling, and drained-only PLIC rearm;
- `c72469ad9`: cumulative RX-budget diagnostics emitted only after rescheduled
  work subsequently drains and the PLIC rearm succeeds;
- `d4fe72bc0`: one-boot ordered 16 KiB, 64 KiB, 1 MiB, and 16 MiB TCP stress
  protocol with exact progress and a final marker bound to all four sizes.

The current abstract protocol still produces its deterministic starvation
lasso. The bounded protocol exhaustively verifies reduced rings of size two,
three, and four. The exact production poll module reports six host tests, and
the RISC-V OSDK compile of `aster-dwmac`, `aster-network`, and `aster-kernel`
passes in the pinned container.

The physical discriminator records these markers in one Asterinas boot:

- `ASTERINAS_GMAC_TCP_PROBE_PROGRESS`, with the exact stage and cumulative
  completed bytes;
- `ASTERINAS_GMAC_RX_POLL`, with cumulative receives, budget exhaustions,
  reschedules, and successful PLIC rearms;
- the existing panic/oops fatal markers and the automatic U-Boot recovery
  prompt driven by the frozen `asterinas.reboot_after=60` policy.

## One-boot physical result and TX root cause

The frozen `reboot_after=60`, Sv39, SMP=4 plan was executed exactly once on
Megrez. All four harts, MMC, framebuffer, PHY selection, and GMAC1 at
1000-Mbit/s full duplex initialized. The 16-KiB stage stopped after 14,600
payload bytes. The decisive datapath progression was:

```text
tx_submitted=10 tx_reclaimed=0 tx_outstanding=10
tx_submitted=60 tx_reclaimed=0 tx_outstanding=60 dma_status=0x00008444
tx_submitted=64 tx_reclaimed=0 tx_outstanding=64 dma_status=0x00008040
```

RX continued from 4 through 128 descriptors while TX filled the exact
64-entry ring and reclaimed none. Linux `ss` simultaneously observed the host
connection in `FIN-WAIT-1` with 16,446 queued bytes. Channel status
`0x00008444` contains DWMAC `TBU` (Transmit Buffer Unavailable), `ETI`, `RI`,
and the normal summary. This closes the bounded-RX-poll hypothesis for the
observed failure and selects TX descriptor completion visibility.

The reduced cache-line model in `tools/riscv/dwmac_tx_cacheline_model.rs`
provides the matching counterexample. Four packed 16-byte descriptors share
one EIC7700 64-byte cache line. Preparing one descriptor through a streaming
cached mapping can clean the whole line after DMA cleared an adjacent
descriptor's `OWN` bit, writing the stale `OWN=1` value back to memory. Linux
avoids this ownership conflict by keeping the descriptor ring in coherent DMA
memory. Asterinas now uses `DmaCoherent::alloc(1, false)` for the ring, which
maps it uncached on a noncoherent target; payload buffers remain streaming DMA.

The board's 60-second kernel recovery completed without a physical reset: the
serial transcript reached fresh board firmware, OpenSBI, and U-Boot. The first
runner result said `recovery-not-observed` only because U-Boot waited in its
30-second autoboot countdown. The runner now sends one empty line only after a
post-terminal `Hit any key to stop autoboot` marker and continues to require a
fresh prompt. It never issues a second `booti` or persistent U-Boot command.

Evidence from the frozen run is retained under
`target/megrez-debug/dwmac-high-info/board-run-20260828-1344/` with these
SHA-256 identities:

- `serial.log`: `3e93883c9e38e90d17d310ee4aaf0cd806137ebd9f3e48a41e1bdd2b396a4a08`;
- `transport.json`: `b791b727055771e4f2abe3d7404f7f14a797349ad6f8edd49ff35b2fecd935f0`;
- `probe-tcp-info.json`: `485eb427cd07eaf77dc1e1c35275d589939014300c79dc70cec6228f3da243dd`;
- `result.json`: `60422313642cddec0ff5e11bc581d4235e43995a54e16453873fe843ff13e213`.

The uncached-ring fix passed its deterministic interleaving model, all Megrez
host tests, and the pinned RISC-V OSDK compile. The later frozen-plan run below
does not show positive `tx_reclaimed` progress, so this document does not claim
that the fix resolves the hardware failure.

## Frozen uncached-ring physical validation

The follow-up gate used a fresh Sv39, SMP=4 kernel containing commit
`131381300`. The frozen plan SHA-256 is
`c415f249e1802bf5a21522e266518ddebda314915dcdd7720010e414e4a4006d`.
The kernel SHA-256 is
`334b7bf431afd67e55a50dfb95c9e288f8f9c6b1ba551bd5008be05763c040ca`
and its U-Boot CRC32 is `da9266aa`.

Before the board was opened, the exact kernel and probe passed the generic
Sv39/SMP4 QEMU TCP gate and a separate 60-second software-reboot gate.
The physical runner then transferred only the stale kernel and DTB, reused the
matching initramfs cache entry, and issued exactly one `booti`.
The serial transcript contains no `saveenv` or `reset` command.

The board booted all four harts and selected GMAC1 at 1000 Mbit/s full duplex.
The guest completed the first 16-KiB receive stage:

```text
ASTERINAS_GMAC_TCP_PROBE_PROGRESS bytes=16384 completed_bytes=16384 pattern=mod251
```

During the second stage, RX continued while TX completion remained absent:

```text
rx=62  tx_submitted=52 tx_reclaimed=0 tx_outstanding=52 dma_status=0x00004484
rx=94  tx_submitted=64 tx_reclaimed=0 tx_outstanding=64 dma_status=0x0000c4c4
ASTERINAS_GMAC_TCP_PROBE_FAIL reason=receive-poll errno=110 attempts=2 current_bytes=0 completed_bytes=16384
```

The host probe accepted application writes for both the 16-KiB and 64-KiB
bodies. Its retained `TCP_INFO` samples for both connections reached
`bytes_sent=13201`, `bytes_acked=0`, and `unacked=10`.
This is consistent with a TX path that stops making completion progress after
initial traffic, rather than a pre-TX routing or link failure.

The terminal failure was followed by fresh board firmware, OpenSBI, and U-Boot
output. The runner stopped the recovered autoboot countdown and returned the
board to a U-Boot prompt without a physical reset. Its final result is
`passed: false` with reason `guest-failure-recovered:receive-poll`.

The selected classification is **`tx-reclaim-still-stalled`**.

## Document-driven DWMAC 5.20 preboard gate

The EIC7700 TRM identifies both integrated controllers as Synopsys DWMAC 5.20.
Commit `34341f8b8` therefore replaces the previous broad acceptance of any
`0x40..=0x5f` revision with exact `MAC_VERSION.SNPSVER == 0x52` on both ports.
The driver fails closed before PHY selection if either port reports another
revision.

Commit `5359f2d92` extends the dependency-free Rust reference model from five to
nine tests. In addition to the packed-cache-line and publication-ordering
counterexamples, it now freezes the documented 5.20 normal-descriptor contract:

- full 64-bit buffer addresses occupy words zero and one;
- RX publication sets OWN, IOC, and BUF1V;
- one-buffer TX publication sets OWN, FD, LD, and the exact length;
- 64 entries encode a ring length of 63;
- the initial TX tail is the TX base and initial RX tail is one-past-ring;
- later tails advance modulo the ring;
- status acknowledgement writes only known W1C bits and RBU requests RX resume.

The model remains an independently readable oracle rather than a second
driver. Existing source-contract tests bind its ordering and address/tail
expectations to the production descriptor, queue, register, and device files.

The combined fresh host gate reported 34 Megrez contract tests and 14 DWMAC
model/source tests passed. The pinned RISC-V
`cargo osdk check --ktests -p ostd -p aster-dwmac -p aster-network -p aster-kernel`
completed successfully in 18.72 seconds. This gate did not boot QEMU or Megrez.
The next board transaction must first capture
`ASTERINAS_GMAC_DMA_CONTRACT`; only then may the existing TX-reclaim/RX-progress
evidence be interpreted against the verified physical, device, and CPU-alias
addresses.
Switching the descriptor ring to `DmaCoherent` did not make TX ownership
transitions visible to the CPU on this board. The cache-line stale-writeback
interleaving remains a valid model defect, but it is not a sufficient
explanation for this physical failure. The next investigation must remain
offline until it can discriminate descriptor-format/readback, DMA address and
direction semantics, and the ownership-to-tail ordering boundary. This gate
does not authorize a second physical run.

Evidence is retained under
`target/megrez-debug/dwmac-tx-reclaim-validation/board-run-20260828-153054/`
with these SHA-256 identities:

- `serial.log`: `48480fbe0b2a5796276dc73ab7413f973e99cef0c904fe1c95a16eaa6d3b622a`;
- `transport.json`: `e68fe4d90ce9adbb924aa31ed743886afb4efef59ce89b526661198b7d404cf7`;
- `probe-tcp-info.json`: `e2d3ddf20e970b1900f3837fcfdf4d780039c7c931cf9646eedb177ee8369571`;
- `result.json`: `13c444d2257b0bf37b1ff7e33fc47b478890a2955bbe1fc5c6cc9d0552a3969d`.

## Current-main DMA-domain and TX-reclaim validation

The later `asterinas-riscv` main integration at commit
`f61a8352e0c0dfbbc8a0c721d78857d4206154e1` was rebuilt as Sv39 with four
harts and validated with one physical `booti`. The frozen plan SHA-256 is
`4b373462a071670ec92a5520185a46e9a8ee4e997f775d2769c8ee8eaa3ee1ee`;
the kernel SHA-256 is
`350e6f13c6ebc18fcbe163385f3b1ae608846fbb23584b5014c3f04e4eece93b`.
Before the board was opened, that exact kernel and probe passed both the
generic Sv39/SMP4 TCP QEMU gate and the independent 60-second software-reboot
gate.

The new one-shot DMA contract established the physical address-domain facts
before datapath counters were interpreted:

```text
ASTERINAS_GMAC_DMA_CONTRACT version=0x52
ring_paddr=0x00000002a0832000
ring_daddr=0x00000002a0832000
ring_cpu_alias=Some(0x000000c220832000)
tx_ring=0x00000002a0832000 rx_ring=0x00000002a0832400
tx_tail=0x00000002a0832000 rx_tail=0x00000002a0832800
```

Thus the shipped identity-DMA device tree, hardware DMA address, and PBMT
non-cacheable CPU alias agree. The board selected GMAC1 with DWMAC revision
`0x52`, 1000-Mbit/s full duplex, and MAC address `00:48:54:71:00:48`.

Unlike the earlier runs, TX ownership made sustained forward progress. The
bounded diagnostics advanced from `tx_submitted=2 tx_reclaimed=1` to:

```text
rx=128 tx_submitted=473 tx_reclaimed=472 tx_outstanding=1
rx_reschedules=2 plic_rearms=419 dma_status=0x00008444
```

This closes the previous TX-reclaim failure on the current physical build: the
ring neither filled to 64 entries nor stopped reclaiming. The host also learned
the guest MAC in its ARP neighbour table. Nevertheless, the ordered probe
reported:

```text
ASTERINAS_GMAC_TCP_PROBE_FAIL reason=connect-poll errno=110 attempts=11 current_bytes=0 completed_bytes=0
```

The bound host trace contains zero accepted TCP connections. A historical
packet capture from an earlier build shows repeated board-to-host TCP packets
and immediate host replies, but that capture does not encode TCP flags and is
not evidence from this exact run. Therefore this run is classified
**`tx-reclaim-fixed/later-network-stall`**, with the first unresolved boundary
between receipt of the host's TCP response and TCP socket state-machine
acceptance. The current evidence does not distinguish a DWMAC RX
descriptor/error-classification problem from a packet that reaches the network
stack but fails socket matching; a later run must not choose between those
without a read-only packet-class diagnostic prepared and tested offline.

The terminal failure was followed by fresh board firmware, OpenSBI, U-Boot,
and a new prompt. The 60-second recovery required no physical reset. The
transport log contains exactly one `booti` and no `saveenv` or `reset` command.
Evidence is retained under
`target/megrez-debug/dwmac-board-f61a8352e/board-run-20260828-191525/` with
these SHA-256 identities:

- `serial.log`: `a7f0308b15882bc22b0933ba854ebf48671ec8ec9171a4f08209d656e21b5e68`;
- `transport.json`: `4a886a2b702ddce87cd0a299131f08c54fa0650e5eb6ce4631d4be42c227b83d`;
- `probe-tcp-info.json`: `fcf3ca88e92426bb5e3007dc57187c9b4224b67f1d22af05315dcf1003bd47b3`;
- `result.json`: `acad4aa8f0cded228f774aafcf40bde4f2dff2d00f076dddaa894ba82a3b0de8`.

### Next-run receive boundary discriminator

Before another physical boot, the next kernel must add two bounded, read-only
diagnostics. `ASTERINAS_GMAC_RX_CLASS` samples at most the first 512 receive
headers and reports ARP, IPv4, TCP SYN, TCP SYN-ACK, malformed-frame, and
descriptor-error counters. It copies no payload beyond the Ethernet, maximum
IPv4, and minimum TCP headers, does not change descriptor ownership, and stops
sampling after the fixed bound. `ASTERINAS_TCP_SYN_ACK` advances monotonically
through `parsed`, `connection-found`, and `socket-accepted`, so retransmissions
cannot flood the log or move the observation backwards.

The next single run is interpreted without guesswork:

- descriptor errors with no DWMAC SYN-ACK identify the hardware/descriptor
  receive boundary;
- a DWMAC SYN-ACK without `parsed` identifies Ethernet/IP/TCP validation or
  checksum rejection;
- `parsed` without `connection-found` identifies tuple lookup;
- `connection-found` without `socket-accepted` identifies socket-state
  rejection;
- `socket-accepted` with a userspace timeout moves the fault to connect wakeup
  or a later userspace boundary.

## Ordering-instrumented run and memory-type discriminator

A later single-boot run used the frozen kernel SHA-256
`8560f935cdaaec923c6acb0dcfd5659ca084327748feae20092c4d0f26fb1e39`.
It completed the 16-KiB stage and made limited reclaim progress before the
64-KiB stage filled the ring:

```text
tx_submitted=3  tx_reclaimed=2 tx_outstanding=1
tx_submitted=11 tx_reclaimed=2 tx_outstanding=9
tx_submitted=66 tx_reclaimed=2 tx_outstanding=64 rx=42  dma_status=0x00008444
tx_submitted=66 tx_reclaimed=2 tx_outstanding=64 rx=153 dma_status=0x00000000
```

The host accepted the 64-KiB response and observed retransmission and a
congestion-window collapse before the guest stalled. Hardware therefore
transmitted descriptors beyond the first two while the CPU continued to see
the oldest outstanding ownership word as set. The board recovered through the
60-second software reboot and returned to U-Boot without a physical reset.

The EIC7700X TRM MCPU feature table, exact board DTB, and boot log contain no
Svpbmt extension. Therefore the ring's `WriteCombining` page property could not
encode PBMT_NC and remained an ordinary cacheable PTE. The selected diagnostic
hypothesis is **`tx-reclaim-partial/stale-cpu-view`**. This supersedes the
earlier assumption that allocating `DmaCoherent(false)` alone made the ring
uncached on Megrez, but it remains a leading diagnosis rather than a proven
physical root cause until the separately authorized post-alias run.

The production fix makes DWMAC explicitly consume
`DmaCoherent::into_uncached`. Svpbmt systems retain the page-based PBMT_NC
path; EIC7700 maps the checked hardware non-cacheable DRAM alias after cleaning
the original view; unsupported RISC-V systems fail closed. Packet buffers,
ring layout, interrupt policy, and tail-pointer protocol are unchanged.

Evidence from the ordering run is retained under
`target/megrez-debug/dwmac-ordering-20260828/board-run-ordering-single/`:

- `serial.log`: `cccdc52be7b3ba392179414146532336c78af1c4f1b35e924a33995ce5d6025b`;
- `transport.json`: `6764cf93f35c09d101529619fedf023a2a2f1291b14227866162a5574a3c2bfb`;
- `probe-tcp-info.json`: `34350c9e6ddb944ce0d597d71be9d4c5314cdb34a405a3ddbb385a0b321aeadd`;
- `result.json`: `532c3d25e49241636d88c69bd48b3a282af5c4c34cb760a75c9de96982aeab54`.

No physical rerun is included in the alias implementation milestone. Its next
board use must be separately frozen and recovery-armed.
