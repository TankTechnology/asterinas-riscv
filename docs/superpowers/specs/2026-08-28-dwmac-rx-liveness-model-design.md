# DWMAC RX Liveness Model Design

Date: 2026-08-28

## Goal

Determine whether the Megrez sustained-RX hang is caused by Asterinas's
DWMAC/PLIC/softirq protocol before changing the production driver or asking
for another physical reset. The primary deliverable is a host-executable,
bounded state model that produces a minimal counterexample for the current
protocol or establishes the stated invariants within the model bounds.

The work is successful when it can distinguish these software failures:

- an RX poll that never yields under continuous arrivals;
- a PLIC source that remains masked without deferred work able to rearm it;
- a lost RX wakeup between DMA status clearing and PLIC rearming;
- incorrect descriptor ownership or RX tail wraparound;
- TX or timer starvation caused by an unbounded RX softirq.

Only after the model identifies a counterexample may production code change.
One final physical run validates the remaining MMIO, cache-coherency, and
silicon assumptions that cannot be proved by a software model.

## Current evidence

The physical Megrez accepts small HTTP traffic, but a 16 MiB receive stress
stops acknowledging after about 14.6 KiB and loses general kernel liveness.
There is no panic or explicit DMA failure, and the armed 180-second software
restart does not execute. The same failure occurs with both USB host
controllers disabled, so xHCI is not a necessary cause.

The current interrupt protocol masks the level-triggered PLIC source in the
top half, raises RX and TX softirqs, and rearms the PLIC source only from
`notify_poll_end`. The current ingress poll drains packets with an unbounded
`while device.receive()` loop. A schedule in which DMA completes another
descriptor before each empty check can therefore prevent poll completion,
PLIC rearming, and other softirq work. This is the first hypothesis to model;
it is not yet treated as the proven physical root cause.

## Scope

The model covers only queue-zero receive progress and the scheduling boundary
around it:

- a reduced ring of two to four RX descriptors;
- CPU/DMA descriptor ownership;
- RX head and tail positions;
- relevant DMA status bits, including RX interrupt and RX-buffer-unavailable;
- PLIC masked/unmasked state;
- RX, TX, and timer softirq pending/running state;
- poll work count and an optional finite RX budget;
- nondeterministic packet arrival at every scheduling boundary.

The model does not parse Ethernet, IP, or TCP packets. It does not model
Debian, NetSurf, xHCI, PHY negotiation, multiple DMA queues, or the complete
Asterinas scheduler. It does not claim to prove the EIC7700 implementation,
MMIO register specification, cache maintenance, memory barriers, or silicon
errata.

## Chosen approach

Use a small pure-Rust explicit-state model first. Each transition is a pure
function and the checker enumerates all enabled transitions up to a bounded
depth while retaining predecessor links for a minimal counterexample trace.
Small ring sizes are sufficient because the properties depend on ownership,
wraparound, masking, and scheduling rather than the production ring length.

This approach is preferred because it runs with normal host tests, requires no
new verifier installation, represents IRQ and DMA events directly, and gives
a counterexample that can become a regression test. The model remains separate
from MMIO and DMA allocation code; production queue arithmetic is exposed as
small pure helpers only when sharing it avoids a model/implementation mismatch.

Two alternatives are secondary:

- Kani may later prove bounded arithmetic and ownership properties of the pure
  transition helpers, but temporal liveness is awkward to express solely as a
  Kani harness and the repository has no existing Kani workflow.
- TLA+/PlusCal would express fairness and liveness well, but adds a second
  language and a larger translation gap. It is reserved for an ambiguity the
  executable model cannot resolve.

Loom is not selected because the interesting actors are DMA, a level-triggered
PLIC source, and softirqs rather than ordinary Rust threads sharing atomics.

## State and transitions

The immutable model state records:

- descriptor ownership for every reduced-ring slot;
- CPU RX head and the tail value last written to DMA;
- queued packet completions and RX-buffer-unavailable status;
- PLIC mask state and whether its level is asserted;
- pending RX, TX, and timer softirq bits;
- whether RX polling is active and how much budget remains;
- observable progress counters for received packets, poll returns, rearms, TX
  service, and timer service.

