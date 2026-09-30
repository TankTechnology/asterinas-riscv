# Megrez PowerVR META reset preflight (2026-09-30)

The kernel now has an opt-in prefix of the selected RockOS `RGXStart`
sequence. After four prepared firmware segments and owned FWIF/MMU pages are
staged, `asterinas.powervr_reset_preflight=1` performs the vendor's secure-bus,
SPU/Jones soft-reset, META master-boot, wrapper, MMUv4 range and AXI-ACE
register setup. It leaves the META processor **held in reset**. The existing
`asterinas.powervr_mmu_preflight=1` flag can then install and read back the
firmware-private catalogue; neither flag releases META or proves a GPU page
walk. Closing the root-only control descriptor restores the CRG reset state.

The sequence follows the pinned
[`rgxstartstop.c`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/server/devices/volcanic/rgxstartstop.c)
for BVNC 30.3.408.101. Its MMU range values follow the same driver's
[`RGXMMUInitRangeValue`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/server/devices/volcanic/rgxlayer_impl.c)
with the pinned [`config_kernel.h`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/config_kernel.h)
16 KiB non-4K heap default and a 4 KiB global range. Register offsets and
reset masks in [registers.json](registers.json) were extracted from those
vendor headers by [`measure_rgx_start_registers.sh`](../../../../tools/riscv/drm/measure_rgx_start_registers.sh).
The RISC-V and native ELF sections matched byte for byte; their section
SHA-256 is `3395f4c7d4cd237f9029b9ea08a8a092679cd3f94c2072049db8ff161c13e2b0`.

The [focused QEMU kernel test](ktest.txt) selected one test and reported
`1 passed; 0 failed`. Its fake register bank checks the source-derived reset
sequence and that no write releases the META reset before catalogue setup.
The first offline RISC-V release Image had SHA-256
`1516590ee3fb74184d96ef4cdaf2ec6c728c8c910b9c4b06593daf03b5318bce`.
It booted the board, but the catalogue step returned EIO. A diagnostic boot
read back `context=0x00000000 base=0x10000000` before the write. Bit 28 is
`RGX_CR_MMU_CBASE_MAPPING_INVALID_EN`, so the original all-zero precondition
was wrong. Clearing the register to zero would also have made address zero a
valid catalogue base. The fix accepts the exact invalid state and restores it
on cleanup; it still rejects any nonzero address bits or unexpected context.

The fixed release Image has SHA-256
`139b7a7c9d519215b5026c07122edb63363651ab5628a1f0c690e2934ed699c7`.
Two focused QEMU kernel tests selected one test each and reported `1 passed;
0 failed`: catalogue install/readback/clear, and rejection of an invalid root
without register mutation. The release build also completed. On the board,
one-shot boot `fbb8674c-00c5-4bfb-b857-bd4bf7adb300` returned:

```text
ASTERINAS_POWERVR_META status=reset_prepared meta_held=1 release=not_attempted gpu_visibility=unverified
ASTERINAS_POWERVR_MMU status=catalogue_prestate context=0x00000000 base=0x10000000
ASTERINAS_POWERVR_MMU status=catalogue_register_readback root_daddr=0x1f1a43000 context=0 gpu_visibility=unverified
ASTERINAS_POWERVR_OWNER session=closed crg_restored=1 aclk=0x00000020 cfg=0x00000000 gray=0x00000000 reset=0x00000000
```

The root-only staging command exited 0 after staging four firmware segments.
Twice after closing and reopening the stable serial device, nonce-framed
commands reported UID 0, the same boot ID, and reboot watchdog 0. Both
checks also reported the desktop and browser services active with one Xorg
process. A subsequent manual safe reboot, guarded by the current-boot desktop
ready marker and the verified reboot-script SHA-256, reached U-Boot. Selecting
the vendor RockOS entry started Linux 6.6.87. A nonce-framed root command
reported UID 0 and new RockOS boot ID
`2e6d0321-293a-4c67-810f-f21a3b8392fc`; two more checks after closing and
reopening the stable serial device confirmed that same root identity. The
board was left in RockOS with a root serial control channel. This proves
software recovery for this selected boot, not recovery from an actual GPU
firmware hang, which has not yet been attempted.

The host staging verifier exited 1 **after** the successful stage:
it searched `dmesg` for markers that were visible in the serial transcript
but absent from `dmesg`. The kernel stage was not rerun. This verifier issue
does not change the GPU visibility status.

The next hardware gate is a bounded probe that must install the root in the
vendor order, release META reset, poll the FWIF
`bFirmwareStarted` flag, capture faults on timeout, and stop/reset the GPU
before freeing any mapped DMA. Command completion and pixel readback remain
necessary before desktop or Firefox acceleration can be claimed.
