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
