# Firefox EGL device-selection audit (QEMU Task 6)

This is a source-level diagnosis supporting the bounded r43 runtime receipt;
it is not a runtime acceleration receipt and does not count as a WebRender or
physical-board pass.

## Root cause boundary

The r43 run used both `webgl.force-enabled=true` and
`MOZ_WEBGL_FORCE_EGL=1`. That changed the result from no WebGL context to a
WebGL context, but the scene reported `llvmpipe`. The independent GBM probe
and Firefox `glxtest` in the same run reported `virgl` on
`/dev/dri/renderD128`.

The local Firefox ESR 140.15 source explains the split:

* `GLLibraryEGL::GetAndInitDeviceDisplay` reads the internal
  `gfxVars::DrmRenderDevice()` and matches EGL's
  `EGL_DRM_RENDER_NODE_FILE_EXT` exactly (`GLLibraryEGL.cpp:173-210`). This
  is the path that can select `/dev/dri/renderD128`.
* `CreateDisplayLocked` first selects a software EGL device when
  `WebglUseHardware()` is false (`:943-949`). Otherwise it only tries the
  explicit device path when `gdk_display_get_default()` is null
  (`:950-956`). Under the X11/GTK desktop this condition is false, so the
  fallback is `eglGetDisplay(EGL_DEFAULT_DISPLAY)` (`:967-970`).
* `GLContextProviderEGL::CreateHeadless` only switches to the software
  display when `FORBID_HARDWARE` is set; otherwise it calls
  `DefaultEglDisplay` (`GLContextProviderEGL.cpp:1243-1254`). The
  `MOZ_WEBGL_FORCE_EGL` switch selects this provider but does not force the
  DRM render-node display.

The source snapshots used for this audit are identified by SHA-256:

```text
39e2cbb4854daff33903126a78900aca61e8312b3c991bd6b68427f5cca721a2  GLLibraryEGL.cpp
7cab444255b31f7c4aee92f8297a19b6abe3bbf97b9f6d56d90e0ce15611e213  GLContextProviderEGL.cpp
```

## Consequence and next gate

Repeating the two already-tested switches or another standalone Mesa probe is
not useful. The next single bounded gate must expose and verify the Firefox
feature state (`FEATURE_WEBGL_USE_HARDWARE` / `WebglUseHardware`) and the
selected EGL display. A successful run must show Firefox itself reporting
`renderer=virgl`; until then the Task 6 WebRender, screenshot, frame-time, and
restart gates remain closed. No physical experiment is authorized by this
audit.

## r44 sanitizer boundary

r44 set the feature status override and reached raw `GL_RENDERER="virgl"`, but
the browser log contains `Couldn't sanitize GL_RENDERER "virgl"`, followed by
`Failed GL context creation for WebRender`. Mozilla's sanitizer maps unknown
renderer strings to `Generic Renderer`; its device-replacement table accepts
known families such as llvmpipe, Radeon, Intel, Mali, PowerVR, Vivante and
VideoCore, but has no `virgl` entry in the audited ESR path. Therefore merely
relaxing the blocklist is insufficient: the browser binary must recognize the
virgl string (or the Mesa driver must expose a recognized, truthful renderer
identity) before WebRender can be accepted.

This is a compatibility finding, not permission to weaken the gate or spoof a
renderer. Any future diagnostic spoof must remain separate from the product
receipt and must still be paired with raw virgl and pixel evidence.

## r46/r47 runtime boundary

r46 ran the formal profile and recorded `renderer=llvmpipe,_or_similar` even
though the independent GBM/EGL and `glxtest` probes reported `virgl`. r47
opened the WebGL hardware feature and preferred GLES, but the scene then
recorded `Generic_Renderer`; its browser diagnostic still contained
`Couldn't sanitize GL_RENDERER "virgl"` and `Failed GL context creation for
WebRender: 0`. Thus the GLES preference alone is not a fix, and neither run
provides a WebRender, screenshot, frame, or restart pass.
