# QEMU timer-fault boundary (2026-10-11)

This is a host/QEMU-only experiment. No physical board, serial device, MMIO,
reboot, or network traffic was used.

## What was added

The companion QEMU tree `/home/ubuntu/.codex/qemu-eic7700-10.2.1` has an
uncommitted `virt,timer-fault-disabled=on` property. It keeps the ACLINT/CLINT
MMIO region present, cancels timer callbacks, lowers the timer IRQ, and ignores
future `mtimecmp` scheduling. The change is intentionally fail-closed and is
only a fault-injection aid; it is not a hardware model claim.

## Verification

- QEMU build target `qemu-system-riscv64` completed in
  `qemu-eic7700-builder-20261004`.
- The EIC7700 QTest binary completed **19/19** tests, including DWC3 fault
  isolation and GMAC-1 MMIO timeout fail-closed behavior.
- A guest run with `virt,timer-fault-disabled=on` and
  `asterinas.reboot_after=1` did not reach the Asterinas reboot marker. It
  stopped in the U-Boot command plan at `kernel-load` and was terminated by
  the bounded host timeout. This is evidence that disabling the timer at
  machine realization is too early: it perturbs U-Boot itself, so it cannot
  yet classify an Asterinas guest timer failure.

## Consequence

The delayed model is now implemented as `timer-fault-after-boot=on`. It arms
the fault five seconds after QEMU realization, leaves U-Boot and `booti`
available, and then cancels the ACLINT timer callbacks.

An actual Asterinas guest run used `asterinas.reboot_after=8`, reached
`ASTERINAS_REBOOT_TIMER_READY`, and was observed for seven seconds without a
second OpenSBI epoch. The generated receipt is
`target/qemu-guest-faults/timer-disabled-20261011/receipt.json` and reports
`classification=timer-disabled`, `watchdog_state=guest-software-timer-stalled`,
and `qualified=true`. This is still QEMU-only evidence; it says nothing about
the physical board timer.

The parser and related recovery tests pass **38/38** in the combined host
suite. The rebuilt QEMU binary hashes are recorded by the run environment:
`4dc50f779673a782dadd8391e03600f3aaa5dee1d21441ce56067c0d1b8ce84c` for
`qemu-system-riscv64` and
`9721b9e358da876ec66737f55e15c183cecdba42697feaf9680068452ea853ad` for the
EIC7700 QTest binary.
