# EIC7700 GMAC-1 QEMU bounded-gating repeat

## Scope

This run exercises the focused EIC7700 QEMU model before any new access to the
development board.  It covers the failure-closed clock-gated state and the
platform sequence that makes GMAC-1 readable.  It does not use a network
backend, PHY traffic, or guest DMA outside the bounded descriptor loopback
test.

## Inputs

- QEMU tree: `/home/ubuntu/.codex/qemu-eic7700-10.2.1`
- Branch: `codex/eic7700-gmac-phase3`
- HEAD: `f854e2a tests/qtest: wrap EIC7700 machine arguments`
- Test binary: `build/tests/qtest/eic7700-dwc3-test`
- QEMU binary: `build/qemu-system-riscv64`
- Execution image: `asterinas/asterinas:0.18.0-20260702`
- Every invocation had a 20 second per-test-process timeout.

## Result

The test binary was run three consecutive times in the container.  Each run
reported 9/9 tests passing:

1. DWC3 identity isolation;
2. DWC3 glue reset;
3. DWC3 event DMA/PLIC IRQ;
4. DWC3 IRQ assertion/deassertion;
5. DWC3 bounded bulk loopback;
6. GMAC identity, MDIO and reset;
7. GMAC descriptor loopback;
8. GMAC-1 clock-gated fail-closed reads/writes;
9. GMAC-1 platform sequence enabling the gated port.

No test process exceeded its timeout and no QEMU process remained after a
test completed.

## Safety interpretation

The model proves that the intended software sequence can be exercised without
blocking the QEMU vCPU when GMAC-1 is gated.  It does not prove that a real
EIC7700 bus transaction is recoverable.  The next physical step remains a
RockOS read-only resource check followed by one watchdog-bounded Asterinas
probe, with no PHY, DMA, or packet traffic.

