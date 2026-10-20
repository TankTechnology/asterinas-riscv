# E3 official Firefox policy-r4 runtime failure (2026-10-18)

## Inputs

- rootfs overlay chain: `target/e3-render-policy-r1/overlay.json`,
  `overlay-r2.json`, `overlay-r3.json`, `overlay-r4.json`
- root image SHA-256: `daf60920477e2227f52b0a501536af3e8bf524fa72aabc76fc6afa4c74aea8cf`
- rootfs manifest SHA-256: `a3b6cd9d14a9b92843168f05fd64a35ec481855be8c8183a0ac271f1e52622e8`
- QEMU U-Boot SHA-256: `18c34ed87dd053ad012a0d3f8acef7445d40b0199c4da54ec04ba8cbba841710`
- DTB SHA-256: `1bcec907d28a9ede2c0020d9024de93640d2d0ba9bdfc5f09f20e81f4777a9eb`

## Runtime evidence

The guest reached the DRM desktop and the Firefox service. Independent GPU
markers passed before browser control:

```
FIREFOX_DRM_ACCESS device=renderD128 ... read=1 write=1
FIREFOX_DRM_EGL_PROBE ... renderer='virgl' gl='3.1 Mesa 25.0.7-2+deb13u1'
FIREFOX_DRM_GLXTEST ... RENDERER virgl ... DRM_RENDERDEVICE /dev/dri/renderD128
DEBIAN_DESKTOP_M4_DRM_X11_SCREENSHOT skipped=firefox-scene-gate
DEBIAN_DESKTOP_M4_READY user=asterinas display=:0
FIREFOX_DRM_PHASE name=new-session
```

Firefox's bounded cold-start window expired at the Marionette `NewSession`
phase. No `FIREFOX_DRM_SUPPORT`, `FIREFOX_DRM_GL`, screenshot, frame, or
recovery markers were emitted. The gate result is `passed=false` with
`insufficient Firefox acceleration evidence (need=2 have=none)`; the underlying
boundary is the NewSession timeout, not a software-renderer fallback.

## Classification

This is a new guest input and log boundary, so it supersedes neither the
historical r44/r47 receipts nor the E2 virgl proof. The E3 runtime is frozen
at this boundary; a further run requires a deliberate change to the Firefox
startup/Marionette environment or an explicit decision to extend the cold
NewSession budget.
