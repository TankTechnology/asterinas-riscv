# QEMU Firefox DRM scene r35

r35 was a single-variable diagnostic derived from r33. It added only
`user_pref("webgl.disabled", false)` to the temporary Firefox profile. The
local DOM scene again completed 32 timer samples, but the WebGL context was
still unavailable:

```text
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=undefined done=0 samples=0 dom_marker=started dom_done=1 dom_samples=32 scripts=1 script_bytes=1990 timeout_type=function webgl_context=0 webgl_debug=0 errors=0 last_error=none
FIREFOX_DRM_FAIL_REASON reason=Firefox_WebGL_renderer_was_not_reported_context=0_debug=0
```

The explicit disabled preference did not close the GL boundary. This remains
a fail-closed Task 6 diagnostic and supplies no WebRender, virgl, pixel, frame,
or restart receipt. The next investigation should inspect Firefox's actual GL
startup path (X11 EGL/GLX and GPU-process diagnostics), rather than adding
more profile toggles.
