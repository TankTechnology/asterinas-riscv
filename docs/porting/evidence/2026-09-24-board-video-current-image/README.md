# Short video playback on the current Megrez Image

The Megrez board remained on Asterinas boot ID
`80a2b08d-e2aa-4f6b-b3ae-9394770f8384` after the second controlled boot
recorded in [the route-dump evidence](../2026-09-24-route-local-table-current-stack/README.md).
That boot selected the production Image with SHA-256
`3e3707b7e59f46303a914395a8dd4a85de55d0feb063fa26bb510130ce02b40e`.
The board used the existing root debug console on the stable by-id UART device.

Firefox fetched a static page and a 10-second VP8 WebM from the LAN host.
The source was 1280×720 at 30 fps in both runs. Marionette navigated to the
pages and read `getVideoPlaybackQuality()` after playback ended:

| Display | Ended | Decode error | Dropped frames | Firefox CPU ticks | Xorg CPU ticks |
| --- | --- | --- | ---: | ---: | ---: |
| Native size | yes | none | 121/300 | 1854 | 51 |
| 640×360 | yes | none | 10/300 | 1394 | 43 |

Both runs completed in about 10.3 seconds after navigation. The
[structured result](result.json) includes navigation durations, fixture hashes,
and the boot identity. The [guest probe](guest-media-probe.py) and
[two static pages](index720.html) ([smaller variant](index720-small.html))
record the measurement method; the 1.25 MB WebM remains in the local
`/home/ubuntu/asterinas-board-media-20260924/` fixture directory, identified
by its SHA-256 in the result. The [serial transcript](current-image-media-serial.json.gz)
retains both short-line results and the guest command's zero exit status.

After playback, two separately reopened serial connections each obtained a
fresh nonce-framed UID-0 response for the same boot ID, `systemd` on
`/dev/mmcblk0p2` ext2, active desktop and browser services, watchdog `0`,
and a successful bounded route lookup. The [first](current-image-handoff-1.log.gz)
and [second](current-image-handoff-2.log.gz) handoff logs preserve those
responses. The descriptor was closed after the checks, and RockOS remains
the unattended default boot entry.

This short repeat agrees with the earlier 117/300 versus 5–10/300 observation
on an ancestor Image. It points to display-size-sensitive Firefox work as a
profiling target, but the counters do not identify which paint, copy, or
composition stage dominates. This generated clip has no audio, seeking, or
streaming-player JavaScript; it is not a general website or long-run stability
test.
