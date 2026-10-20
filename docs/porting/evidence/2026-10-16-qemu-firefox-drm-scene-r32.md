# QEMU Firefox DRM scene r32

r32 added a read-only 1x1 WebGL context and required
`UNMASKED_RENDERER_WEBGL` to report a virgl renderer. The page script still
completed 32 DOM-backed samples, but the renderer value was empty:

```text
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=undefined done=0 samples=0 dom_marker=started dom_done=1 dom_samples=32 scripts=1 script_bytes=1776 timeout_type=function errors=0 last_error=none
FIREFOX_DRM_FAIL_REASON reason=Firefox_WebGL_renderer_was_not_reported
```

This is a fail-closed Firefox GL boundary. It does not claim WebRender or
virgl. The next run records whether WebGL context creation or the debug
renderer extension is missing before changing Firefox startup settings.
