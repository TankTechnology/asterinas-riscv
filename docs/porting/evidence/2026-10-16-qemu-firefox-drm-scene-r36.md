# QEMU Firefox DRM scene r36

r36 was a single-variable startup-path diagnostic derived from r33. It added
only `MOZ_X11_EGL=1` to Firefox's environment, leaving profile preferences,
QEMU, Xorg, and the strict renderer gate unchanged. The page still completed
32 DOM-backed timer samples, but WebGL context creation remained unavailable:

```text
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=undefined done=0 samples=0 dom_marker=started dom_done=1 dom_samples=32 scripts=1 script_bytes=1990 timeout_type=function webgl_context=0 webgl_debug=0 errors=0 last_error=none
FIREFOX_DRM_FAIL_REASON reason=Firefox_WebGL_renderer_was_not_reported_context=0_debug=0
```

The X11 EGL selection variable alone did not close the GL boundary. This is a
fail-closed Task 6 diagnostic and supplies no WebRender, virgl, pixel, frame,
or restart receipt. The next diagnostic should collect bounded Firefox
GPU-process/GL initialization evidence, rather than adding another blind
profile toggle.
