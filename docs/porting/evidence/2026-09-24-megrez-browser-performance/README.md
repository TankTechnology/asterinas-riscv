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
