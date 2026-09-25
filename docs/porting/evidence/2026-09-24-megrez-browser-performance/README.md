# Short physical Firefox performance split on Megrez

This run used the existing Asterinas Desktop canary built from
`9690d59f2d09e374bea036a1a26d79dc215e5e86`, Image SHA-256
`3e3707b7e59f46303a914395a8dd4a85de55d0feb063fa26bb510130ce02b40e`.
It is an ancestor of the newly merged `main`, **not** a physical run of merge
commit `e51f1c12b116d02cf6d6c62cbc8828e93be5a3b9`. RockOS verified the
versioned Image, Stage1 and independent menu hashes before the boot; see the
[RockOS inventory](rockos-current-inventory.json). The menu still defaults to
RockOS. `/boot` reported zero available space, so no new Image was staged.
The host selected its Desktop entry through a fresh U-Boot epoch.
The [boot serial log](physical-canvas-boot.serial.log.gz) records the selected
files and the root debug console. The combined desktop readiness poll timed out,
but a fresh [diagnosis](physical-canvas-diagnose.serial.log.gz) on the same
Asterinas boot ID `c6846443-9d90-4ff5-854e-5c4132fcfbbc` found Xorg,
Firefox, desktop readiness, and browser service active. The software reboot
watchdog was disarmed. This run does not establish a cold-start time.

Firefox loaded two small LAN fixtures from the host. The
[Canvas page](canvas-perf.html) filled a solid-color 2D canvas on each
`requestAnimationFrame` for eight seconds. The order was small, large, large,
small; the page reported `visibilityState=visible` in every run. The
[result](physical-canvas-result.json), [host/guest probes](physical-canvas-host.py)
([guest](physical-canvas-guest.py)), and [serial capture](physical-canvas-serial.log.gz)
retain exact samples and SHA-256-framed transport.

| Run | Canvas | rAF frames / 8 s | p95 interval | Intervals >33 ms | Firefox CPU ticks | Xorg CPU ticks |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 640×360 | 586 | 13.3 ms | 8 | 883 | 191 |
| 2 | 1280×720 | 555 | 13.3 ms | 10 | 904 | 186 |
| 3 | 1280×720 | 526 | 13.3 ms | 10 | 894 | 178 |
| 4 | 640×360 | 564 | 13.3 ms | 11 | 865 | 186 |

The [input page](input-perf.html) received 16 X11-generated keystrokes that
Firefox reported as trusted `input` events. It measured from each event handler
to the second `requestAnimationFrame` callback: median 10 ms and nearest-rank
p95 23 ms. The [raw 16 samples](physical-input-result.json),
[host](physical-input-host.py) and [guest](physical-input-guest.py) probes, and
[serial capture](physical-input-serial.log.gz) retain the result. This does
not include physical USB-to-handler delay or a complex webpage's work.

For comparison, the **same encoded 1280×720 VP8 clip** on this Image previously
dropped 116 and 129 of 300 frames at native display size versus 5 and 10 at
640×360. Its Firefox `Renderer` and `SwComposite` threads used substantially
more CPU at native size; see the [prior video thread result](prior-video-thread-profile.json)
and [serial record](prior-video-thread-serial.json), and the
[earlier video qualification](../2026-09-24-board-video-current-image/README.md).
The solid-color Canvas test does not reproduce that size sensitivity. It
narrows the next target to video frame presentation, conversion, scaling, or
software compositing; it does **not** isolate a kernel defect or prove which
Firefox stage dominates. There is no same-browser RockOS A/B result from this
run.

## Changing RGB frames from the same clip

