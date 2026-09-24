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
Firefox stage dominates. A changing full-color RGB frame at both sizes would
separate general pixel-copy cost from the media/YUV path more clearly. There
is no same-browser RockOS A/B result from this run.

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
