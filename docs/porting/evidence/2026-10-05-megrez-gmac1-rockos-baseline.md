# Megrez GMAC-1 RockOS baseline

Date: 2026-10-05 (Asia/Shanghai)

This is a read-only RockOS observation after the authenticated software reboot.
No Asterinas image was started, no U-Boot environment was changed, and no
GMAC/DMA/PHY register was written by the experiment.

## Control identity

- SSH target: `debian@10.100.19.200` with the existing strict host key.
- RockOS kernel: `6.6.87`, `riscv64`.
- Boot ID: `f051fcb7-e5e8-422a-81fe-ddacefbae442`.
- `end1` is `UP, LOWER_UP`; `end0` is down.
- The host-side capture is retained under
  `target/megrez-physical-step1/rockos-gmac1-baseline-20261005/`.

## Device-tree contract

The two Ethernet nodes are present and enabled:

| Instance | MMIO | IRQ | TX clock cell | Reset mask | Link |
| --- | --- | ---: | --- | --- | --- |
| GMAC-0 | `0x50400000` | `61` | `3, 552` | `0x00000004` | `end0` down |
| GMAC-1 | `0x50410000` | `70` | `3, 553` | `0x00000002` | `end1` up |

Both nodes use `eswin,win23-qos-eth`, `rgmii-txid`, the shared system clock and
reset controllers, and per-port HSP/RGMII and reset-GPIO cells. The raw DT
properties are in `read-only-baseline.log`.

## Vendor-driver evidence

RockOS logs show the vendor `win2030-eth-dwmac` driver reading User ID `0x10`
and Synopsys ID `0x52` from both controllers, discovering the RTL8211F PHYs,
and configuring RGMII. GMAC-1 (`end1`) subsequently reports a 1-Gbps full-duplex
link. This proves the physical block is readable after the vendor platform
sequence, but does not identify which individual clock, reset, GPIO, HSP or
RGMII write is sufficient on its own.

The earlier Asterinas read-only GMAC-1 probe timed out before its first register
read and was recovered by the hardware watchdog. The safe conclusion is to
keep platform bring-up and register access separate: model the sequence in
QEMU first, then perform at most one guarded physical transaction.

## Desktop isolation change

`tools.riscv.megrez_desktop_boot` now accepts
`--device-profile minimal` (also exposed as
`MEGREZ_DESKTOP_BOOT_DEVICE_PROFILE=minimal`). It applies volatile U-Boot DT
edits that disable both GMAC and both DWC3 nodes for one desktop boot. The
default `full` profile is unchanged; no persistent menu or MMC data is altered.