The checker nondeterministically applies these transitions:

1. DMA completes the next hardware-owned descriptor and asserts status.
2. The PLIC delivers an asserted unmasked source, masks it, and schedules work.
3. RX softirq starts a poll.
4. Poll consumes and republishes one completed descriptor and updates the tail.
5. Poll observes no work or exhausts its finite budget.
6. Poll end clears known DMA status and rearms the PLIC source.
7. DMA asserts RX-buffer-unavailable between clear and rearm.
8. TX or timer softirq executes when scheduling can reach it.

Every transition is deterministic for a state; nondeterminism comes only from
which enabled transition the checker chooses next. Counterexample output lists
the exact transition sequence and relevant state changes.

## Properties

The checker enforces safety properties for every reachable state:

- every descriptor has exactly one owner;
- CPU consumes only completed CPU-owned descriptors;
- DMA completes only DMA-owned descriptors;
- head and tail always identify positions in or at the contractually permitted
  end of the ring;
- republishing a descriptor occurs before making its tail visible to DMA;
- the number of completed, consumed, and hardware-owned descriptors remains
  consistent with ring capacity;
- no fatal state is silently converted back to an operational state.

Bounded liveness properties are checked over fair schedules:

- one RX softirq invocation returns within a configured maximum number of
  model transitions regardless of packet arrivals;
- after a delivered nonfatal interrupt, the source is rearmed or a new RX
  softirq remains pending within a finite bound;
- pending TX and timer work is serviced within a finite bound even while RX
  packets continue arriving;
- a packet completed while status is cleared or the source is rearmed cannot
  become a lost wakeup.

The current unbounded protocol is expected to fail at least the first and
third liveness properties. A finite-budget candidate must satisfy all
properties without weakening descriptor safety.

## Relationship to production code

The first implementation models the current behavior exactly and must emit a
counterexample before any driver edit. The counterexample becomes a named host
test. A candidate protocol is then changed in the model alone, normally by
bounding RX work and explicitly preserving/rescheduling pending work.

After the candidate passes exhaustive bounded exploration, production changes
are limited to the corresponding scheduling and queue boundary. Production
tests cover ring wrap, budget exhaustion, work remaining at poll end, an RX
arrival during clear/rearm, and the requirement that TX/timer work can run.
The normal RISC-V kernel tests and compile checks remain required, but a QEMU
VirtIO pass is regression evidence only; QEMU does not emulate EIC7700 DWMAC.

## Hardware contract audit

Before selecting a production fix, compare the model's RX-tail, status-clear,
and restart transitions with the pinned ESWIN Linux 6.6 `stmmac` implementation
and Synopsys DWMAC4/5 register definitions already used as M5 authority. Record
whether the RX tail names the last available descriptor or a resume boundary,
which status bits are write-one-to-clear, and the required order for RBU
recovery and interrupt enablement. Any uncertain hardware rule remains an
explicit assumption rather than being hidden in the model.

## Verification and physical acceptance

Host verification has three layers:

1. exhaustive reduced-state exploration with a reproducible minimal trace;
2. production pure-helper and queue regression tests for every proven
   invariant and counterexample;
3. RISC-V OSDK compile/kernel tests plus existing QEMU network regressions.

Only after all three pass is one Megrez run authorized. Linux and RJ45 stage
the frozen artifacts, U-Boot loads them from MMC, and a single Asterinas boot
tests 16 KiB, 64 KiB, 1 MiB, and 16 MiB receive sizes in order. Evidence must
show bounded RX polls, PLIC rearm progress, TX and timer progress, no descriptor
ownership violation, and successful software recovery. The run stops at the
first failed size and publishes counters; it does not request repeated manual
resets or use unrelated driver changes as experiments.

## Deliverables

- one cohesive host model/checker and counterexample trace format;
- tests that demonstrate the current protocol's failure before the fix;
- a hardware-contract note recording verified rules and assumptions;
- the smallest production change justified by the model;
- one physical result bound to exact kernel, DTB, initramfs, and model version.
