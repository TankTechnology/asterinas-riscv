# Megrez PowerVR four-segment MMU preparation (2026-09-30)

The selected Asterinas kernel now holds all four prepared META firmware
allocations under one DMA-backed GPU MMUv4 root. It **did not** install that
root in the GPU, execute firmware, observe a GPU read, or render pixels. The
desktop still uses software rendering and Xorg `/dev/fb0`.

The code allocation starts at the vendor firmware-heap base. The other GPU
virtual addresses are selected by Asterinas, not copied from a RockOS runtime
allocation. The pinned vendor [firmware allocator](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/server/devices/volcanic/rgxinit.c)
requires code at `0xE1C0000000`; its [META image processor](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/server/devices/rgxfwimageutils.c)
takes the data and coremem GPU addresses as separate boot parameters. All
four allocation flags include META protection; coremem code removes GPU write
permission in the vendor path.

| Allocation | Pages | Asterinas GPU VA | GPU writable |
| --- | ---: | ---: | --- |
| code | 13 | `0xE1C0000000` | yes |
| data | 5 | `0xE1C000E000` | yes |
| coremem code | 18 | `0xE1C0014000` | no |
| coremem data | 3 | `0xE1C0027000` | yes |

There is one unmapped 4 KiB guard page after each allocation. The selected
layout fits within the vendor's minimum 4 MiB firmware heap. META LDR section
destinations are a distinct address space. Before booting firmware, the META
boot configuration must be constructed with these actual GPU VAs and all its
other required fields; the current materialized image has no such patch.

The [QEMU kernel test](qemu-test.txt) selected exactly one test and passed. It
staged all four bounded payloads, read back each mapped allocation's first and
last PTE, compared physical addresses with the owned DMA allocations, checked
the coremem-code read-only bit, and confirmed each guard PTE was invalid.
The RISC-V release build passed. The board-tested Image SHA-256 was
`85c16cbced58b811d3f5f63688aed6bfd90e2aead13dac2e95d03d5d5bc7ce37`;
a rebuild after renaming the QEMU-only test produced the same Image hash.

RockOS verified the Image, Stage1, and DTB hashes before the non-default
selected boot. The first selected Asterinas boot ID was
`12102991-afc8-4574-9995-7ad456354980`. The root-only staging client checked
the matched firmware and four segment digests, then opened one control
session. Its [serial record](stage.serial.log) shows all four DMA allocations,
the GPU VAs above, `gpu_root_installed=0`, and exact CRG restoration on close.
A fresh [serial reopen](post-reopen.serial.log) proved UID 0, the same boot ID,
watchdog 0, both desktop/browser services active, and Xorg holding `/dev/fb0`.
No firmware bytes are stored in Git.

The board then completed a planned Asterinas-to-default-RockOS software reboot
and another selected Asterinas boot. The [boot receipt](boot-recovery.result.json)
records RockOS boot ID `539de51d-7336-453e-8f6e-68556a29ef1a` and the
checked boot artifacts. A final [fresh serial reopen](recovery-final.serial.log)
proved UID 0 on Asterinas boot `9735fe49-cc2b-4862-b5c9-7cc89b0d77fb`,
watchdog 0, desktop-ready marker matching the boot ID, both services active,
and Xorg on `/dev/fb0`. This is a software-reboot recovery test, not recovery
from a GPU firmware hang.
