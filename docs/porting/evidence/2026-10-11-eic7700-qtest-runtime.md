# EIC7700 QEMU runtime gate (2026-10-11)

This record covers one runtime QTest execution of the companion EIC7700 model.
It is simulation evidence only: no development-board MMIO, serial write,
hardware reboot, or physical reset was performed.

## Inputs and provenance

- QEMU source checkout: `/home/ubuntu/.codex/qemu-eic7700-10.2.1`
- QEMU source `HEAD`: `8baab16fe80800bd1d787a79763ec373782d4b4f8`
- QEMU binary SHA-256:
  `c5f6bf2b763bb53df6f91fcb3a636d1db3847268b1a99b8bc7fc704c9ba8e55b`
- QTest binary SHA-256:
  `ee23e4ff5aff9c0e3bdab5f021e403a3b268d3b817cb45d335f473b6e21b33b2`
- Execution container: `qemu-eic7700-builder-20261004`

Command:

```text
docker exec -e QTEST_QEMU_BINARY=/root/qemu/build/qemu-system-riscv64 \
  qemu-eic7700-builder-20261004 \
  /root/qemu/build/tests/qtest/eic7700-dwc3-test --tap
```

## Result

The TAP run reported `1..19` and all 19 tests passed.

The runtime coverage includes:

- DWC3 identity/reset, event DMA and PLIC IRQ behavior;
- DWC3 reset, IRQ-without-event-ring, and register-read fail-closed faults;
- DWC3 bounded bulk loopback;
- GMAC identity/MDIO/reset and descriptor loopback;
- GMAC-1 clock-gated reads/writes failing closed;
- GMAC-1 MMIO-timeout reads returning the unavailable value, with writes,
  MDIO, DMA and IRQ side effects suppressed;
- the six-register read-only probe whitelist;
- the ordered platform sequence, auxiliary readback and reset isolation;
- network attachment and the single-packet model test.

The checked test binary completed without a timeout or forced termination.
The companion host contract suite also passed 32 tests, and
`make test_riscv_uboot_matrix_unit` passed 24 tests in the same workspace.

## Boundary

This run proves the QEMU device model and its qtest contracts. It does not
prove EIC7700 clock/reset/PHY wiring, interconnect behavior, or recovery from
a physically stalled MMIO transaction. The recovery-fault matrix in
`tools/riscv/qemu_recovery_faults.py` remains explicitly scoped to a bounded
host serial peer (`execution_scope=host-serial-peer`); it must not be described
as a guest-kernel or hardware watchdog result. A physical GMAC-1 Probe remains
out of scope for this record.
