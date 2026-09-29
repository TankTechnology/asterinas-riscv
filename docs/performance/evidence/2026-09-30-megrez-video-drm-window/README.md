# Megrez Firefox video with bounded DRM counters (2026-09-30)

The selected RISC-V release Image was built from main `679acc6b0` and had
SHA-256 `78bf3a4bcb7fc446187b5263adf74b06ac647971e1a09b53e50b6eeb6c780c8d`.
RockOS boot `ce372466-ad38-42f5-8f40-f3bb75c66554` verified that Image,
the existing Stage1, and the prepared DTB before the non-default Asterinas
boot. The [boot result](boot-result.json) records their identities. Asterinas
boot ID was `ae17015f-216f-409a-a5b6-fdfe9e14cd5e`, with
`asterinas.drm_phase_profile=1`. The default RockOS menu entry was unchanged.

The same hash-pinned 1280×720, 300-frame, 10-second VP8 clip was preloaded
and played once in each provider. Clip SHA-256:
`1aa863f2a73698241c9daa016c7bfcfa0f94b038ca9f0b29f296c3ca0b65eb95`.
[`video_manual_phase.py`](../../../../tools/riscv/debian/rootfs/video_manual_phase.py)
bracketed browser playback with per-thread `schedstat` and the new
`/proc/asterinas_drm_scanout` snapshot. The Firefox page posted an independent
[DRM result](metrics-s30j.json) and [fbdev result](metrics-s30k.json) to the
host fixture.

| Provider/run | Counter window | Dropped / total | Renderer + SwComposite CPU | Xorg CPU | DRM dirty presents | DRM copied bytes | DRM present time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| DRM `s30j` | 10.264 s | 141 / 301 | 13.777 s | 5.093 s | 159 | 587,443,200 | 4.819 s |
| fbdev `s30k` | 10.115 s | 118 / 301 | 15.718 s | 0.499 s | 0 | 0 | 0 |

The full [DRM](s30j-record.json) and [fbdev](s30k-record.json) guest records
contain boot IDs, exact windows, per-thread counters, browser video metrics,
and the scanout deltas. Their SHA-256 digests are respectively
`8cd48b2c365bce7b18fb76a1a19e640de4189a7cd4d60d8dc369d324870508f3`
and `c8e5733e56d87cd74526204310d068aa49a25543657630d0cd403e35a7d63fbc`.
The [DRM activation transcript](activate-drm.serial.log) proves Xorg opened
`/dev/dri/card0` and that counters began increasing. The fbdev run's zero
counter delta proves the proc entry did not misattribute `/dev/fb0` work.

The DRM run spent about 47% of its 10.265-second scanout-counter window inside
firmware framebuffer dirty-present calls. Its 477 sampled rows spent 21.156 ms
in the direct-copy-and-sync microprobe; that sample is **not** the total copy
time. Xorg's 5.093 CPU seconds are close to the 4.819-second present total.
The DRM run reported 23 more dropped frames than fbdev, while Firefox used
less render-thread CPU because it painted fewer frames (156 versus 183).
These are one run per provider, so the frame-count difference is directional
evidence, not a stable before/after estimate. No PowerVR rendering or physical
HDMI capture was involved. Software YUV conversion and composition are still
localized by saved Firefox PCs, not separately timed in milliseconds.

The first attempt to enter DRM [failed before Xorg](activate-first.serial.log):
the desktop launcher had put a fbdev-only config root under `/run`, while the
DRM config was installed under `/etc/asterinas/display-providers`. A temporary
systemd override selected that root and `ASTERINAS_DISPLAY_PROVIDER=drm`.
The override was removed after the run. The [restore attempt](restore-fbdev.serial.log)
used an overly narrow Xorg path check and returned failure even though Xorg
had opened `/dev/fb0`; Firefox was then explicitly restarted. Two independent
[fresh serial](final-reopen-1.serial.log) [reopens](final-reopen-2.serial.log)
proved UID 0, the same boot ID, watchdog 0, desktop and browser active,
Firefox running, responsive X11, and Xorg holding `/dev/fb0`.

The installed safe-reboot helper also refused an initially requested manual
reboot because its timeout calculation still applied the expired boot deadline.
The repository's corrected helper was hash-verified and used only from `/run`;
no persistent Asterinas rootfs file or default boot selection was changed.
RockOS retains the three checked test artifacts under
`/home/debian/asterinas/video-drm-20260930/` for replay or recovery. The
original failure and complete RockOS recovery transcript remain in the private
local experiment directory; credentials and raw login traffic are not in this
evidence directory.
