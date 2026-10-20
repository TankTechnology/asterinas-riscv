# EIC7700 QEMU fault matrix (2026-10-12)

This is simulation evidence only.  No physical-board MMIO, serial write,
reboot, or reset was performed.

## Changes covered

The companion QEMU model now exposes two bounded GMAC-1 fault switches:

- `eic7700-gmac1-phy-absent=on`: Clause 22 reads return `0xffff`, clear the
  busy bit, and do not start DMA or assert the GMAC IRQ.
- `eic7700-gmac1-irq-drop=on`: status bits are retained while the interrupt
  output is suppressed.

The qtest binary adds:

- `/eic7700/gmac/phy-absent-fail-closed`;
- `/eic7700/gmac/unreachable-descriptor-bounded`.

The latter uses an unmapped TX buffer (`0xdead0000`) and verifies that the
descriptor failure is bounded, the DMA status is observable, and the dropped
IRQ does not escape the model.

## Runtime command

```text
docker exec -e QTEST_QEMU_BINARY=/root/qemu/build/qemu-system-riscv64 \
  qemu-eic7700-builder-20261004 \
  /root/qemu/build/tests/qtest/eic7700-dwc3-test --tap
```

The retained TAP output is `target/qemu-eic7700-qtest-20261012/tap.txt`:

- `1..21`;
- 21 `ok` results;
- no timeout or forced termination.

The matrix now covers clock-gated GMAC-1, asserted reset, MMIO timeout, PHY
absence, unreachable DMA descriptor, IRQ loss, and the existing bounded DWC3
fault cases.  Watchdog recovery remains a host/QEMU session contract rather
than a property of the QTest device process; it is covered separately by
`tools/riscv/qemu_recovery_faults.py` and is not promoted to hardware evidence.

## TCG trace plugin

`tools/riscv/qemu_plugins/eic7700_boot_trace.c` was compiled in the same
container with QEMU's plugin header and glib flags.  The plugin registers
translation-block, vCPU idle, vCPU resume, and exit callbacks and emits only
aggregate counters.  It does not inspect guest memory and does not claim
cycle-accurate or hardware timing.  A QEMU launch loaded the plugin and
emitted `ASTERINAS_QEMU_TCG_TRACE_START`.  The host-side static gate passed;
the clean QMP exit used for this receipt did not produce a retained aggregate
line, so no timing result is claimed from this run.

## Shared contract

`tools/riscv/eic7700_platform_contract.py` is the source of truth for the
verified MMIO base, resource size, IRQ, probe offsets, reset values, DMA
directions, platform dependencies, and fail-closed side effects.  The
Asterinas probe registry and QEMU GMAC source gate consume the same registry.

## Driver access audit

The RISC-V DWMAC probe wraps its `PlatformRegisters` boundary with
`ProbeAudit`. It emits one structured `ASTERINAS_PROBE_ACCESS` marker after
the platform sequence, counting logical reads and writes plus GMAC, MDIO,
DMA, TX, RX, and IRQ categories. The host evidence gate can require this
marker for GMAC probes; it accepts a pass only when the GMAC read count
matches the six-register whitelist and every datapath counter is zero. The
driver ktest also checks the allowed platform reset write and the absence of
GMAC datapath writes.

The host gate `make test_riscv_eic7700_qemu_unit` passed 41 tests, including
the audit parser's missing-marker, read-count, and side-effect rejection
cases. `cargo check -p aster-dwmac --target riscv64imac-unknown-none-elf`
also passed; an additional `RUSTFLAGS="--cfg ktest"` check compiled the
audit ktest path.

## Renode replay adapter

Renode 1.17.0 was run from its portable distribution on 2026-10-13. The
`.repl`/`.resc` scenario now embeds the Python peripheral, so it does not
depend on an installation-specific script search path. The live replay first
cleared the fault register and read `MAC_VERSION=0x1052` and
`DMA_STATUS=0x00000000`; it then selected clock-gated + reset-asserted +
IRQ-drop and read `0xffffffff` for both registers. Renode exited with status
zero and produced no timeout.

The standalone runner enumerated all 64 combinations of the six GMAC-1
faults. Every combination ended with watchdog recovery, at most one descriptor
submission and at most one IRQ attempt. These are state-machine and MMIO
adapter results; they do not qualify the physical EIC7700 board.

## 2026-10-13 revalidation

The rebuilt QEMU test binary was rerun in `qemu-eic7700-builder-20261004`
with `--tap`: plan `1..21`, 21 `ok` results, 0 failures, exit status 0.

The TCG plugin was rebuilt from
`tools/riscv/qemu_plugins/eic7700_boot_trace.c`. With
`ASTERINAS_QEMU_TCG_TRACE_OUTPUT` pointing at a temporary report and a
controlled QMP `cont`/`quit` cycle, QEMU exited with status 0 and the report
was read back successfully:

```text
ASTERINAS_QEMU_TCG_TRACE tb=12175768 insns=59941992 idle=4 resume=3
```

The report is an aggregate execution trace only; it does not claim hardware
timing or physical-board behavior.
