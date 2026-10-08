# Megrez PowerVR MMUv4 page-table preparation (2026-09-30)

The selected Asterinas boot prepared a three-level GPU page table for the
firmware **code** allocation, using pinned, uncached, identity-addressed DMA
pages. It did not install the catalogue address in a GPU register, start the
firmware, observe a GPU DMA read, submit work, or produce pixels. The ordinary
desktop remains on software rendering and `/dev/fb0`.

## Matching hardware contract

The [pinned RockOS build](https://github.com/rockos-riscv/rockos-kernel/tree/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic)
selects the `hwdefs/volcanic/km` definitions. Its
[`rgx_bvnc_table_km.h`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/hwdefs/volcanic/km/rgx_bvnc_table_km.h)
row for `30.0.408.101` has MMU-version index 2, whose value is 4, and does
not set `MH_PARITY`. The exact 40-bit virtual-address indices, 32-bit catalogue
entries, 64-bit directory/table entries, and valid/protection bits come from
[`rgxmmudefs_km.h`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/hwdefs/volcanic/km/rgxmmudefs_km.h)
and [`rgxmmuinit.c`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/server/devices/rgxmmuinit.c).
MMUv4 directory entries do not encode the old page-size field. The vendor's
firmware allocation flags include GPU read and write plus META protection;
the vendor PTE derivation also selects `AXCACHE_WBRWALLOC`.

The firmware's META LDR section bases describe destinations in the META
loader's address space. They must not be used as GPU virtual addresses.
[`rgx_heap_firmware.h`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/include/rgx_heap_firmware.h)
sets the raw GPU firmware heap base to `0xE1C0000000`, and `rgxinit.c` asserts
that the first non-MIPS firmware-code allocation starts there. This selected
preflight maps only that code allocation. The actual placement of data and
coremem allocations, GPU MMU register programming, and firmware boot remain
for the next stage.

## Verification

Four focused RISC-V QEMU kernel tests passed individually, each selecting one
test and reporting `1 passed; 0 failed`: vendor entry encoding, invalid-range
rejection, PC/PD/PT index boundaries, and mapping an owned two-page DMA buffer
across a page-table boundary with duplicate mapping rejected. A fifth focused
test staged all four firmware segments and required a nonempty code page-table
root before the control session ended ([test receipts](qemu-tests.txt)). The
optimized RISC-V kernel build passed.
The [table code](../../../../kernel/src/device/dri/powervr_probe/mmu.rs) keeps
each mapped allocation alive with its table root and poisons a partially built
root after a write or readback error.

The board-tested release Image SHA-256 was
`d916098c0e3fcfcd819897b9407b018b6edc1dbcc38e681921060e2f4c38e005`.
After comment and import formatting changes, the release Image rebuilt to
`6fb3052d5494033d2dcefd117c4660828e28ed2029bcaecea90a0157dee6ef38`;
that later binary was compiled but was not installed on the board. The
formatting changes do not alter the MMU operations or control flow.
RockOS verified that Image, the existing Stage1
`666d58af0673e2af52c9f1f36b5438095c759d95b3efcd800180054606bb3d71`,
and the prepared DTB
`465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba`.
U-Boot checked their loaded sizes and CRC32 before the non-default selected
boot. The default RockOS menu entry was unchanged; see the
[first boot receipt](boot-first.result.json).

On Asterinas boot `54daa6eb-7c6d-43c7-9ca7-e1eb095a7eae`, the matched
META image and all four reconstructed segment hashes passed. The
[serial transcript](stage.serial.log) shows code/data/coremem allocations of
13/5/18/3 DMA pages, a prepared code catalogue at `0x2f6b23000`, and
`gpu_root_installed=0`. Closing the exclusive root-only control session
restored the three clock words and five reset bits to their original values.
After closing and reopening the serial device, a fresh framed command proved
UID 0, the same boot ID, watchdog 0, and both desktop/browser services active
([receipt](post-reopen.serial.log)). No firmware bytes are stored in Git.

The board then completed a planned Asterinas-to-RockOS software reboot and
another non-default Asterinas boot, recorded in the
[recovery receipt](boot-recovery.result.json). A fresh serial reopen on final
boot `11eb3028-78f4-47cb-952b-8e6318352e0e` proved UID 0, watchdog 0,
desktop-ready state, both services active, and Xorg attached to `/dev/fb0`
([final check](recovery-final.serial.log)). This proves software recovery for
page-table preparation, not recovery from a GPU firmware hang.
