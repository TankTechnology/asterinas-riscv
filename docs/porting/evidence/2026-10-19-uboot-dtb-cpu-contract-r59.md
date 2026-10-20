# U-Boot DTB CPU contract r59

This is a read-only static audit. No kernel, guest, serial device, or hardware was started.

## Inspected artifact

- DTB actually staged for the U-Boot desktop gate: `target/qemu-uboot/debian-root/qemu-virt.dtb`
- DTB SHA-256: `d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c2`
- U-Boot script evidence: `desktop_m4_gate.py` loads `/qemu-virt.dtb` with `ext4load virtio 0:0 0x90000000` and selects it with `fdt addr 0x90000000`.
- Parser: `tools/riscv/qemu_dtb_cpu_contract.py` using `fdtget`.
- JSON report: `/mnt/mydisk/qemu-uboot-dtb-cpu-contract-r59.json` (SHA-256 `c50b0ef49ddb1bf88f809cace18f07024421d865b3e9d2c219b637edf1cb84bb`).
- r57 boot disk: `/mnt/mydisk/qemu-startup-fbdev-r57/boot.ext4`; read-only `debugfs dump /qemu-virt.dtb` produced the same SHA-256 `d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c2` (`packed_match=true`).

## Contract table

| Property | Required by current main path | Observed in all four CPU nodes | If absent |
|---|---:|---:|---|
| `mmu-type` | yes | yes (`riscv,sv39`) | `validate_cpu_nodes` and `for_each_hart_id` skip the node; if none remain, Asterinas panics before UART with no MMU-capable CPU. |
| `riscv,isa` (or `riscv,isa-extensions`) | yes for ISA discovery | yes (`riscv,isa`) | ISA extension discovery cannot establish that CPU's set; this is not the specific zero-CPU panic. |
| `riscv,cbom-block-size` | yes in current DMA cache-maintenance path | yes (`64`) | the current `sync_dma_range` path expects and parses it; the isolated KVM patch adds a 64-byte fallback. |

Nodes audited: `/cpus/cpu@0` through `/cpus/cpu@3`; every node has all three properties.

## TCG, U-Boot, and KVM provenance

These are not the same DTB byte stream:

1. The U-Boot path uses the checked-in/staged DTB above, copied into the boot disk as `/qemu-virt.dtb` and explicitly loaded by U-Boot.
2. The direct TCG/OpenSBI path in r55/r58 does not pass `-dtb`; QEMU supplies a generated DTB. Its r58 serial receipt reaches `Enter riscv_boot`, `OSTD initialized`, and `first-kthread-enter`.
3. The KVM path generates a guest DTB from the host/KVM accelerator. The KVM evidence reports that its CPU nodes omit `mmu-type` (and may omit `riscv,cbom-block-size`), so it is a distinct input and is fixed by the isolated KVM patch or DTB normalization.

Therefore the KVM missing-`mmu-type` panic explains a silent KVM early failure, but it does not explain the current TCG no-output report: the TCG-generated DTB reaches Rust boot in r58, while the earlier TCG negative control stopped with the incompatible explicit `sv48=false`/Svade CPU contract. The TCG no-output investigation remains a separate CPU/handoff issue unless a future TCG DTB audit finds the property missing.
