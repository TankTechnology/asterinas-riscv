# Megrez Firefox video CPU phase sample (2026-09-30)

The same pinned 300-frame, 10-second VP8 clip used in the
[current-kernel A/B/A run](../2026-09-30-megrez-video-phase-current/README.md)
was replayed once at 1280×720 on the same Asterinas boot ID
`b36ee7a2-3081-49d4-84d4-402cb48f0d0a`. Its SHA-256 is
`1aa863f2a73698241c9daa016c7bfcfa0f94b038ca9f0b29f296c3ca0b65eb95`.
No kernel reboot or image change occurred. The playback dropped 119 of 300
frames, versus 116 and 124 in the prior native-size A/B/A controls.

The root-only [`video_manual_phase.py`](../../../../tools/riscv/debian/rootfs/video_manual_phase.py)
recorded a 10.096 s monotonic playback bracket and thread CPU deltas. The
bounded [`thread_pc_sampler.py`](../../../../tools/riscv/debian/rootfs/thread_pc_sampler.py)
sampled the Firefox `Renderer` and `SwComposite` threads 90 times each at
100 ms intervals. All 180 samples' new absolute monotonic timestamps fell
inside the playback bracket, from 148 to 9050 ms after its start. The
ptrace stops accumulated 458 ms across both threads; the sampled run is a
profile, not the uncontended performance baseline.

The installed `/usr/lib/firefox/libxul.so` SHA-256 remained
`54076bf72d585b3591aa0edb814fee6a6012c6a9545aca43cd989359da23d1b7`.
PCs were translated using the two [relevant mappings](video-pc.maps) and
the matching `firefox-dbgsym` Build ID
`1c9f58ed7ccb6730d65e3957f560dc732b577218`.

| Sampled user PC | Renderer | SwComposite | Total |
| --- | ---: | ---: | ---: |
| SWGL `linear_row_yuv<false>` | 58 | 55 | 113 |
| SWGL `linear_blit<true>` | 18 | 14 | 32 |
| libc | 12 | 21 | 33 |
| Other `libxul` | 2 | 0 | 2 |

In the same 10.096 s bracket, `Renderer` used 7.926 CPU s and
`SwComposite` used 7.537 CPU s. Uniformly apportioning those measured CPU
times by each thread's PC sample counts gives **heuristic**, not direct,
estimates of about 9.71 CPU s in YUV rows and 2.76 CPU s in blit/composition.
Saved user PCs can include off-CPU locations and the samples are correlated;
these estimates must not be read as precise function durations or serial wall
time. The 113 versus 32 sample difference, matching symbol IDs, and earlier
size A/B/A behavior make software YUV conversion the best-supported first
GPU replacement target for this video workload.

Xorg used 0.497 CPU s in the bracket. A fresh root-console check showed
Xorg PID 229 held `/dev/fb0` and mapped its 8,294,400-byte region writable
and shared. Its video display path therefore does not call the kernel
`fb0.write_at` once per frame, and the zero DRM scanout counters do not
time it. Xorg's total CPU is an upper bound on CPU work charged to Xorg,
not an exact framebuffer-copy or physical scanout latency. Direct timing of
that memory-mapped path and of individual Firefox functions remains open.

The retained [playback counters](video.json), [PC samples](video-pc.jsonl),
and [two used map entries](video-pc.maps) are sufficient to check the time
window and repeat symbolization. The original full map file is retained
outside Git at
`/home/ubuntu/.codex/asterinas-video-phase-20260930/phasepc0930e.maps`
(SHA-256 `7cd02190dd26128260878d6765269d038355606abe84067241671e65312399e2`).
The host video fixture and temporary upload listener were stopped after
collecting the files.

After playback, two independently reopened sessions on
`/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0` returned
fresh nonce-framed UID 0 responses, the unchanged boot ID, active
`asterinas-browser-web.service`, Xorg PID 229, Firefox PID 244, and the
temporary `/run/megrez-safe-reboot.sh` SHA-256
`8aa9ba53778eed919983d7475158be20720d09f3ddb25d4dcfed20998f7b3a9f`.
This run did not invoke reboot or test recovery back to RockOS. There was no
HDMI capture, so browser counters and register/process state do not prove
the monitor's actual pixels.
