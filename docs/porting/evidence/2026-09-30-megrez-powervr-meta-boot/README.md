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

## Pinned vendor start path now available

The exact [Volcanic `rgxstartstop.c`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/server/devices/volcanic/rgxstartstop.c)
was retrieved from the same RockOS commit after the source rate limit reset;
its SHA-256 is
`6b3d0f243e0028d9c18a1f7194194dd30fc9bab925bb0c15a75d69d074d8a4a8`.
The paired [Volcanic layer implementation](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/server/devices/volcanic/rgxlayer_impl.c)
has SHA-256
`6b189b407837445c8387043fccb6a0cddb03cf1736b8f71af5d288d604b1d675`.
Its `RGXDoFWSlaveBoot` returns false in this vendor build, so the board uses
the **META master-boot branch**; the slave-port writes in `RGXStartFirmware`
are not the start sequence to reproduce.

For BVNC `30.3.408.101`, the pinned chip config reports
`HOST_SECURITY_VERSION=1`, `ECC_RAMS=0`, `MMU_VERSION=4`, META MTP219, and
`SYS_BUS_SECURE_RESET`. The applicable `RGXStart` sequence disables the initial
secure-bus guard, performs the staged soft-reset sequence, selects META master
boot, sets up the META wrapper and applicable MMU/AXI registers, installs the
firmware-private page-catalogue base, marks the device powered, and only then
deasserts META reset with a 32-cycle wait on each side. This is a source
contract for the next implementation, **not a passed hardware boot**. A
bounded firmware-ready/fault observation and recovery design is still required
before executing that sequence on the board.

The [vendor power-up path](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/server/devices/rgxpower.c)
does not treat `RGXStart` returning as proof of firmware execution. It
invalidates and polls the shared `RGXFWIF_SYSINIT.bFirmwareStarted` field for
true, with a timeout and fault dump on failure. Asterinas owns and maps the
four firmware image segments and, since the configuration-heap mapping below,
three zeroed configuration slots. It does **not** populate a valid FW interface
object. The next step must establish the matching FWIF structure layout,
initial data, dependent allocations, and a bounded readback before a
firmware-ready claim is possible. Writing the catalogue base
and releasing META reset alone would be an unsafe and unverifiable shortcut.
The pinned [FW interface structure](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/include/volcanic/rgx_fwif_km.h)
contains runtime configuration, trace and system-data pointers, coremem DMA
metadata, and `bFirmwareStarted`; a zeroed four-byte flag by itself cannot
stand in for this boot contract.

## Firmware configuration heap mapping

The same pinned RockOS source defines a 32 MiB firmware raw heap in
[`config_kernel.h`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/config_kernel.h)
and reserves its final three 64 KiB granules in
[`rgx_heap_firmware.h`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/include/rgx_heap_firmware.h).
The allocation order in `rgxfwutils.c` is connection control, OSINIT, then
SYSINIT. Their GPU virtual addresses are `0xe1c1fd0000`, `0xe1c1fe0000`, and
`0xe1c1ff0000`. The configuration region ends at `0xe1c2000000`.

The kernel now owns one zero-initialized, uncached Die 0 DMA allocation per
granule and maps all three in the same unpublished GPU MMU root as the image
segments. The entries are writable and set `PMMETA_PROTECT`. The stage log
reports the three addresses with `fw_config_initialized=0` and
`gpu_root_installed=0`; the GPU has not used these mappings.

The new RISC-V QEMU ktest checks both ends of each 64 KiB slot, physical-page
continuity, PTE flags, the gap before the configuration heap, and the unmapped
address after it. It passed with `1 passed; 0 failed` in
[`fw-config-ktest.serial.log`](fw-config-ktest.serial.log). The pre-existing
four-segment mapping ktest also passed separately. A normal RISC-V kernel build
and targeted `rustfmt --check` passed. The whole-workspace formatting check
still reports unrelated pre-existing changes, so it is not used as a pass
claim for this patch. None of these tests proves firmware execution, a GPU
rendered pixel, or a real-board boot of this new kernel image.