A follow-up on the **same boot and Image** extracted two 1280×720 RGB PNGs
from the earlier VP8 clip at 4 and 5 seconds with `ffmpeg -ss 4` and
`ffmpeg -ss 5`, `-frames:v 1 -pix_fmt rgb24`. The source clip SHA-256 was
`1aa863f2a73698241c9daa016c7bfcfa0f94b038ca9f0b29f296c3ca0b65eb95`;
the committed [first](rgb-frame-a.png) and [second](rgb-frame-b.png) frames
have SHA-256 values
`d19de1f5b43f3b65bf909ae0118f0ff3a37092534466f5a6aba9f788cda63ab1`
and `f617023387d06fe7685dea127bd0df500bdce10e44767f498449d19fc0444d3b`.
The source is a moving, synthetic color test pattern with large flat regions.

The [RGB page](rgb-frame-perf.html) preloaded both PNGs and alternated
`drawImage` on each animation frame for eight seconds, at 640×360 and
1280×720 in small–large–large–small order. All four pages were visible.
The [result](physical-rgb-result.json), [host](physical-rgb-host.py) and
[guest](physical-rgb-guest.py) probes, [serial capture](physical-rgb-serial.log.gz),
and [command record](physical-rgb-host-commands.json) preserve the full data.

| Run | Canvas | rAF frames / 8 s | p95 interval | Intervals >33 ms | Firefox CPU ticks | Xorg CPU ticks |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 640×360 | 598 | 13.3 ms | 6 | 877 | 180 |
| 2 | 1280×720 | 574 | 13.3 ms | 9 | 945 | 192 |
| 3 | 1280×720 | 572 | 13.3 ms | 8 | 965 | 194 |
| 4 | 640×360 | 580 | 13.3 ms | 9 | 848 | 179 |

Changing decoded RGB images at native size also did not reproduce the video
playback cliff. This weakens a general 720p canvas copy or X11 presentation
explanation and makes the media-specific decode, YUV conversion, or video
presentation path a better next target. It does not prove a single stage is
responsible: two predecoded, low-detail frames are not a 30 fps video stream,
and rAF timing is not a direct dropped-video-frame measurement.

After this follow-up, the stable serial device was closed and reopened twice.
Each [nonce-framed handoff](physical-rgb-handoff.json) proved UID 0 on the same
boot, `systemd` on `/dev/mmcblk0p2` ext2, active desktop/browser services,
and watchdog `0`; the [first](physical-rgb-handoff-1.log.gz) and
[second](physical-rgb-handoff-2.log.gz) serial captures retain the responses.
The temporary host fixture server was stopped. No board boot files changed.

After the input test, two independent reopenings of the stable
`/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0` device each
returned a fresh nonce-framed UID-0 response for the same boot ID, `systemd`
on `/dev/mmcblk0p2` ext2, active desktop/browser services, and watchdog `0`.
The [handoff summary](physical-canvas-handoff.json) and its
[first](physical-canvas-handoff-1.log.gz) and
[second](physical-canvas-handoff-2.log.gz) serial captures retain this proof.
The serial descriptor and temporary host fixture server were closed. The board
was left on the working Asterinas desktop with the root debug console accessible;
an unattended reboot still defaults to RockOS. No board boot files were changed.

## Video-path isolation and mitigation attempt

A later short follow-up used the **same boot, Image, Firefox and Xorg processes**.
The original [720p VP8 clip](clip720.webm) has SHA-256
`1aa863f2a73698241c9daa016c7bfcfa0f94b038ca9f0b29f296c3ca0b65eb95`.
The [video-to-Canvas page](media-path-perf.html) kept the video element visible
at 320×180 and drew each `requestVideoFrameCallback` frame to Canvas. The
[samples](physical-media-path-v3-result.json), [guest](physical-media-path-v3-guest.py)
and [host](physical-media-path-v3-host.py) probes, and
[serial capture](physical-media-path-v3-serial.log.gz) preserve the exact run.

| Mode | Output | Dropped / 300 | Video-frame callbacks | `drawImage(video)` p50 / p95 |
| --- | --- | ---: | ---: | ---: |
| Direct video | 320×180 | 6 | 240 | — |
| Video + Canvas | 1280×720 | 236 | 53 | 86 / 152 ms |
| Video + Canvas | 640×360 | 242 | 55 | 90 / 134 ms |

