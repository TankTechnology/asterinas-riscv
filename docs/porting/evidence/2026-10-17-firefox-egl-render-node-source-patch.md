# Firefox EGL DRM render-node source patch gate

Date: 2026-10-17

## Scope

This is a bounded source-level gate for the Firefox WebRender EGL device
selection boundary. It does not build Firefox, alter a rootfs, run QEMU, or
claim a virgl/WebRender runtime pass.

## Change

`tools/riscv/debian/rootfs/firefox_compat/0002-prefer-drm-render-node-for-webrender.patch`
changes the existing `GLLibraryEGL::CreateDisplayLocked` condition so the
`forceAccel` WebRender request tries `GetAndInitDeviceDisplay()` even when GTK
has already initialized an X11 display. If device enumeration fails, the
existing surfaceless/default-display fallback remains available.

This directly addresses the audited boundary where Firefox's GTK path skipped
the configured `gfxVars::DrmRenderDevice()` and selected llvmpipe. It is a
candidate source fix, not a renderer spoof.

## Verification

Against `/tmp/firefox-esr-audit.l06p/GLLibraryEGL.cpp`, the patch was run with
both `patch --dry-run --fuzz=0 -p1` and a real `patch --fuzz=0 -p1`. The
post-apply source assertion passed:

```text
FIREFOX_EGL_RENDER_NODE_SOURCE_GATE pass forceAccel_device_fallback=1
```

Patch SHA-256:

```text
9e7b3352ae1d9a53296eddeeaf8fa66999fd0db2fca512f45a5c2d128c6f2963
```

## Boundary

The patch still requires an authenticated, provenance-tracked Firefox ESR
build and the strict QEMU scene gate. Until that binary is built and the gate
records `renderer=virgl`, screenshot pixels, frame samples, and restart
recovery, Task 6 remains incomplete.
