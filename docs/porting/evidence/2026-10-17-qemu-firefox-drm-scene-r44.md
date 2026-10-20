# QEMU Firefox DRM scene r44

## Result

This bounded diagnostic A/B added only
`user_pref("gfx.blacklist.webgl-use-hardware", 1)` to the r43 profile. The
pre-Firefox GBM/EGL and `glxtest` checks still report `virgl`. Firefox creates a
WebGL context, but its WebRender path fails before the scene can be accepted:
Firefox sanitizes the raw `GL_RENDERER="virgl"` to `Generic Renderer`, then
reports failure to create the WebRender GL context and to connect the
WebRender bridge. The strict gate therefore remains fail-closed; no screenshot,
frame, or restart result is claimed.

```text
FIREFOX_DRM_EGL_PROBE rc=0 bytes=125 text=PIXEL_PROBE ok=yes left=ffffff right=000000 expect=ffffff/000000 display=gbm renderer='virgl' gl='3.1 Mesa 25.0.7-2+deb13u1'
FIREFOX_DRM_GLXTEST rc=0 bytes=211 text=DRI_DRIVER virtio_gpu VENDOR Mesa RENDERER virgl VERSION 3.1 Mesa 25.0.7-2+deb13u1 TFP TRUE DRM_RENDERDEVICE /dev/dri/renderD128 MESA_VENDOR_ID 0x1af4 MESA_DEVICE_ID 0x1050 DDX_DRIVER modesetting; TEST_TYPE EGL
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=undefined done=0 samples=0 dom_marker=started dom_done=1 dom_samples=32 scripts=1 script_bytes=1990 timeout_type=function webgl_context=1 webgl_debug=1 errors=0 last_error=none
FIREFOX_DRM_BROWSER_DIAG ... Couldn't sanitize GL_RENDERER "virgl" ... Failed GL context creation for WebRender ... Failed to connect WebRenderBridgeChild ...
FIREFOX_DRM_FAIL_REASON reason=Firefox_WebGL_renderer_is_not_virgl:_Generic_Renderer
```

This A/B closes the earlier uncertainty about `WebglUseHardware`: enabling the
feature changes Firefox's raw renderer path to `virgl`, but the current ESR
build rejects that renderer during sanitization and does not bring up
WebRender. The profile override is diagnostic-only and is not a product fix.
No physical experiment was run.

## Reproducibility

- Serial log: `serial.log` (`e35dcaa71c599e4a40956892124157978df2e85090d891f5a5c5a66feb0847fb`)
- Gate source: `af7a816b487f34c066eb3d15200311c530c3cb2cc2c09050470825a26479f82f`
- Scene source: `fde6bed9e11dcaa4e988dee369cc75490293d1236f6df8fdc93481dd159ad04c`
- Service source: `81b6db6adf5afa481687a1f7bed449fe5239e6644fadfd9ba68801c563f2d584`
- Diagnostic override: `gfx.blacklist.webgl-use-hardware=1` (temporary only)
- Structured result: `result.json`
- QEMU only; no physical board access or hardware MMIO was performed.