This identifies a costly **synchronous video-to-Canvas transfer** in this
Firefox build; it is not proof that direct `<video>` playback takes the same
internal path. An earlier pilot with a 1×1 video element was discarded because
that element itself yielded only about 60 callbacks and over 226 dropped frames
regardless of Canvas size. The corrected probe retained a normally visible
320×180 video control. Callback counts are not interchangeable with Firefox's
`getVideoPlaybackQuality().droppedVideoFrames` counter.

For direct video, [CSS scaling](video-transform-perf.html) tested a 640×360
layout stretched to the same 1280×720 visible rectangle. In the ABBA
[result](physical-video-transform-result.json), direct native video dropped
124 and 134 frames; the stretched variant dropped 143 and 140. Thus merely
changing layout size did not help. The [guest](physical-video-transform-guest.py),
[host](physical-video-transform-host.py) and
[serial log](physical-video-transform-serial.log.gz) retain the experiment.

A [360p VP8 derivative](clip360.webm) was encoded from the original clip with
`ffmpeg -i clip720.webm -vf scale=640:360 -c:v libvpx -b:v 500k -an`;
it contains 300 frames at 30 fps and has SHA-256
`a083aa58898dbabc0f98f3c16208815dd4b4d160a151f4af6f044b314c725c49`.
At the same 1280×720 visible size, the [source-size ABBA result](physical-video-source-result.json)
was 143/139 dropped with the original 720p source and 92/96 with the 360p
source. The derivative also changes VP8 encoding complexity, so this is a
practical source-size comparison, not a pure pixel-count experiment. The
[page](video-source-perf.html), [guest](physical-video-source-guest.py),
[host](physical-video-source-host.py) and
[serial log](physical-video-source-serial.log.gz) retain the run.

Keeping the original 720p source, the [display-size result](physical-video-size-result.json)
was 87 and 83 dropped at 960×540, and 18 dropped in one 800×450 run. The
[guest](physical-video-size-guest.py), [host](physical-video-size-host.py) and
[serial log](physical-video-size-serial.log.gz) retain these points. Earlier
640×360 display runs dropped 5–10 frames. A smaller player is the only
verified immediate mitigation; 800×450 needs repetition before being used as
a dependable limit. The native 1280×720 workload remains substantially
impaired. Firefox `Renderer` and `SwComposite` dominate its sampled CPU time,
while Xorg consumed comparatively little. These data do not establish a kernel
defect or justify a speculative framebuffer change. No global Firefox or
website CSS was changed, since that would degrade unrelated pages without
fixing full-size playback.

The final [handoff record](physical-video-handoff.json) and its
[first](physical-video-handoff-1.log.gz) and
[second](physical-video-handoff-2.log.gz) independently reopened the stable
serial device and proved UID 0, the same boot ID, active desktop/browser
services and disarmed watchdog. The temporary LAN fixture server was stopped;
the board was left on Asterinas with working root debug-console access.

## Firefox cost attribution and reversible acceleration checks

The next short session used the same boot and Image, Firefox 143.0.3, and the
same 300-frame, 10-second 720p VP8 clip. The [thread chart](firefox-thread-cost.png)
([SVG](firefox-thread-cost.svg), [generator](plot-firefox-thread-cost.py)) is
**thread-level CPU attribution, not a native function-stack flame graph**.
The [sample](physical-thread-profile-result.json), [guest](physical-thread-profile-guest.py)
and [host](physical-thread-profile-host.py) probes, [command record](physical-thread-profile-host-commands.json),
and [serial capture](physical-thread-profile-serial.log.gz) retain the inputs.
Each run sampled 16 half-second intervals; collecting `/proc` and Marionette
data lengthened the sampling interval to about 10.5 seconds. This pair is a
diagnostic comparison, not a controlled benchmark against RockOS.

