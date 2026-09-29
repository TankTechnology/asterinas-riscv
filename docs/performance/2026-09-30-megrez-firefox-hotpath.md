# Megrez Firefox video hot path (2026-09-30)

## Decision

The first GPU integration target is Firefox's video YUV conversion and software
composition. Optimizing EIC7700 scanout or Xorg submission alone cannot remove
the dominant measured work in this workload. This is a priority decision, not
a measured GPU speedup or proof of physical HDMI presentation.

The selected Asterinas desktop boot used the same 10-second, 300-frame,
1280×720 VP8 clip in two short trials. The clip SHA-256 was
`1aa863f2a73698241c9daa016c7bfcfa0f94b038ca9f0b29f296c3ca0b65eb95`.
Firefox 143.0.3 reported 300 decoded frames in each trial. Firefox's internal
playback counter reported 149 and 145 dropped frames, respectively.

| Trial | Dropped / 300 | Renderer CPU run | SwComposite CPU run | Xorg CPU run | Renderer + SwComposite saved PCs: YUV / blit / libc / other |
| --- | ---: | ---: | ---: | ---: | ---: |
| `s30a` | 149 | not retained | not retained | not retained | 128 / 26 / 43 / 3 |
| `s30b` | 145 | 7.791 s | 7.557 s | 0.480 s | 112 / 45 / 40 / 3 |

The `s30b` run and wait times are deltas from per-thread Linux `schedstat`
around playback. The two Firefox render threads together ran for 15.348 CPU
seconds in 10.350 wall seconds; summing different threads is deliberate. Xorg
ran for 0.480 CPU seconds. This localizes the resolution-sensitive workload
within Firefox, consistent with the [previous paired 720p/360p
measurements](2026-09-29-megrez-firefox-video-and-nice.md).

Each trial also sampled the saved user PC of the Firefox `Renderer` and
`SwComposite` threads 100 times each, at 100 ms intervals. The Firefox
`libxul.so` build ID was `1c9f58ed7ccb6730d65e3957f560dc732b577218`;
the matching Debian debug symbols and `riscv64-linux-gnu-addr2line` resolved
the hot PCs to SWGL `linear_row_yuv<false>` and `linear_blit<true>`. The
`s30b` sample counts by thread were:

| Thread | YUV | blit | libc | other `libxul` |
| --- | ---: | ---: | ---: | ---: |
| Renderer | 54 | 22 | 21 | 3 |
| SwComposite | 58 | 23 | 19 | 0 |

Saved-PC counts show where the threads were observed, **not exact time spent
inside each function**. Ptrace stopped both threads for 584 ms in aggregate
during `s30b` (536 ms during `s30a`); this adds measurement interference.
The locally available libc debug package did not match the guest libc build
ID, so the 40–43 libc samples remain classified only as libc. This test does
not separately time video decoding, YUV conversion, composition, and display
submission in milliseconds; the thread CPU times and sampled symbols are the
current stage-localization evidence. A phase-timed trace or GPU timestamp path
is needed for exact stage budgets.

## Reproduction and evidence

The raw Firefox playback counters, saved-PC samples, gzip-compressed process
maps, and `s30b` thread deltas are in [the evidence
directory](evidence/2026-09-30-megrez-firefox-hotpath/).
The existing bounded
[`thread_pc_sampler.py`](../../tools/riscv/debian/rootfs/thread_pc_sampler.py)
was invoked for the two named Firefox threads with `--samples 100` and
`--interval-ms 100`. The sampler SHA-256 was
`ff13a7e69aef01dd09a7f72e45cbb017a3da972bc6aefff68be67893f6be10b0`.
The saved maps make ASLR address translation reproducible; use the executable
mapping's start and file offset to translate a PC before `addr2line`.

The selected boot ID was `d5e68c9c-bd7e-452d-b5d9-56c73a1574b4`.
`/dev/dri/renderD128` on this boot is the EIC7700 display device, not a
PowerVR render node. The current Firefox profile uses software rendering.
There was no HDMI capture, so monitor output, tearing, and physical frame
presentation remain unverified. No GPU hardware rendering is claimed.

## Selected DRM copy-path follow-up

