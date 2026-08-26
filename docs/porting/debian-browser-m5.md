# Debian browser M5 admission

M5 extends the signed Debian desktop M4 root, but does not change its existing
gate.  The first candidate is Debian's prebuilt `firefox-esr` for `riscv64`.
Debian Trixie publishes that architecture, while its Chromium binary set does
not currently publish `riscv64`; Chromium is therefore not an M5 candidate.

The first runtime gate is deliberately offline, silent, and display-backend
neutral.  Firefox opens `browser_m5_probe.html` from `file://`.  The page proves
modern JavaScript execution, decodes a repository-owned VP8/WebM fixture with no
audio track, and emits three ordered DOM markers: JavaScript pass, video
`canplay`, and video `ended`.  A later runner must observe the markers through a
browser automation or accessibility interface; process existence or a window
title alone is insufficient evidence.

Rootfs integration must add the signed Debian security archive as a separately
verified source before requesting `firefox-esr`.  It must record the source
InRelease and package checksum in the immutable manifest, rather than silently
depending on whichever security update is current.  The integration is kept
out of this initial change because the M1-M4 builder currently authenticates
only the configured base mirror and has a fixed one-GiB image contract.

This milestone does not claim network playback, audio, hardware decoding,
Direct Rendering Manager acceleration, or encrypted-media DRM support.
