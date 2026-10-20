# E3 official Firefox policy-r6 final runtime boundary (2026-10-18)

This is the final bounded run after removing the erroneous
`MOZ_AVOID_OPENGL_ALTOGETHER=0` environment entry and extending the cold
startup budget to `NewSession=840s`, scene budget `900s`, and watchdog fallback
`1200s`.

## Passed preconditions

```
FIREFOX_DRM_ACCESS device=renderD128 ... read=1 write=1
FIREFOX_DRM_EGL_PROBE ... renderer='virgl' gl='3.1 Mesa 25.0.7-2+deb13u1'
FIREFOX_DRM_GLXTEST ... RENDERER virgl ... DRM_RENDERDEVICE /dev/dri/renderD128
DEBIAN_DESKTOP_M4_READY user=asterinas display=:0
```

## Final boundary

```
FIREFOX_DRM_PHASE name=new-session
FIREFOX_DRM_FAIL_TYPE type=TimeoutError
FIREFOX_DRM_FAIL_REASON reason=timed_out
```

No `FIREFOX_DRM_SUPPORT`, `FIREFOX_DRM_GL`, screenshot, frame, or recovery
record was emitted. The independent EGL/GLX probes prove the guest virgl path;
the remaining failure is official Firefox Marionette startup under this TCG
runtime. This is the final E3 receipt for the current official package/input
set; no further automatic rerun is permitted.

## Inputs

- rootfs SHA-256: `e869b18076c73a26e801b5286e49d79e337a6d98f7d56f251483b042a2218f9c`
- manifest SHA-256: `1cf1a60727e71ede66da01ce746cbd71551b30d347ee3fe09ae63653c99b6397`
- U-Boot SHA-256: `18c34ed87dd053ad012a0d3f8acef7445d40b0199c4da54ec04ba8cbba841710`
- DTB SHA-256: `1bcec907d28a9ede2c0020d9024de93640d2d0ba9bdfc5f09f20e81f4777a9eb`
