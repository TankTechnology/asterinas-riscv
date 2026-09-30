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
The offline RISC-V release build succeeded with Image SHA-256
`1516590ee3fb74184d96ef4cdaf2ec6c728c8c910b9c4b06593daf03b5318bce`.
These are build and simulated-register checks only. The new Image has not
been booted on the board. The preceding Asterinas desktop boot was still
responsive through the root serial console when checked, but it runs an
older Image.

The next hardware gate is a one-shot boot with both preflight flags, fresh
nonce-bound serial control after reconnect, GPU register evidence, and
software recovery to RockOS. A separate bounded probe must then install
the root in the vendor order, release META reset, poll the FWIF
`bFirmwareStarted` flag, capture faults on timeout, and stop/reset the GPU
before freeing any mapped DMA. Command completion and pixel readback remain
necessary before desktop or Firefox acceleration can be claimed.
