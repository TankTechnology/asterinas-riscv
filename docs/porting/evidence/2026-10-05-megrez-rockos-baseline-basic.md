# Megrez RockOS baseline and Basic canary

Date: 2026-10-05 (Asia/Shanghai)

This record covers the requested read-only RockOS baseline and one minimal
Asterinas Basic boot. No hardware Probe was run.

## RockOS baseline

The board was already in RockOS when collection started. The collection sent
only bounded read-only shell commands and did not select a boot entry, transfer
an artifact, write a filesystem, or reboot the board.

- Model: `Milk-V Megrez`
- Compatible: `sifive,hifive-unmatched-a00 sifive,fu740-c000 sifive,fu740 eswin,eic7700`
- Kernel: RockOS `6.6.87`, `riscv64`
- Device-tree boot identity: `cede669c-4824-4ea9-8485-e3ab54b2fbbe`
- Platform devices: display control `0x502c0000`, GMAC `0x50400000` and
  `0x50410000`, DWC3 `0x50480000` and `0x50490000`, GPU `0x51400000`.
- DT interrupt properties: GMAC IRQs 61/70, DWC3 IRQs 85/86, GPU IRQ 15,
  display-control IRQ 238.
- RockOS `/sys/class/drm` exposes `card0` and `card0-HDMI-A-1`; network exposes
  `end0` and `end1`.
- The filtered PLIC table shows active display, GMAC, DWC3/xHCI and GPU/media
  driver lines. The debugfs clock summary was unavailable and debugfs reset
  enumeration was permission denied; DT clock/reset phandles are retained in
  the raw transcript for later privileged inspection.

Raw baseline transcript and nonce metadata:

- `target/megrez-physical-step1/rockos-baseline-20261005-r2/serial.log`
- `target/megrez-physical-step1/rockos-baseline-20261005-r2/baseline.json`

## Minimal Basic boot

The board was software-rebooted from RockOS to a fresh U-Boot prompt, then the
already-qualified menu candidate was selected once with the verified hardware
watchdog. No load was added. The cycle passed:

- menu SHA-256: `43860f7a807f90cd2fac2930cbf9ba3272d220a3e0218c6f6501edffbc80364c`
- boot-to-ready: `15.872 s`; menu-to-ready: `13.356 s`
- watchdog clock/reset/component/control readback passed
- one `BASIC_OK`, one software reboot marker, and a fresh OpenSBI/U-Boot epoch
- `sync; reboot -f` returned to U-Boot without a physical reset

Evidence:

- `target/megrez-physical-step1/basic-cycle-baseline-20261005/result.json`
- `target/megrez-physical-step1/basic-cycle-baseline-20261005/serial.log`
- `target/megrez-physical-step1/rockos-to-uboot-20261005/serial.log`

## Recovery and handoff

RockOS was selected from the same candidate menu after Basic. A fresh nonce
attestation observed UID 0, boot ID
`77dd6ff8-f018-4978-9366-a7a8d1bb7584`, and the expected default selector,
candidate menu, and BusyBox Stage1 hashes. The stable serial device was then
closed and reopened; a second nonce-framed UID 0 check observed the same boot
ID. The board was left running in RockOS.

- `target/megrez-physical-step1/rockos-after-basic-20261005-r2/attestation.json`
- `target/megrez-physical-step1/rockos-after-basic-20261005-r2/serial.log`
- `target/megrez-physical-step1/rockos-serial-reopen-after-basic-20261005-r2/reopen.json`
- `target/megrez-physical-step1/rockos-serial-reopen-after-basic-20261005-r2/serial.log`

The persistent RockOS selector remained unchanged at
`02280720efe7a1ad0ac084cdc20429406631e12d2e16f05638544bab0883fb26`.
