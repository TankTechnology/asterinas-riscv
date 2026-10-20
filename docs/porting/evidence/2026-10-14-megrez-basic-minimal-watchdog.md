# Megrez minimal Basic watchdog validation (2026-10-14)

## Scope

This was one controlled physical-board attempt using the existing RockOS software
reboot path. The intended profile was UART, timer, MMC, and Basic only. GMAC,
DWC3, and network load were disabled in the volatile U-Boot device-tree edits;
no U-Boot environment, boot entry, or filesystem was modified. The EIC7700
hardware watchdog was armed and read back before boot.

The run used the following manifest artifacts:

- kernel `/asterinas-8a1c81414d78.booti`, CRC32 `b2646eb0`;
- stage1 `/stage1-7806b8348eb1.cpio`, CRC32 `2b5ea4f8`;
- DTB `/megrez-465cb129333c.dtb`, CRC32 `40ed4c65`;
- manifest SHA-256 `1bdcdb791e0dccdfc9508faab30178d78d71ce6e9f8172492e7f23fdb63bacc2`.

## Observed boot

The board reached Asterinas and produced the expected early markers:

- `Enter riscv_boot` and all four harts started;
- UART registration and receive IRQ setup;
- MMC initialization, including SDMA, controller IRQ 81, SDHC RCA, high-speed
  50 MHz timing, and read-only `mmcblk0` registration;
- framebuffer registration (`1920x1080`, stride `7680`, `BgrReserved`);
- `ASTERINAS_SOFTWARE_REBOOT_ARMED seconds=60`.

The intended device isolation was not achieved. Despite the volatile
`status = "disabled"` edits for both Ethernet nodes, the DWMAC component still
ran and reported:

```text
dwmac: EIC7700 platform initialization failed: InvalidDeviceTree
Component initialize error: Unknown
```

The kernel then reached `Spawn the first kernel thread`, but no
`asterinas-basic#` or userspace hello marker appeared before the bounded wait
expired. There was no panic marker in the captured log. The raw run result is
therefore `passed=false`.

## Recovery evidence

The watchdog/recovery chain did work. The serial capture contains a second
OpenSBI and U-Boot epoch, followed by automatic RockOS selection. A post-run
SSH check succeeded with UID 1000 and a fresh RockOS boot ID:

- before: `160963dd-5642-4904-9af0-9e5137fd10ce`;
- after: `b5b56fd5-470e-49c4-97cf-798c56ce43f6`.

Ping and SSH were available after recovery, and a passive serial observation
was quiet for 20 seconds. This is classified as **recovered via automatic
RockOS**, not as a Basic pass and not as a fresh U-Boot prompt handoff. The raw
runner result predates this independent recovery observation and still has
`recovered=false`; it is retained unchanged for auditability.

The capture does not distinguish whether the reset was caused by the armed
hardware watchdog or the configured `asterinas.reboot_after=60` path; both were
active by design. The recovery result is therefore attributed to the combined
protected recovery chain, not to one reset source alone.

Evidence files:

- [raw result](../../../target/megrez-physical-step1/basic-minimal-20261014-r2/result.json)
- [serial capture](../../../target/megrez-physical-step1/basic-minimal-20261014-r2/serial.log)

## Decision and next step

Do not add another physical peripheral or retry GMAC/DWC3 yet. The first
actionable defect is the device-gating boundary: Asterinas still discovers the
DWMAC path after the DT nodes were marked disabled, so this run cannot be used
as a UART/timer/MMC-only isolation test. Fix and test DT `status` handling in
QEMU/host-side validation first, and make the runner consume the recovery
boundary explicitly (prompt or RockOS login) before scheduling one more
physical run. The board is currently back in RockOS and no further hardware
commands were issued after the recovery checks.
