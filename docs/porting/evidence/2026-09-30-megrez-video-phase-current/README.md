# Current-kernel Megrez video-path A/B/A (2026-09-30)

The same 300-frame, 10-second VP8 file (SHA-256
`1aa863f2a73698241c9daa016c7bfcfa0f94b038ca9f0b29f296c3ca0b65eb95`)
played three times on one Asterinas boot, with the source held at 1280×720.
The display size was 1280×720, 640×360, then 1280×720 again. The selected
kernel Image SHA-256 was
`a4fc74046245b286305ac786b40e291409e75d98e682bf06ca2ff2d5fcc20c56`;
the boot ID remained `b36ee7a2-3081-49d4-84d4-402cb48f0d0a`. The
hash-pinned [`video_filter_probe.py`](../../../../tools/riscv/debian/rootfs/video_filter_probe.py)
served the clip. The root-only guest
[`video_manual_phase.py`](../../../../tools/riscv/debian/rootfs/video_manual_phase.py)
was run in Firefox PID 244's network namespace and bracketed playback with
`/proc/*/task/*/schedstat` and `/proc/asterinas_drm_scanout` snapshots.
The small-display guest copy differed only in the page's `size=small` URL
parameter (SHA-256
`aca7c9e813faaf1b3dfb16b0b972850241265a6796d86c6dc74f3189bc1f60da`).
The host fixture server was stopped after the run.

| Display | Dropped / 300 | Renderer CPU | SwComposite CPU | Xorg CPU | Playback wall |
| --- | ---: | ---: | ---: | ---: | ---: |
| Native A, [raw](native-a.json) | 116 | 7.890 s | 7.572 s | 0.499 s | 10.026 s |
| Small, [raw](small.json) | 8 | 4.915 s | 4.137 s | 0.392 s | 10.004 s |
| Native A2, [raw](native-a2.json) | 124 | 7.794 s | 7.280 s | 0.490 s | 10.009 s |

The native-size rendering pair accumulated 15.46 and 15.07 CPU-seconds,
versus 9.05 CPU-seconds at half width and height, while Xorg stayed below
0.5 CPU-seconds. The two rendering threads run concurrently, so their CPU
times are **not** elapsed latency. The A/B/A result corroborates the earlier
matching-symbol PC samples locating most hot `libxul` samples in SWGL's
`linear_row_yuv<false>`; it does not yield a per-function elapsed time or
separate YUV, composition, and display-submit phases.

DRM phase profiling was enabled, but all three playback windows had zero
`/proc/asterinas_drm_scanout` successes and zero measured DRM copy/sync time.
This does **not** mean display is free. Xorg PID 229 held `/dev/fb0` open and
used its fbdev provider, so the DRM scanout counter does not observe this
desktop's presentation path. Xorg CPU is an upper bound on work charged to
that process, not a direct timer for framebuffer writes or physical scanout.
The next timing instrument must observe the actual Firefox SWGL/compose path
and the fbdev write path, or switch the desktop to DRM and then recheck that
the DRM counter advances before using it for display attribution.

After the third run, two independently reopened UART sessions on
`/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0` returned
UID 0, the same boot ID and desktop-ready marker, active desktop service,
Xorg's `/dev/fb0` descriptor, and the unchanged temporary manual reboot
helper under `/run`. The selected one-shot U-Boot entry left default RockOS
unchanged. A final browser-service check returned `active` with the original
Firefox PID 244. This session did not perform a RockOS return. No physical HDMI
capture was available, so the test proves Firefox frame counters and the
fbdev owner, not the visible pixels on the monitor.
