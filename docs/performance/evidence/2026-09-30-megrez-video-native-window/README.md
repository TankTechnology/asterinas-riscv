# Megrez native scanout: bounded 720p Firefox trial

The selected boot used the same release Image SHA-256
`78bf3a4bcb7fc446187b5263adf74b06ac647971e1a09b53e50b6eeb6c780c8d`
as the [firmware-copy DRM and fbdev trials](../2026-09-30-megrez-video-drm-window/),
adding only `asterinas.dc_native_scanout=1` to its boot arguments. U-Boot
checked loaded sizes and CRCs for the locally hash-checked kernel, initramfs,
and DTB. The default persistent boot entry remained RockOS. The selected
Asterinas boot ID was `f000cb81-3032-4f99-b3f0-20ee94134a98`. Its root UART
console answered nonce-framed UID and boot-ID commands after a fresh serial
open; the desktop-ready marker matched that boot ID and the recovery watchdog
was disarmed before the experiment.

A temporary `/run/systemd` override selected the installed DRM Xorg provider.
The process held `/dev/dri/card0`; the firmware-copy counter reported
`unavailable`, as expected when the native backend is selected. An early
`dmesg` probe reported native readiness for the 1920×1080 firmware mode. The
retained [kernel milestones](native-klog.txt) show submission of the GEM
address `0xf8001000` rather than the firmware address `0xfd800000`, and
reported `underflow=false` through 255 dirty notifications. At that last
milestone, accumulated cache-clean time was
494.965 ms. These power-of-two milestones span startup and playback; they do
**not** give the cache-clean time of the 10-second video window. Address
readback and software counters do not prove physical HDMI presentation.

The unchanged, preloaded, 10-second, 1280×720 VP8 clip had SHA-256
`1aa863f2a73698241c9daa016c7bfcfa0f94b038ca9f0b29f296c3ca0b65eb95`.
The [guest record](n30a-record.json) and independently received [host
metric](metrics-n30a.json) agree exactly. The bounded window was 10.193 s:

| Provider and run | Dropped / reported frames | Renderer CPU | SwComposite CPU | Xorg CPU |
| --- | ---: | ---: | ---: | ---: |
| Native DRM `n30a` | 123 / 300 | 7.752 s | 7.343 s | 0.866 s |
| Firmware-copy DRM `s30j` | 141 / 301 | 7.138 s | 6.639 s | 5.093 s |
| fbdev `s30k` | 118 / 301 | 8.001 s | 7.717 s | 0.499 s |

The other two records are in the linked previous evidence directory. Native
scanout removed the measured firmware-copy backend from this boot, but its
video still dropped 41% of reported frames. A single run per provider, with
native on a different selected boot, does not establish a stable provider
speedup. The lower Firefox CPU in the copy-DRM run accompanies fewer painted
frames and cannot be interpreted as faster rendering. The first GPU-rendering
target remains Firefox's software YUV conversion and composition; the native
display path alone does not reach the under-5% drop target.

An attempt to use the [Firefox Gecko
profiler](https://firefox-source-docs.mozilla.org/tools/profiler/code-overview.html)
for a lower-interference YUV-versus-composition CPU split exposed a build
limitation. Startup profiling variables reached the Firefox process, but
stopping the service produced no profile file. A temporary Firefox launch with
the [documented local Marionette system-access
flag](https://firefox-source-docs.mozilla.org/remote/Security.html) let the
chrome-context probe run. Firefox 143.0.3 then reported no profiler component,
no `nsIProfiler` interface, and no `Services.profiler` object. This proves the
profiling interface is unavailable in this board build; it does not separately
measure YUV and blit time. The temporary flag and service override were
removed immediately after the probe.

The fbdev restore helper's narrow check timed out after Xorg had actually
reopened `/dev/fb0`, leaving the browser service stopped. The browser was
started explicitly. The [final fresh-serial
check](final-status-clean.txt) shows UID 0, the same boot ID, watchdog zero,
both services active, Xorg holding `/dev/fb0`, a responsive 1920×1080 X11
server, and the privileged profiler flag absent. A separate Marionette session
also opened and closed successfully. The corrected safe-reboot helper was
hash-checked under `/run/asterinas-video-native-20260930/`; this native boot
was **not** rebooted to RockOS. The previous boot's software reboot to RockOS
and the selected boot's checks are recorded in the [boot result](selected-boot.result.json),
not in a public raw login transcript. There was no HDMI capture or physical
pixel verification.

The [verification output](verification.txt) checks the exact kernel and clip
identities, boot flag and result, the 9–11 s window, and byte-for-byte equality
of the host and guest video metrics. It also lists hashes of the retained
records. No second test image was created; the local trial used symlinks to
the existing Image, initramfs, DTB, and clip.
