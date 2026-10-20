# Megrez Basic minimal-DTB retest — 2026-10-15 (r2)

This is the bounded follow-up to the earlier run recorded in
`2026-10-15-dt-status-basic-watchdog.md`. The earlier run loaded the full
DTB and is intentionally retained as a failed gate. This record covers the
corrected direct artifact-loader path and one physical retest.

## Host/QEMU gates

All host gates were run before the physical retest. The working tree was left
dirty; no reset, clean, commit, or push was performed.

| Gate | Result | Evidence |
|---|---|---|
| Basic direct-loader integration | PASS | `target/megrez-qemu-gates-20261015/host-gates-final.log`; the test suite completed 144 tests with `OK`, including real `dtc`/`fdtput`/`fdtget` edits, five status readbacks, `/chosen` selector removal, and ineffective-edit faults. |
| DWMAC QEMU ktest | PASS | `target/megrez-qemu-gates-20261015/dwmac-qemu-serial.log`: `40 passed; 0 failed`, `All crates tested`. |
| USB/DWC3 QEMU ktest | PASS | `target/megrez-qemu-gates-20261015/usb-qemu-serial.log`: `35 passed; 0 failed`, `All crates tested`. |
| Recovery-epoch regression | PASS | Included in the 144 host tests; stale prefixes are ignored and recovery requires ordered OpenSBI → U-Boot → prompt. |
| Formatting and diff checks | PASS | `python3 -m py_compile ...`; `rustfmt --edition 2024 --check` for the RISC-V DWMAC/USB files; `git diff --check`. |

The source hashes used for this retest are:

```text
91f423f72524ebf720d0ed1bc7681e02894c27bf3d6e449149273b0d3f5adb2d  tools/riscv/megrez_menu_board.py
a44c5240f4cf0a6a36888ef8c7639b42c320f6ca6e65fc040892072db64f23  tools/riscv/megrez_boot_menu.py
6087d702b9ca0f1f77488e8fce4c8071935f7d07bf348e38587da5da652eaae8  tools/riscv/tests/test_megrez_boot_menu.py
63af7ac1ca7624cfb8af32444e7049e5cb3b95643b4cfc8a0a0e02e4f6f6a2f6  tools/riscv/tests/test_megrez_recovery_epoch.py
0f557e6b09cfc12ca468a2b80c2de4ce798cc1b40691ca8cac76776f6023399c  kernel/comps/dwmac/src/arch/riscv.rs
0113e7dbf97b0061ce0e747ab95b1e81d1dfde8d358a47dbbebb01000e0a91b5  kernel/comps/usb/src/arch/riscv/mod.rs
e13c0291867ff18d16393ee0c5a3c43c6de2c89a4963b2c9894dfec25773a078  target/megrez-qemu-gates-20261015/host-gates-final.log
```

## Physical retest

The board was first returned to a fresh U-Boot prompt, and the current kernel
artifact was published through the existing canary path. The publication
manifest and kernel are:

```text
target/megrez-physical-step1/dt-status-basic-20261015-r2/publication/manifest.json
target/megrez-physical-step1/dt-status-basic-20261015-r2/publication/asterinas-35edfd0fa024.booti
manifest sha256=f59cad7bafa39091f60efc3ec634ea3b8cea8eea87e865fef7b9918d21559b13
kernel sha256=35edfd0fa024396eef9d038c029bce6b33612dea64f6216e34ed6abffbcd1db5
kernel crc32=9ee1d1dc
```

The single run used `cycle --mode basic --hardware-watchdog --from-uboot`
with one serial owner. It passed and recovered:

```text
result: target/megrez-physical-step1/dt-status-basic-20261015-r2/physical-basic-final/result.json
passed=true
recovered=true
basic_device_gate.forbidden_registrations=[]
basic_device_gate.required_registrations=[UART, MMC]
recovery_boundary=uboot-prompt
boot_to_ready_seconds=27.927063112030737
elapsed_seconds=67.03607030201238
serial sha256=ca19027a6140b9496fd7243e1080c759a2413a2403781e35d0e93fc6df768fbe
```

The serial evidence is in
`target/megrez-physical-step1/dt-status-basic-20261015-r2/physical-basic-final/serial.log`.
The loader applied and read back all five status changes:

```text
status = "disabled"                 # ethernet@50400000
status = "disabled"                 # ethernet@50410000
status = "disabled"                 # usb0@50480000
status = "disabled"                 # usb1@50490000
status = "disabled"                 # framebuffer@fd800000
chosen { stdout-path = "serial0:115200n8"; }
```

The runtime epoch contains `Enter riscv_boot`, UART registration, MMC
registration, `framebuffer: Framebuffer not found`, and
`usb: EIC7700 USB wrappers disabled; skipping USB host setup`. There are no
forbidden framebuffer registration, GMAC preparation/datapath, or DWC3 host
startup markers. The Basic shell emitted `BASIC_OK:<run-id>` and executed
`sync; reboot -f`; the fresh OpenSBI/U-Boot sequence ended at a U-Boot prompt.

One non-fatal firmware warning is retained verbatim at serial-log line 221:
`ERROR: reserving fdt memory region failed (addr=fffff000 size=1000 flags=4)`.
U-Boot immediately reports `Using Device Tree in place`, and the kernel then
boots through the Basic gate; this warning did not cause a device registration
or recovery failure.

`--hardware-watchdog` was enabled for the run as the recovery guard. The
observed reboot was the requested software reboot, so this receipt does not
claim that watchdog expiry was required or independently demonstrated.

## Scope and remaining boundary

This proves the corrected minimal Basic boot and recovery boundary. It does
not enable or qualify DRM, GPU, GMAC traffic, DWC3 enumeration, or the desktop
path. The board is left at the recovered U-Boot prompt, not in a running
experimental guest. The bounded Basic payload emitted its run nonce, mounts,
and architecture; it did not issue a separate `id -u` command or a
serial-close/reopen management probe, so this receipt must not be promoted as
the full authenticated UID-0 management-chain qualification.
