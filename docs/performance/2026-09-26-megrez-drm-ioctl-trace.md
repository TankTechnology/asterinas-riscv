# Megrez desktop DRM ioctl sequence before native scanout

The fixed-mode native-scanout design depends on what the current Xorg session
actually submits. In particular, a design that only accelerates `PAGE_FLIP`
would have no effect if the desktop repaints a mapped front buffer and reports
damage with `DIRTYFB`.

## Selected boot and control

The candidate was built from main commit `6534a2d64` with RISC-V/Sv39/SMP4
release settings. The Image SHA-256 was
`8371e2f43f8b8f00221a6d9fc1e63dddd750f3306d636d51ffbd62393957f956`.
The boot arguments set `asterinas.dri_trace=1` but did **not** set
`asterinas.drm_direct_copy=1`, so the selected boot also checked the new
firmware-copy default. The prepared DTB and stage-1 hashes remained
`465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba`
and `ea446515661f2380568426b3c37da0b6a0698fc51f88d32c62807fce04890962`.
The files were staged in `/home/debian/asterinas/dri-trace-6534a2d64/` on
RockOS partition 3 with matching local and remote sizes and SHA-256 values.
The temporary U-Boot entry loaded the kernel at `0x80200000`, DTB at
`0xf0000000`, and stage 1 at `0x83000000`, checked byte count and CRC32, and
ran `booti 0x80200000 0x83000000:${initrd_size} 0xf0000000`.

The host used only
`/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0` for the
serial connection. A UID-0 command confirmed the previous Asterinas boot ID
`6809f8c6-e206-4291-be3d-170219ad477b` before a software reboot reached
RockOS. The selected candidate boot ID was
`5c4a9824-7f18-496c-9a63-330ef12f9f91`; a fresh nonce-framed UID-0
response returned the same ID after closing and reopening the host serial
connection. Xorg reached 1920 x 1080, and Firefox's service reported
`MainPID=128`, `NRestarts=0`, `SubState=running`. The recovery watchdog was
disarmed only after these checks; its value was `0`. An ordinary reboot still
returns to RockOS because the persistent default entry was not changed.

## Observed desktop sequence

The trace logged 98 DRM ioctls in the startup window, including one
`MODE_SETCRTC` (`0xc06864a2`), 41 `MODE_DIRTYFB` (`0xc01864b1`), and no
`MODE_PAGE_FLIP` (`0xc01864b0`). Before the controlled short Firefox
operation, cumulative counts were 45 `DIRTYFB`, one `SETCRTC`, and zero
`PAGE_FLIP`. The operation requested the local
`file:///usr/share/doc/firefox/copyright` URL through X11 and sent five
`PageDown` keys. Afterwards the counts were 139, one, and zero respectively:
94 additional `DIRTYFB` calls, no new modeset or page flip. The active window
title remained `Mozilla Firefox`; the loaded URL and HDMI frames were not
independently verified. Trace logging itself adds overhead, so this run makes
no performance or user-visible-latency claim.

## P1 consequence

The current Xorg path is a mapped front buffer with damage notifications. An
initial fixed-mode native backend must make that buffer's modified bytes
visible to the non-coherent display engine on `DIRTYFB`, not only on
`PAGE_FLIP`. The physical 8.29 MB cache-clean probe measured 6.746 ms, making
that approach worth a guarded hardware pixel test. A front buffer can be
written while the controller scans it, so this path may tear. Do not claim a
tear-free desktop or emit flip-complete events until a real back-buffer,
vblank, and buffer-lifetime path exists. The firmware-copy backend remains
the fallback until the controller's DMA read and register handoff are verified.

Raw local records are in `/home/ubuntu/.codex/asterinas-dri-trace-20260926/`:
`manifest.json`, `candidate-boot.result.json`, `dri-ioctl-summary.txt`, and
`firefox-ioctl-sample.txt`.
