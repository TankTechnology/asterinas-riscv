# QEMU Firefox DRM scene r34

r34 was a single-variable diagnostic derived from r33. It added only
`user_pref("webgl.force-enabled", true)` to the temporary Firefox profile;
the repository sources and the strict `renderer=virgl` acceptance rule were
unchanged. The local scene still completed 32 DOM-backed timer samples, but
Firefox returned no WebGL context and no debug renderer information:

```text
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=undefined done=0 samples=0 dom_marker=started dom_done=1 dom_samples=32 scripts=1 script_bytes=1990 timeout_type=function webgl_context=0 webgl_debug=0 errors=0 last_error=none
FIREFOX_DRM_FAIL_REASON reason=Firefox_WebGL_renderer_was_not_reported_context=0_debug=0
```

`webgl.force-enabled` alone did not close the GL boundary. This remains a
fail-closed Task 6 diagnostic and supplies no WebRender, virgl, pixel, frame,
or restart receipt. The next bounded probe should test the X11 EGL selection
path, still without relaxing the renderer or recovery contracts.
