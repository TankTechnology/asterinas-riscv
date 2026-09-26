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
writeback experiment on Asterinas. The safe PowerVR reference boot also
remains pending a verified reset route: RockOS's installed `pvrsrvkm` module
does not match its running kernel, and an early hang in a temporary matching
kernel cannot currently be recovered remotely.
