# QEMU Firefox DRM scene r42

## Result

This bounded QEMU run used the current scene gate, current scene HTML, and
`SupplementaryGroups=render` service. It is **fail-closed**: Firefox did not
create a WebGL context, so no WebRender, screenshot, frame-rate, or restart
claim is made.

The same unprivileged service first passed both the user-level GBM/EGL pixel
probe and Firefox's bundled `glxtest`:

```text
FIREFOX_DRM_ACCESS groups=992,1000 owner=0 group=992 mode=0660 read=1 write=1
FIREFOX_DRM_EGL_PROBE rc=0 bytes=125 text=PIXEL_PROBE ok=yes left=ffffff right=000000 expect=ffffff/000000 display=gbm renderer='virgl' gl='3.1 Mesa 25.0.7-2+deb13u1'
FIREFOX_DRM_GLXTEST rc=0 bytes=211 text=DRI_DRIVER virtio_gpu VENDOR Mesa RENDERER virgl VERSION 3.1 Mesa 25.0.7-2+deb13u1 TFP TRUE DRM_RENDERDEVICE /dev/dri/renderD128 MESA_VENDOR_ID 0x1af4 MESA_DEVICE_ID 0x1050 DDX_DRIVER modesetting; TEST_TYPE EGL
```

The local scene completed its DOM timer samples (`dom_done=1`,
`dom_samples=32`), but Firefox's WebGL creation failed:

```text
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=undefined done=0 samples=0 dom_marker=started dom_done=1 dom_samples=32 scripts=1 script_bytes=1990 timeout_type=function webgl_context=0 webgl_debug=0 errors=0 last_error=none
FIREFOX_DRM_BROWSER_DIAG lines=12 truncated=0 text=... WebglAllowWindowsNativeGl:false restricts context creation on this system. () ... Exhausted GL driver options. (FEATURE_FAILURE_WEBGL_EXHAUSTED_DRIVERS)
FIREFOX_DRM_FAIL_REASON reason=Firefox_WebGL_renderer_was_not_reported_context=0_debug=0
```

## Reproducibility

- Serial log: `serial.log` (93e7df9439119c9abc07c643cc4f35fec34c4471d3f4ddca281d5a7865c71c79)
- Gate source: `67be178080ef0774f084485581438cb89991d1dbdfc8654cf43764ab6bd9f215f`
- Scene source: `fde6bed9e11dcaa4e988dee369cc75490293d1236f6df8fdc93481dd159ad04c`
- Service source: `81b6db6adf5afa481687a1f7bed449fe5239e6644fadfd9ba68801c563f2d584`
- Structured result: `result.json`
- QEMU only; no physical board access or hardware MMIO was performed.