The ordinary desktop profile above used Xorg `fbdev` and held `/dev/fb0`, not
`/dev/dri/card0`. The selected `c9310e0d0` release kernel was then booted with
`asterinas.drm_phase_profile=1`. A temporary `/run/systemd` override selected
the installed Xorg `modesetting` provider; `/proc` confirmed that Xorg held
`/dev/dri/card0`. The unchanged 10-second 720p clip reported 136 dropped of
300 decoded frames and 10.278 seconds of playback wall time. The two Firefox
render threads used 14.461 CPU seconds, and Xorg used 5.274 CPU seconds during
the Marionette navigation/playback window. The raw [playback
record](evidence/2026-09-30-megrez-firefox-hotpath/s30f-record.json) includes
the per-thread counters.

The [kernel counter snapshots](evidence/2026-09-30-megrez-firefox-hotpath/s30f-drm-phase.json)
bracketed the whole Marionette command, a 24.842-second interval that includes
navigation and script overhead. Dirty DRM presents increased by 162, copied
446,398,016 bytes, and spent 3.389 seconds in the firmware-framebuffer copy
path, averaging 20.92 ms per dirty present. The opt-in row sample recorded
492 rows and 16.976 ms of direct copy and sync; its separate GEM-read and
framebuffer-write fields were zero because this kernel selected direct copy.
These cumulative differences are **not exact costs of the 10-second video
alone**. The prior fbdev trial and this DRM trial also use different display
providers and different single runs, so their dropped-frame counts do not
establish a speedup.

This follow-up exposes an expensive DRM firmware-copy path while leaving the
main Firefox SWGL YUV/composition hotspot intact. Eliminating that copy is a
display-path target; hardware rendering still requires a working PowerVR
command, fence, and readback path. The override was removed and a fresh serial
connection verified that both desktop services were active again with Xorg
holding `/dev/fb0` on the same boot.

## Manual-start measurement window

A later [bounded manual-start run](evidence/2026-09-30-megrez-video-manual-phase/)
preloaded the same clip and took thread CPU snapshots immediately before the
single playback trigger and after its `ended` result. On the fbdev/software
desktop, the 10.192-second counter window contained 300 frames, 115 reported
drops, 8.230 seconds of Renderer CPU run, 7.685 seconds of SwComposite CPU
run, and 0.522 seconds of Xorg CPU run. Preloading and the removal of ptrace
sampling changed the measurement conditions; this is a better-defined window,
not evidence of a graphics speedup. The current DRM scanout counters still
need an on-demand readout to isolate display submission to this window.

## On-demand DRM window on the board

The read-on-demand `/proc/asterinas_drm_scanout` counter and matching
bracketing logic in `video_manual_phase.py` passed a single RISC-V QEMU kernel
test and three host parser tests, then ran on a selected release kernel on the
board. The [bounded experiment](evidence/2026-09-30-megrez-video-drm-window/)
used the same clip after manual preload. Xorg's DRM process held
`/dev/dri/card0`; the later fbdev process held `/dev/fb0` on the **same** boot.

| Provider | Dropped / 301 reported | Renderer + SwComposite CPU | Xorg CPU | Firmware DRM dirty-present time |
| --- | ---: | ---: | ---: | ---: |
| DRM | 141 | 13.777 s | 5.093 s | 4.819 s across 159 presents |
| fbdev | 118 | 15.718 s | 0.499 s | 0; no DRM presents |

The DRM window copied 587,443,200 bytes in 10.265 seconds; the present-call
time alone occupied about 47% of that interval. This makes the firmware copy
path a measured display bottleneck. The Firefox render threads also consume
more than one CPU's worth of time, so removing the copy alone does not solve
the software YUV/composition bottleneck. The lower Firefox CPU sum in the DRM
run accompanies fewer painted frames, and is **not** a measured speedup.
One run per provider cannot establish a stable drop-rate difference. The
counter does not prove HDMI pixels were shown, and it records no GPU work.
YUV conversion and composition still need separate stage timing beyond the
earlier saved-PC localization.

## Next gates

1. Complete the PowerVR firmware layout and bounded DMA/GPU-MMU ownership
   contract, then demonstrate firmware ready and clean recovery on a selected
   boot. The default RockOS entry and root serial channel remain the recovery
   path.
2. Run the unchanged 16×16 GLES pixel test twice with hardware readback,
   PowerVR renderer identification, zero GPU faults, and a clean reboot.
3. Connect the render result to EIC7700 scanout, verify the internal readback
   and fences, and then enable a selected Firefox GPU profile.
4. Repeat this same 10-second clip and a fixed page interaction workload.
   Require under 5% dropped frames at 720p, materially lower Firefox render
   CPU time, no GPU faults, and continued desktop and serial access. Internal
   readback does not replace later physical HDMI verification.
