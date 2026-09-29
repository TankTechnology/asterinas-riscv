# Firefox manual-start video measurement (2026-09-30)

This is one bounded check of the measurement mechanism on the selected
Asterinas desktop boot, **not a GPU-speedup result**. The desktop used Xorg
`fbdev` and Firefox software rendering. The fixed 1280×720, 300-frame,
10-second VP8 clip had SHA-256
`1aa863f2a73698241c9daa016c7bfcfa0f94b038ca9f0b29f296c3ca0b65eb95`.

[`video_filter_probe.py`](../../../../tools/riscv/debian/rootfs/video_filter_probe.py)
now accepts `start=manual`. It loads the clip without autoplay and exposes a
hidden DOM start button so Marionette's isolated script context can trigger
the page's normal playback handler. The default `start=auto` behavior remains.
[`video_manual_phase.py`](../../../../tools/riscv/debian/rootfs/video_manual_phase.py)
waited for `readyState >= 2`, read Firefox/Xorg `schedstat`, clicked once,
waited for the `ended` result, and read the counters again. The host probe
server independently received the [same video result](metrics-s30i.json).

| Run | CPU-counter window | Firefox video counter | Renderer CPU run | SwComposite CPU run | Xorg CPU run |
| --- | ---: | ---: | ---: | ---: | ---: |
| `s30i` | 10.192 s | 115 dropped / 300 total | 8.230 s | 7.685 s | 0.522 s |

The page's `playing`-to-`ended` time was 10.005 seconds. The wider CPU window
includes the Marionette start and completion polling. This run preloaded the
video and did not use the older ptrace PC sampler, so its 115/300 count is not
a controlled before/after comparison with the earlier 145/300 result.
Neither the CPU counters nor the browser video counter prove physical HDMI
presentation. They also do not separately measure YUV conversion and
composition; symbol-localized PC samples from the earlier run remain the
evidence for those hotspots.

The [guest record](s30i-record.json) has SHA-256
`dff5c8a934ce3ec96549ad193240704c3d4e678b134b4be291bf0e613786a85a`.
The [run transcript](s30i-run.serial.log) records the checked guest script
hash and bounded result. The [fresh serial check](s30i-post.serial.log)
confirms boot ID `9735fe49-cc2b-4862-b5c9-7cc89b0d77fb`, watchdog zero,
and both desktop and browser services active. The temporary HTTP servers were
stopped after the run.

Two preparatory attempts did not play a frame: Firefox did not preload enough
data without an explicit `preload="auto"`, and Marionette's isolated sandbox
could not read a page-defined `window.startProbe` function. The DOM button
solves the second issue without changing the playback handler. A next DRM
run needs read-on-demand scanout counters at the same start/end points;
the existing five-second cumulative log cadence cannot give exact display
time for this 10-second window.
