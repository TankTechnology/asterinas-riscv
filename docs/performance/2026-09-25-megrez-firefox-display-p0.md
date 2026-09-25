# Megrez Firefox display P0 observation

This is a short attribution sample from the Asterinas boot documented in
[the physical boot note](2026-09-25-drm-main-physical-boot.md), not a benchmark
score. The boot ID was `613f0ade-3352-45f3-bbd9-6b7d4959521f`, the Image
SHA-256 was `92e46db13dc2c23ecbdcf4b7eb67b8aa27b8a259f5cf99461e6c9c0eeeefabe7`,
and Xorg was configured for 1920 × 1080. The root serial command channel was
verified before the experiment and closed afterward.

At the X11 session, `xdotool` requested Firefox navigation to the local
`file:///usr/share/doc/firefox/copyright` page (82,470 bytes) and sent ten
`PageDown` keys approximately 0.6 seconds apart. The before/after snapshots
were 17 seconds apart, including navigation and a short settle interval. The
window title remained `Mozilla Firefox`; the page URL, physical input, and
visible frames were **not independently confirmed**. Do not interpret this as
trusted-input-to-visible-frame latency or a clean per-scroll result.

| Counter, before → after | Delta |
|---|---:|
| DRM dirty presents, 127 → 228 | 101 |
| DRM dirty bytes, 235,860,728 → 589,754,424 | 353,893,696 bytes |
| DRM dirty present time, 2,350,946,000 → 5,728,396,000 ns | 3,377,450,000 ns |
| Xorg all-thread CPU run time, 4,602,874,000 → 8,270,463,000 ns | 3,667,589,000 ns |
| Firefox all-thread CPU run time, 46,433,045,000 → 49,925,076,000 ns | 3,492,031,000 ns |
| Firefox all-thread scheduler wait time, 9,181,771,000 → 12,948,754,000 ns | 3,766,983,000 ns |

The average dirty present took 33.44 ms and moved about 3.50 MB in this
window, versus 1.274 ms for small mostly idle updates on the same boot. The
3.377 s of display present time accounts for most of the Xorg thread CPU
increase, though the counters alone do not prove every Xorg cycle was spent
in that code. This is enough to prioritize removal of the firmware-framebuffer
copy loop. Firefox also consumed 3.492 s of CPU, so display acceleration alone
cannot be assumed to meet the 2× end-to-end experience goal.

Raw local evidence, including counter and `schedstat` snapshots, is at
`/home/ubuntu/.codex/asterinas-board-prepare-20260925/firefox-scroll-p0-20260925.txt`.
The next selected boot enables `asterinas.dc_probe=1` to log the live display
controller address, stride, mode registers, and DRM GEM pool physical address.
The probe makes no register writes and leaves the firmware backend active.
