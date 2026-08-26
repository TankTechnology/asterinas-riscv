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

This foundation defines and validates the separately signed Debian security
source and its immutable schema-6 provenance contract, but does not enable a
`browser-m5` rootfs build.  The stacked builder follow-up requests
`firefox-esr`, retains both InRelease files, records each package's source role,
and publishes the six-file artifact set.  That browser profile keeps the fixed
one-GiB image contract, so a real build still needs the documented capacity
preflight before publication.

This milestone does not claim network playback, audio, hardware decoding,
Direct Rendering Manager acceleration, or encrypted-media DRM support.

## Minimal signed-security-source design

The current builder retains and verifies one base `InRelease`, re-fetches that
same document before publication, maps every apt list to it, and publishes a
manifest containing one signed-metadata identity.  M5 needs a two-source form of
the same invariant, not a special exception for Firefox:

1. Define immutable source records for `trixie` and `trixie-security`, each with
   its own HTTPS mirror, suite, expected Codename, retained `InRelease`, and
   Debian archive keyring verification.  Base requires `Suite: stable`, a
   canonical `Version: 13.x`, and `Codename: trixie`; security requires the
   distinct exact tuple `Suite: stable-security`, `Version: 13`, and
   `Codename: trixie-security`.  Both must advertise `riscv64` and their exact
   expected Components lists.
2. Write both apt source lines only after both signatures pass.  During audit,
   map each apt list filename to exactly one source record and authenticate the
   decompressed Packages bytes against that source's retained `InRelease`.
3. Re-fetch and byte-hash-check both signed releases immediately before package
   admission.  A change to either release aborts the build.
4. In browser schema 6, replace the manifest's singular `signed_metadata`
   object with a sorted,
   exact-key `signed_sources` array.  Each entry records role, mirror URL, suite,
   InRelease URL and SHA-256.  Publish both retained files under distinct names.
5. Keep package admission unchanged after indexes are concatenated: every `.deb`
   hash must still have one unique package/index row.  Also record the source
   role on each admitted package to preserve provenance when versions overlap.

Browser M5 uses manifest schema 6 because remote main already assigns legacy
schema 5 to `desktop-m5-network`. Existing M1-M4 and network-M5 manifests keep
their legacy single-source shape and remain byte-for-byte compatible.

The security InRelease advertises the component as `updates/main`, while the
authenticated checksum paths and apt list remain `main/binary-riscv64/Packages`.
The source validator checks the former metadata field exactly; index admission
continues to bind the latter path and must not rewrite one into the other.
