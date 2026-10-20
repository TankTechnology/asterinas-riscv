# QEMU Firefox DRM scene r33

r33 kept the fixed 1280x800 window, measured 1152x584 content screenshot
contract, DOM-backed scene progress markers, and the read-only 1x1 WebGL
probe. The page completed its 32 deterministic timer samples, so navigation
and content-script execution are proven. WebGL context creation itself
returned null and the debug renderer extension was therefore unavailable:

```text
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=undefined done=0 samples=0 dom_marker=started dom_done=1 dom_samples=32 scripts=1 script_bytes=1990 timeout_type=function webgl_context=0 webgl_debug=0 errors=0 last_error=none
FIREFOX_DRM_FAIL_REASON reason=Firefox_WebGL_renderer_was_not_reported_context=0_debug=0
```

This is a fail-closed Task 6 boundary. It does not prove WebRender, virgl,
pixels, frame timing, or browser restart recovery. The serial receipt and
machine-readable result are archived beside this document. The next bounded
QEMU diagnostic should change one Firefox WebGL enablement variable at a time
(`webgl.force-enabled`/debug-renderer-info or the X11 EGL path), while keeping
the strict `renderer=virgl` requirement.
