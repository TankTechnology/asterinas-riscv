# QEMU Firefox DRM scene r37

r37 redirected Firefox's MOZ log to its captured standard error and enabled
bounded GL/Widget/Layers/WebRender/WebGL categories. The run still completed
the DOM scene (32 samples) but WebGL context creation remained unavailable:

```text
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=undefined done=0 samples=0 dom_marker=started dom_done=1 dom_samples=32 scripts=1 script_bytes=1990 timeout_type=function webgl_context=0 webgl_debug=0 errors=0 last_error=none
FIREFOX_DRM_BROWSER_LOG text=... D/Widget ... nsWindow::Create() ...
FIREFOX_DRM_FAIL_REASON reason=Firefox_WebGL_renderer_was_not_reported_context=0_debug=0
```

No strict renderer, screenshot, frame, or restart receipt exists. The bounded
log redirection proves Firefox logging reaches its stderr capture, but the
current marker only preserves a prefix/suffix summary; a follow-up diagnostic
must emit a small filtered GL initialization summary before the browser log is
archived.
