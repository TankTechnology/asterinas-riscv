# Megrez fbdev desktop safe-loader incident and correction — 2026-10-15

## Physical attempt

The guarded run was started only after the known `desktop-m4` QEMU receipt,
the framebuffer installation receipt, and a fresh U-Boot prompt were checked.
It used one serial owner, the hardware watchdog, and the previously published
desktop artifact pair. The receipt is:

`target/megrez-physical-step1/desktop-m4-watchdog-handoff-20261015-r3/`

The run did not pass. The serial transcript reached:

```text
Enter riscv_boot
ASTERINAS_HARDWARE_WATCHDOG_HANDOFF status=active period_seconds=1
[kernel] rootfs is ready
```

It never reached `ASTERINAS_DEBUG_CONSOLE_READY`, the framebuffer marker, or a
desktop marker. The runner stopped after its bounded startup deadline with
`passed=false` and `recovered=false`; no second boot attempt was made. A
follow-up passive serial read, one Ctrl-C, and one empty line produced no fresh
OpenSBI/U-Boot prompt. The known RockOS address was also not reachable. This
does not distinguish a board hang from a watchdog/firmware recovery failure;
the board must be externally reset before another physical run.

No screenshot is claimed for this attempt: no framebuffer marker was emitted,
and there is no HDMI capture device.

## Root cause found in the host runner

`boot_cycle(..., mode="desktop")` previously sent menu choice `4`. That path
loaded the immutable full DTB, so the guest could register framebuffer, GMAC,
and DWC3 before userspace. Basic already used a direct artifact loader with a
minimal DTB, but desktop did not use the equivalent protected path.

The desktop path now uses `display-safe` direct loading. It preserves the
firmware `simple-framebuffer` node and verifies it remains `status="okay"`,
disables both Ethernet and both USB wrapper nodes, removes the `/chosen`
`asterinas,usb-host` selector, reads all five statuses back, and only then
executes `booti`. The runner also observes the recovery boundary after a
pre-desktop timeout before closing the serial owner.

## Host verification

The real-DTB fixture and desktop-cycle regression tests pass:

```text
python3 -m unittest -q \
  tools.riscv.tests.test_megrez_boot_menu \
  tools.riscv.tests.test_megrez_display_gate \
  tools.riscv.tests.test_megrez_control_chain \
  tools.riscv.tests.test_qemu_uboot_contracts
Ran 116 tests ... OK
```

The run included `dtc`/`fdtput`/`fdtget` readback, Python compilation, and
`git diff --check`. No commit or push was made.

The next physical attempt is forbidden until the board is externally reset
and a fresh U-Boot prompt is observed. It must use the corrected display-safe
loader and a new output directory; the failed r3 receipt remains immutable.
