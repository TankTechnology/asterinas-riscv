# Firefox virgl sanitizer source patch gate

Date: 2026-10-17

## Scope

This is a bounded host-side source-integrity gate for the Firefox WebGL
renderer boundary. It does not build or install Firefox, run QEMU, or claim a
WebRender runtime pass. The patch is intended for the pinned Debian ESR 140
source identified by the existing compatibility audit.

## Artifact

The repository artifact is
`tools/riscv/debian/rootfs/firefox_compat/0001-recognize-virgl-webgl-renderer.patch`.
It adds the `virgl` prefix to `ChooseDeviceReplacement()` and adds a focused
`TestLinuxVirgl` regression test. The patch has no timestamps in its file
headers and applies with zero fuzz.

## Verification

The dry run and actual apply were performed against the read-only Firefox
source snapshot in `/tmp/firefox-esr-audit.l06p` using a disposable tree in
`/tmp/firefox-patch-test`:

```text
cd /tmp/firefox-patch-test
patch --dry-run --fuzz=0 -p1 < \
  /mnt/shared/xaj/Program/asterinas/tools/riscv/debian/rootfs/firefox_compat/0001-recognize-virgl-webgl-renderer.patch
checking file dom/canvas/SanitizeRenderer.cpp
checking file dom/canvas/gtest/TestSanitizeRenderer.cpp

patch --fuzz=0 -p1 < \
  /mnt/shared/xaj/Program/asterinas/tools/riscv/debian/rootfs/firefox_compat/0001-recognize-virgl-webgl-renderer.patch
patching file dom/canvas/SanitizeRenderer.cpp
patching file dom/canvas/gtest/TestSanitizeRenderer.cpp
```

The resulting changes match the expected two hunks: one `virgl` sanitizer
branch and one `TestLinuxVirgl` test before the existing Radeon tests. The
patch SHA-256 is:

```text
0af3a98ec6b932366621eabbf8b1d359d3a6c8528dfae0c4635cc4f604b09325
```

## Result and boundary

**PASS:** the source patch is reproducible and applies without fuzz to the
audited source snapshot. **Not tested:** Firefox compilation, the gtest binary,
package metadata/provenance integration, and guest WebRender. A Debian ESR
source build and an authenticated rootfs package/overlay step are still
required before this change can be used by a runtime gate.
