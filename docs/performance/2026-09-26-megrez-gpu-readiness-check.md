# Megrez GPU readiness and pixel-acceptance boundary

The current Megrez desktop has a working Asterinas root debug console and an
opt-in EIC7700 native scanout boot. It does **not** have verified PowerVR
rendering. The display controller can scan a GEM framebuffer without the GPU
drawing that framebuffer.

On 2026-09-26, the stable serial device was
`/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`.
Two separately opened, exclusive serial sessions returned nonce-framed UID 0
responses and the same boot ID,
`516751d7-4719-449f-8f6d-59e275602e65`. The observed command line included
`asterinas.dc_native_scanout=1` and `--debug-console=root`. The guest reported
`card0` and `renderD128` under `/dev/dri`; the latter is the generic render node
that `kernel/src/device/dri.rs` exposes whenever a display exists, including
the firmware-display path. It is not evidence of a bound PowerVR driver.
At the observation point `/proc/uptime` was 39066 seconds, but no HDMI pixels
or Firefox interaction were captured in this read-only check. The current
root image had neither `/usr/lib/asterinas/egl-pixel-probe` nor
`/usr/lib/asterinas/drm-gbm-probe`, and no `/lib/firmware/rgx*` files. Its DRI
directory offered `kms_swrast_dri.so` and `swrast_dri.so` through the Mesa
megadriver. The Image hash for this already-running boot was not established;
there was no new boot or recovery action.

The physical graphics gate previously accepted any fresh, structurally valid
HDMI PNG/JPEG as display evidence. It now compares that independent capture
with the final 1920 × 1080 Firefox screenshot, rejects a blank expected image,
rejects wrong dimensions and mismatched colors, and records the comparison
metrics. A failed comparison still enters the existing recovery path. QEMU and
host tests cover the acceptance and rejection behavior; no physical HDMI
capture was available for threshold calibration or end-to-end qualification.

Without a capture device, the EIC7700 dump/writeback registers in the RockOS
driver may provide a separate internal pixel witness, but that would verify
the controller's composition before HDMI rather than the monitor's pixels.
The register contract and DMA coherency must be checked before enabling a
writeback experiment on Asterinas.

## Matching RockOS PowerVR reference, 2026-09-26

The operator confirmed a person was available to reset the board if a
temporary kernel hung. We verified the staged `6.6.87-win2030` Image, initrd,
and DTB by both SHA-256 on RockOS and CRC32 after U-Boot loaded each item from
MMC. The boot used `booti` with RAM-only arguments; it did not change
`extlinux.conf` or the default entry. The selected kernel reached a login
prompt. A fresh, nonce-framed root shell reported boot ID
`fad44d27-e989-4da8-8520-448a120a7cda`, `uname -r` of
`6.6.87-win2030`, and matching `pvrsrvkm` vermagic.
U-Boot printed `ERROR: reserving fdt memory region failed` for a 4 KiB
reservation before `Starting kernel`; the Linux boot nevertheless completed.
The first host script treated that diagnostic as a fatal U-Boot error and
stopped listening, so a fresh serial session checked the login prompt and
kernel identity. The reservation warning remains a separate boot-contract
item to inspect before reusing this selected kernel as a default.

`modprobe pvrsrvkm` returned zero. The kernel reported loading
`rgx.fw.30.3.408.101` and `rgx.sh.30.3.408.101`; DRM exposed `es_drm` on
`card0` and `pvrsrvkm` on `card1`, plus `renderD128`. `vulkaninfo --summary`
identified a PowerVR A-Series AXM-8-256 integrated GPU and the proprietary
24.2@6643903 driver. `eglinfo -B` identified the same hardware for OpenGL ES
3.2, although it also listed a separate software OpenGL/softpipe platform.
Device enumeration alone therefore remains an insufficient acceleration test.

The new `tools/riscv/drm/gles-pixel-probe.py` creates an EGL context from the
specified GBM render node, draws a white quadrilateral over the left half of a
black 16×16 FBO, and reads back a left and a right pixel. On RockOS,
`--expect-renderer PowerVR` returned `left=(255,255,255,255)` and
`right=(0,0,0,255)` with exit status zero. Deliberately requiring `llvmpipe`
returned a renderer mismatch and exit status one. This verifies actual GPU
rendering through the reference render node without a capture card; it does
not verify HDMI output or Asterinas GPU support.

One bounded `glmark2-es2` desktop blur scene at 800×600, with
`--frame-end readpixels`, reported 671 FPS and 1.491 ms/frame. This is a
reference measurement, not an Asterinas speedup. The broader `--validate`
reported 21 Success, six Failure, and six Unknown outcomes; a zero process
exit code does not mean every validation scene passed. During a separate
bounded run, the client had both `/dev/dri/card0` and `renderD128` open.
The serial transcript is retained at
`/home/ubuntu/.codex/asterinas-evidence/2026-09-26/megrez-gpu-reference.serial.log`.
Asterinas still lacks a verified PowerVR render driver and its
matching firmware/userspace contract.

The tested RockOS userspace uses the vendor DDK path. Its
[kernel DRM entry point](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/server/env/linux/pvr_drm.c#L408-L428)
dispatches `PVR_SRVKM_CMD` to `PVRSRV_BridgeDispatchKM` and has a separate
`PVR_SRVKM_INIT` command. The `/usr/include/drm/pvr_drm.h` installed on RockOS
also declares the upstream DRM PowerVR `DEV_QUERY`/`CREATE_BO`/`SUBMIT_JOBS`
interface, but those are not the vendor entry points shown by this loaded
driver. Porting only that public header's ioctls would therefore not satisfy
the tested `libVK_IMG.so`/GLES stack. The next kernel work must inventory the
vendor bridge, its memory and firmware lifecycle, and the exact dma-buf/fence
contract before exposing a PowerVR render node on Asterinas.

After the reference run, a software reboot returned to U-Boot, the original
default RockOS entry reached a login prompt, and a separately reopened
exclusive serial connection returned nonce-framed UID 0, `uname -r=6.6.87`,
and new boot ID `62b2a783-2532-45de-92e0-6173edaf4ac8`. The selected GPU
boot therefore left no persistent boot-entry change. This restoration is
control-path evidence, not an Asterinas desktop or HDMI validation.
