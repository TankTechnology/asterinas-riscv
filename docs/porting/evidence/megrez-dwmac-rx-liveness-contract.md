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
