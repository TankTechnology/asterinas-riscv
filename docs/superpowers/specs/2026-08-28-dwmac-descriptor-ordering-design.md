# DWMAC Descriptor Ordering Design

Date: 2026-08-28

## Goal

Make the DWMAC queue's CPU/device handoff explicit and correct on RISC-V:
descriptor body before ownership, ownership before the MMIO tail notification,
and device ownership release before CPU completion reads.

## Root cause

The current fences are attached to a stack-local `Descriptor`. `publish_tx`
and `publish_rx` write the local body, execute a Release fence, and set local
OWN. Only after the method returns does `DmaQueue::write_descriptor` copy the
whole 16-byte value into DMA memory. The fence therefore precedes every real
descriptor-ring store and cannot order body stores before the OWN store.

`DwmacDevice` then writes the TX or RX tail MMIO register without a device
barrier after the descriptor-ring update. On RISC-V, a Rust atomic fence is a
memory-ordering primitive and is not the same contract as Linux's device-aware
`wmb()` (`fence ow,ow`). Asterinas already exposes the conservative
`fence iorw,iorw` through `ostd::arch::device::io_mem::fence`; DWMAC can use it
without unsafe code.

Completion has the inverse problem. `read_descriptor` copies all four words
before `reclaim_completed_tx` or `take_completed_rx` tests OWN. The later
Acquire fence cannot order a body read that already happened. Linux first
observes cleared ownership, executes `dma_rmb()`, and only then consumes the
completion fields.

Primary references:

- pinned ESWIN Linux `stmmac_main.c` at
  `fc6038c00e006226e3bd504d2679c534eabf5503`;
- upstream Linux `stmmac_flush_tx_descriptors`, which executes `wmb()` before
  the TX tail write;
- upstream Linux RISC-V `barrier.h`, where device-aware `wmb()` is
  `fence ow,ow` and the full barrier is `fence iorw,iorw`;
- Linux DMA guide, which requires barriers even for coherent descriptor rings.

## Alternatives

### A. One full fence before each tail

This fixes only the final ring-to-MMIO ordering. It leaves the local-descriptor
Release fence disconnected from DMA-memory stores and leaves completion reads
ordered incorrectly. It is insufficient.

### B. Staged descriptor I/O with DWMAC-local barrier wrappers (chosen)

Write words 0-2 to the DMA ring, execute the platform DMA write barrier, then
write word 3 containing OWN. Before every tail-pointer MMIO write, execute the
write barrier again. On completion, read word 3 first; if OWN is clear, execute
the read barrier and then read words 0-2.

The RISC-V wrappers call OSTD's safe full I/O fence. Non-RISC-V stubs use a
SeqCst fence so the public queue module continues to compile, although the real
DWMAC device is RISC-V-only. This is conservative but small and testable.

### C. New generic OSTD DMA-barrier API

This could eventually provide optimized `dma_wmb`/`dma_rmb` on every
architecture, but it broadens the change beyond the failing device and would
need cross-architecture contracts. It is deferred until another driver needs
the abstraction.

## Production protocol

Publication of a descriptor that grants ownership follows:

1. encode the descriptor locally without fences;
2. write words 0-2 into the uncached DMA ring;
3. execute `dma_write_barrier`;
4. write word 3, including OWN, into the DMA ring;
5. retain the software buffer and counters;
6. execute `dma_write_barrier` before writing the tail MMIO register.

Initialization applies the same final barrier before the initial tail values.
RX refill, RBU resume, and TX submission all apply the tail barrier at their
actual MMIO boundary.

Completion follows:

1. read word 3 from the DMA ring;
2. if OWN is set, return without reading the body;
3. execute `dma_read_barrier`;
4. read words 0-2 and reconstruct the local descriptor;
5. validate completion, clear it, and publish the cleared descriptor through
   the same staged writer.

## Test strategy

The existing host cache-line model gains a store-visibility model proving that
omitting either publication barrier admits an invalid DMA observation, while
the staged protocol admits only complete descriptors. It also models OWN-clear
followed by a completion-body read.

A source contract binds the model to production by requiring the staged body,
barrier, control, and tail calls in order, and by rejecting fences inside the
stack-local descriptor encoder. RISC-V OSDK ktest compilation verifies the
real component and its descriptor tests.

No QEMU or board run is part of this milestone. QEMU lacks the EIC7700 DWMAC
and cache hierarchy, so it cannot validate this ordering. The fix becomes one
input to a later single recovery-armed physical discriminator.

## Non-goals

- no new generic DMA API;
- no change to ring size, tail arithmetic, interrupt handling, PHY, or TCP;
- no cache-maintenance change beyond the completed PBMT_NC milestone;
- no claim that ordering is the only remaining TX-reclaim cause;
- no physical reboot or repeated board experiment.
