# EIC7700 GMAC QEMU Phase 3a: register and descriptor loopback

Date: 2026-10-04

## Scope and artifacts

This slice adds two optional EIC7700 GMAC instances to the RISC-V `virt`
machine when `virt,eic7700-gmac=on` is selected. The devices are mapped at
`0x50400000` and `0x50410000`, use IRQs 61 and 70, expose DWMAC version `0x52`
and the documented FIFO feature encoding, and provide a deterministic Clause
22 PHY at address 0. The QEMU source is in
`/home/ubuntu/.codex/qemu-eic7700-10.2.1` on branch
`codex/eic7700-gmac-phase3` at commit
`f854e2ac3211171b2dc15fbf77d143fd12eb7226` (the tip includes the
engine-start/error-path tests and scope clarification).

The first data-path slice implements one bounded queue-zero normal descriptor
per kick. A TX descriptor owned by DMA is read from guest memory, copied to an
owned RX buffer, and both descriptors are completed with TX/RX status bits.
Tail publication does not run until both DMA channel start bits and MAC RX/TX
enable bits are set. A descriptor without RX ownership reports the RX
buffer-unavailable status. The DMA status and interrupt-enable registers use
write-one-to-clear and level-sensitive IRQ semantics. The implementation is
deliberately bounded to 64 descriptors per MMIO kick so malformed guest rings
cannot hang QEMU.

## Verification

The QEMU build completed in the persistent builder container with Meson 1.7.2:

```text
make -j4 qemu-system-riscv64 tests/qtest/eic7700-dwc3-test
```

The combined qtest executable passed all seven tests, including the new GMAC
identity/MDIO/reset and descriptor-loopback cases:

```text
ok 1 /riscv64/eic7700/dwc3/identity-isolation
ok 2 /riscv64/eic7700/dwc3/glue-system-reset
ok 3 /riscv64/eic7700/dwc3/event-dma-plic-irq
ok 4 /riscv64/eic7700/dwc3/irq-assert-deassert
ok 5 /riscv64/eic7700/dwc3/bulk-loopback-hotplug
ok 6 /riscv64/eic7700/gmac/identity-mdio-reset
ok 7 /riscv64/eic7700/gmac/descriptor-loopback
```

The descriptor test verifies guest-memory TX/RX payload equality for a fixed
96-byte frame, ownership clearing, first/last completion flags, DMA status
bits, engine-start gating, IRQ assertion, status write-one-to-clear deassertion
and RX buffer-unavailable error handling. The
identity test verifies both instances, PHY ID reads, MDIO write/readback and
synchronous software reset isolation.

`git diff --check` passed. QEMU `checkpatch.pl` reported no new diagnostics in
the GMAC model, header or qtest; its one warning on `hw/riscv/virt.c` is an
existing unrelated comment warning.

## Boundaries

This proves the model's register, MDIO, descriptor ownership, guest-memory
DMA, status and IRQ contracts in QEMU. It is not yet a full DWMAC implementation:
multi-descriptor frames, external QEMU network backends, checksum/offload,
PHY timing, cache-coherency behavior, and the complete EIC7700 clock/pinctrl/
reset device-tree glue remain pending. The generated Ethernet nodes are
currently inventory scaffolding and are not evidence that Asterinas' strict
Megrez `dwmac` DT validation or guest network registration succeeds.