| Same 720p source | Dropped / 300 | Renderer user + kernel | SwComposite user + kernel | Combined runqueue wait |
| --- | ---: | ---: | ---: | ---: |
| Display 1280×720 | 143 | 6.52 + 0.58 s | 6.55 + 0.52 s | 4.25 s |
| Display 640×360 | 6 | 4.43 + 0.62 s | 4.27 + 0.23 s | 3.93 s |

The two hot threads consumed 14.17 seconds of CPU at native size versus
9.55 seconds with the same encoded source displayed small. User-mode work
accounts for 4.37 of the 4.62-second increase. Their combined runnable wait
changed by only 0.33 seconds, and Xorg used 46 versus 43 CPU ticks. This
points to size-dependent Firefox video presentation/software drawing as the
main source of the *difference*. It does not yet identify the function or
exclude a syscall or memory-management contribution to absolute cost.
Thread CPU times can overlap on different cores, and runqueue wait must not
be added to CPU time as a critical-path estimate.

A reversible [CPU-affinity A/B/A probe](physical-affinity-result.json)
([guest](physical-affinity-guest.py), [host](physical-affinity-host.py),
[command record](physical-affinity-host-commands.json),
[serial log](physical-affinity-serial.log.gz)) pinned `Renderer` to CPU 2
and `SwComposite` to CPU 3 for the middle native-size run. Dropped counts
were 138/300 before, 132/301 while pinned, and 136/300 after restoring
the original all-core affinities. Both threads still accumulated around two
seconds of runqueue wait. This small, non-replicated difference does not
support an affinity fix or a 2× speedup; the probe and a host cleanup restored
both affinity masks to CPUs 0–3.

An isolated temporary Firefox profile tested `gfx.webrender.force-disabled`.
Its [default result](physical-compositor-default-result.json) dropped
123/300 native and 12/300 small; the [candidate result](physical-compositor-basic-result.json)
dropped 129/300 native and 11/300 small. The corresponding [default](physical-compositor-default-serial.log.gz)
and [candidate](physical-compositor-basic-serial.log.gz) captures retain the
short runs. The [isolated launcher](physical-profiler-launch-guest.sh) records
the candidate preference and local Marionette port; the [stop record](physical-profiler-stop.json)
shows only the normal browser remained active afterward. The candidate did
not help and the normal browser service was left unchanged.

The [render-backend inventory](physical-render-backend-inventory.txt) found
`/dev/fb0` and no `/dev/dri`. Xorg ran with the fbdev provider and
`-extension GLX`. This boot therefore offered no measured GPU-backed path
for Firefox. The open DRM rollup PR #139 describes virtio-gpu 3D for QEMU
and a firmware-framebuffer fallback for Megrez; it explicitly does not claim
native EIC7700 display/GPU programming or physical performance. Its merge
cannot be counted as a verified acceleration of this board.

The next fix needs function-level evidence from a Firefox build with the Gecko
Profiler enabled, or equivalent RISC-V native stack sampling. The privileged
[probe](physical-gecko-profiler-probe-output.txt) found `Services.profiler`
undefined in this build; no `perf` binary was present, and the current
RISC-V `ptrace` implementation lacks register reads needed by a conventional
external stack sampler. The immediate engineering target is the hot
`Renderer`/`SwComposite` video path, especially YUV conversion, scaling,
and software rasterization. A candidate is accepted only after a same-boot,
same-clip A/B/A run preserves correct output and cuts the combined hot-thread
CPU cost from about 14.2 to at most 7.1 seconds per clip **and** reduces native
display drops below 30/300 in repeated short runs. These are proposed 2×
acceptance criteria, not results achieved here. Native hardware acceleration
would need a separate Megrez display/GPU implementation and validation.

The fixture server and temporary Firefox process were stopped. The final
[handoff](physical-firefox-profile-handoff.json) independently reopened the
stable UART twice and proved UID 0, the same boot ID, active desktop/browser
services, and watchdog `0`; the [first](physical-firefox-profile-handoff-1.log.gz)
and [second](physical-firefox-profile-handoff-2.log.gz) serial captures retain
the responses. No persistent browser, boot, or affinity configuration changed.
