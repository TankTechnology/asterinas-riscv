# Megrez DT-status Basic gate — 2026-10-15

This record covers the host/QEMU gate and one guarded physical attempt. It is
not a qualification pass: the physical attempt reached the Basic shell and
recovered through the hardware watchdog, but the selected menu entry loaded the
immutable full DTB.

## Host and QEMU evidence

- `kernel/comps/dwmac/src/arch/riscv.rs` treats absent/`ok`/`okay` status as
  enabled, gates disabled GMAC ports before clocks, reset GPIOs, or MMIO, and
  rejects a disabled ancestor such as `/soc`.
- `kernel/comps/usb/src/arch/riscv/mod.rs` gates both EIC7700 wrapper nodes
  before PCI/xHCI setup, rejects partial wrapper trees and selector aliases,
  and checks every selected host ancestor before MMIO acquisition.
- Host tests passed, including the Basic runtime device gate and minimal DT
  readback tests:

  ```text
  python3 -m unittest -q tools.riscv.tests.test_megrez_boot_menu \
    tools.riscv.tests.test_megrez_board_session \
    tools.riscv.tests.test_megrez_hardware_probe
  Ran 139 tests ... OK
  ```

- The DWMAC and USB component ktests completed successfully in the project
  container. They validate the status/parser/probe contracts, but do not claim
  that QEMU emulates the EIC7700 GMAC or DWC3 MMIO blocks.
- A host-side DTB replay copied the staged EIC7700 DTB, applied the minimal
  profile, and read back:

  ```text
  /soc/ethernet@50400000=disabled
  /soc/ethernet@50410000=disabled
  /soc/usb0@50480000=disabled
  /soc/usb1@50490000=disabled
  /framebuffer@fd800000=disabled
  /chosen/asterinas,usb-host=absent
  ```

The host Basic gate now scopes the serial transcript to the latest
`Enter riscv_boot` epoch. It requires UART and MMC registration and rejects
firmware framebuffer registration, GMAC preparation/datapath markers, and DWC3
host startup. This prevents an intended minimal profile from being recorded as
passed when the runtime DT was not actually modified.

## Physical attempt

Artifacts staged before the attempt:

```text
kernel sha256=f168df8cb2c8418bb4be26487411c5cb6d1c1baf9c4f6928a76cfa328aed21f1
kernel crc32=67895f0f
manifest sha256=d16ea663bd49d6306a9e9aee1ee62eaefeadcf4a744c1306e554ce35a12aead0
```

The run used the hardware watchdog, reached `BASIC_OK`, and returned to a
fresh OpenSBI/U-Boot prompt. That proves the bounded recovery chain. It does
not prove the requested minimal device set. Within the Asterinas epoch the
serial log contains:

```text
framebuffer: Registered firmware framebuffer
dwmac: prepared GMAC0 ...
dwmac: prepared GMAC1 ...
usb: Selected DWC3 USB host 0 ...
usb: Selected DWC3 USB host 1 ...
```

The menu cycle used `/extlinux/asterinas-menu-a681aed72669.conf`, whose Basic
stanza points at the immutable full DTB. The volatile `fdt set ... status
disabled` commands in `megrez_board_session.py` were therefore not applied.
The original run receipt has been corrected to `passed=false,
recovered=true` so it cannot be used for promotion.

Evidence:

- [serial log](../../../target/megrez-physical-step1/dt-status-basic-20261015/physical-basic-run/serial.log)
- [corrected result](../../../target/megrez-physical-step1/dt-status-basic-20261015/physical-basic-run/result.json)

## Next physical action

Do not count this run as the minimal Basic qualification. The host runner is
now corrected so its `basic` cycle uses the direct guarded artifact loader
(`megrez_board_session.py` with `device_profile=minimal`) from a fresh U-Boot
epoch. The next physical run must keep one serial owner, apply and read back
all five `status = "disabled"` nodes plus the removed USB selector, arm the
watchdog, and only then boot. Its runtime transcript must pass the Basic device
gate before any result is marked passed.
