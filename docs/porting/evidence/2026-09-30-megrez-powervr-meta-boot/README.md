# Megrez PowerVR META boot configuration preflight (2026-09-30)

The pinned BVNC `30.3.408.101` firmware now has a generated META bootloader
configuration for the four firmware GPU virtual addresses selected by Asterinas.
The generated code segment passed a bounded root-only DMA staging session on
the selected Asterinas desktop boot. **The GPU MMU root was not installed, the
firmware was not executed, and no GPU-rendered pixel was observed.**

The selected RockOS DDK's
[image processor](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/server/devices/rgxfwimageutils.c)
puts configuration at code-buffer offset `0x200`. It writes the privileged JTAG
pair, the META data-segment mapping, the 17 configuration writes decoded from
the firmware's LDR stream, cache controls, a zero-pair terminator, the coremem
code firmware address and size, and the META-DMA code address. The
[chip configuration](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/hwdefs/volcanic/km/configs/rgxconfig_km_30.V.408.101.h)
selects MTP219, 96 KiB coremem, pipeline version 0, SLC_VIVT, and META_DMA;
the [DDK second-thread condition](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/hwdefs/volcanic/km/rgxdefs_km.h)
therefore selects two META threads. The
[firmware-private MMU context](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/include/rgx_common.h)
is context zero. These are fixed inputs in
[`rgx_meta_boot.py`](../../../../tools/riscv/drm/rgx_meta_boot.py), rather than
runtime guesses.

The board read the original firmware from
`/lib/firmware/rgx.fw.30.3.408.101` and verified SHA-256
`25e9e7ff4645292ceb991617dba028e350a5c17088a105230dbaaaf995f4413b`.
The generated configuration was 296 bytes at offset 512, contained 34
register/value pairs including the 17 LDR writes, and had SHA-256
`2c70442a16a343dbae1312d3d1f54fedf773a26d544ff4702c58cb8fcd88d4d5`.
The patched code allocation had SHA-256
`77bb4e9e42f25a40e67e9002c5f76fa7c5a702cb5efe42ffaad57594dc244118`.
The other three segments retained their previously measured hashes. The
generator also rejects any LDR code write that overlaps the 296-byte boot
configuration; rerunning that check on the matched board firmware produced the
same two hashes in the [overlap-check transcript](overlap-check.serial.log).
Firmware bytes and generated segments remain private in
`/run` and are not committed.

The [unit tests](unit-tests.txt) cover the pair order, two-thread cache values,
suffix, identity checks, and LDR/configuration overlap rejection. The
[board preparation transcript](prepare.serial.log) records the full metadata
and hashes. The [DMA staging transcript](stage.serial.log) records four
successful CPU readbacks and the prepared GPU MMU root, explicitly reporting
`gpu_root_installed=0` and `gpu_visibility=unverified`. Closing the control
session restored the CRG state. The [fresh serial reopen](post-reopen.serial.log)
confirmed UID 0, boot ID `9735fe49-cc2b-4862-b5c9-7cc89b0d77fb`, watchdog
zero, both desktop and browser services active, and the patched code-segment
hash unchanged. It did not verify physical HDMI output.

The next hardware gate is installing the GPU-private root and performing a
bounded firmware-ready handshake with fault/timeout evidence and a verified
RockOS recovery path. Only after that can command submission, fences, and a
readback pixel test establish actual GPU rendering.
