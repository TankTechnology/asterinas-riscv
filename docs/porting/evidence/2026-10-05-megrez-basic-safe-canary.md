# Megrez Basic recovery-safe canary

Date: 2026-10-05 (Asia/Shanghai)

The Basic menu entry was hardened before the first physical Basic run:

- `asterinas.reboot_after=60` is present in the Basic entry.
- `--hardware-watchdog` is accepted only for a Basic cycle. It is armed after
  a fresh U-Boot prompt and before `sysboot`, with clock, reset, component and
  control-register readback checks.
- Menu preparation now rejects a Stage1 archive without `bin/busybox` and
  `bin/sh`, preventing a late `basic-busybox-missing` failure.

The immutable canary menu has SHA-256
`43860f7a807f90cd2fac2930cbf9ba3272d220a3e0218c6f6501edffbc80364c` and was
published as `/boot/extlinux/asterinas-menu-43860f7a807f.conf`. The persistent
selector remained unchanged at
`02280720efe7a1ad0ac084cdc20429406631e12d2e16f05638544bab0883fb26`.
The BusyBox Stage1 is
`7806b8348eb10456930388bdd250880205d198035b2da5cf7be17348a2a5f671`.

QEMU validation passed for both Basic and Probe with this kernel and Stage1.
The physical Basic cycle used the stable FTDI device
`/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0` and produced:

- result: `target/megrez-physical-step1/basic-cycle-safe-busybox-wdt-20261005/result.json`
- serial log: `target/megrez-physical-step1/basic-cycle-safe-busybox-wdt-20261005/serial.log`
- `BASIC_OK` marker and `sync; reboot -f` were observed;
- a fresh OpenSBI/U-Boot epoch was observed after the reboot;
- watchdog readback was `clock=0xfe3fff83`, `reset=0x1`, component type
  `0x44570120`, and `CR=0x1f/TORR=0xf`.

The board was subsequently returned to RockOS. An independent SSH check
observed a new boot ID and UID 1000, and read back the candidate and selector
hashes. The final closure run below also completed the authenticated UID 0
check; no credential is recorded.

## Closure run

The same menu and Stage1 were run once more with the same hardware-watchdog
option. Both Basic results passed with the same menu identity:

| Run | boot-to-ready | menu-to-ready | `BASIC_OK` | software arm | firmware recovery |
| --- | ---: | ---: | --- | --- | --- |
| `basic-cycle-safe-busybox-wdt-20261005` | 16.415 s | 13.832 s | 1 | 1 | OpenSBI/U-Boot |
| `basic-cycle-safe-busybox-wdt-20261005-r2` | 16.126 s | 13.672 s | 1 | 1 | OpenSBI/U-Boot |

The one-shot Probe on the same candidate also passed:
`target/megrez-physical-step1/probe-cycle-safe-busybox-20261005/result.json`.
Its serial log contains exactly one probe pass, one `count=1 status=pass`, one
automatic reboot marker, and a fresh OpenSBI/U-Boot epoch.

After the final RockOS recovery, a fresh nonce-framed SSH command observed
UID 0 with boot ID `cede669c-4824-4ea9-8485-e3ab54b2fbbe` and read back the
candidate and persistent selector hashes. The stable serial device was closed
and reopened twice; both attempts reached the RockOS login prompt, and a final
SSH read confirmed the same boot ID. No credential is included here.
