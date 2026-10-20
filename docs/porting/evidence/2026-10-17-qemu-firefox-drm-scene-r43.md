# QEMU Firefox DRM scene r43

## Result

This bounded QEMU A/B run added the ESR-specific WebGL escape route identified
from local source audit: `webgl.force-enabled=true` and
`MOZ_WEBGL_FORCE_EGL=1`. It remains **fail-closed**. Firefox now creates a
WebGL context, but the scene reports a software `llvmpipe` renderer, so the
strict `renderer=virgl` gate rejects the run before WebRender, screenshots,
frame measurements, or restart recovery.

The same unprivileged service first passed the independent GBM/EGL pixel probe
and Firefox's bundled `glxtest`:

```text
FIREFOX_DRM_ACCESS groups=992,1000 owner=0 group=992 mode=0660 read=1 write=1
FIREFOX_DRM_EGL_PROBE rc=0 bytes=125 text=PIXEL_PROBE ok=yes left=ffffff right=000000 expect=ffffff/000000 display=gbm renderer='virgl' gl='3.1 Mesa 25.0.7-2+deb13u1'
FIREFOX_DRM_GLXTEST rc=0 bytes=211 text=DRI_DRIVER virtio_gpu VENDOR Mesa RENDERER virgl VERSION 3.1 Mesa 25.0.7-2+deb13u1 TFP TRUE DRM_RENDERDEVICE /dev/dri/renderD128 MESA_VENDOR_ID 0x1af4 MESA_DEVICE_ID 0x1050 DDX_DRIVER modesetting; TEST_TYPE EGL
```

The fixed DOM scene completed its timer samples and reports context creation:

```text
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=undefined done=0 samples=0 dom_marker=started dom_done=1 dom_samples=32 scripts=1 script_bytes=1990 timeout_type=function webgl_context=1 webgl_debug=1 errors=0 last_error=none
FIREFOX_DRM_BROWSER_DIAG lines=2 truncated=0 text=JavaScript warning: file:///usr/share/asterinas/firefox-drm-scene.html, line 27: WEBGL_debug_renderer_info is deprecated in Firefox and will be removed. Please use RENDERER. | JavaScript warning: file:///usr/share/asterinas/firefox-drm-scene.html, line 27: WEBGL_debug_renderer_info is deprecated in Firefox and will be removed. Please use RENDERER.
FIREFOX_DRM_FAIL_REASON reason=Firefox_WebGL_renderer_is_not_virgl:_llvmpipe,_or_similar
```

This closes the earlier “no WebGL context” diagnostic boundary but does not
close the Task 6 acceleration gate: Firefox's own EGL/headless path is still
selecting software rendering while the separate GBM and `glxtest` paths select
virgl. No physical experiment was run.

## Reproducibility

- Serial log: `serial.log` (`f4b617658183d798c7e4d324fd1f07019f4d28cf1d6fd4140ac1b4617f1da3d9`)
- Gate source: `b4aa755de48e6a1e0fec3b9fc9abc2355e14b58067178203d668a0addbc15c32`
- Scene source: `fde6bed9e11dcaa4e988dee369cc75490293d1236f6df8fdc93481dd159ad04c`
- Service source: `81b6db6adf5afa481687a1f7bed449fe5239e6644fadfd9ba68801c563f2d584`
- Structured result: `result.json`
- QEMU only; no physical board access or hardware MMIO was performed.
