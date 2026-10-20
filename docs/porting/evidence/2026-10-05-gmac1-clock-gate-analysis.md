# GMAC-1 read-only probe failure analysis, 2026-10-05

This is a static and log-only analysis. It does not authorize another board
boot and does not change the probe's read-only contract.

## Observed boundary

The physical `gmac-1` run emitted the fixed resource and device-tree markers
for `0x50410000`, IRQ 70, clocks `[3, 550, 3, 551, 3, 553]`, and reset cells
`[20, 7, 0x02000000]`. It emitted no register-read marker. The watchdog then
returned the board to a fresh OpenSBI/U-Boot epoch. The strict review is kept at
`target/megrez-physical-step1/probe-gmac-1-20261005/strict-validator-review.json`.

This separates the failure from DT parsing and resource validation: the first
MMIO transaction is the first operation that does not return.

## Evidence for a platform-gating cause

The retained RockOS transcript shows the vendor `win2030-eth-dwmac` driver
bringing up `50410000.ethernet` and later reporting User ID `0x10`, Synopsys ID
`0x52`, PHY discovery, and a 1-Gbps link. That proves the physical block can be
read after vendor platform setup, but it does not identify which individual
clock, reset, GPIO, HSP, or RGMII write is necessary.

The Asterinas normal GMAC path contains the corresponding platform sequence in
`kernel/comps/dwmac/src/arch/riscv.rs`: HSP setup, TX-clock gate/divisor setup,
system reset pulse, and the per-port reset GPIO. The read-only probe deliberately
skips all of those writes. GMAC-0 works without them in the saved run; GMAC-1
does not, so the current evidence is consistent with a GMAC-1-specific gate or
reset dependency. It is not proof that any particular write is safe.

The production-side fake-register test `official_platform_sequence_is_exact_and_read_back`
currently checks GMAC-1's 14 writes and every readback, including the `0x02000000`
reset mask. This is a software ordering guarantee; it does not turn the sequence
into an approved physical probe.

The external QEMU model now mirrors that boundary. Its ninth qtest writes the
same HSP/SYSCRG sequence and observes GMAC-1 transition from `0xffffffff` to
version `0x52` only after the complete sequence. GPIO and RGMII writes remain
outside this focused model; they are still prerequisites for any physical
experiment.

## Safe next experiment

Before another physical run, model the platform sequence with fake registers in
QEMU and add assertions for the exact write order and readback. The physical
experiment, if later approved, must be a separate menu entry with one selected
port, a pre-armed watchdog, no DMA/PHY/network traffic, and a single reversible
clock/reset transaction. A missing completion marker remains a failed timeout;
it cannot qualify GMAC-1.
