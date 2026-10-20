# QEMU Firefox DRM scene r38

r38 used the bounded browser-diagnostic marker added to the gate and sent
Firefox's selected GL log categories to captured stderr. The page still
completed 32 DOM-backed samples, but the diagnostic now exposes the concrete
browser boundary:

```text
FIREFOX_DRM_BROWSER_DIAG ... JavaScript warning: ... Failed to create WebGL context: WebGL creation failed:
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=undefined done=0 samples=0 dom_marker=started dom_done=1 dom_samples=32 scripts=1 script_bytes=1990 timeout_type=function webgl_context=0 webgl_debug=0 errors=0 last_error=none
FIREFOX_DRM_FAIL_REASON reason=Firefox_WebGL_renderer_was_not_reported_context=0_debug=0
```

The run remains fail-closed and has no WebRender, virgl, screenshot, frame, or
restart receipt. The next bounded probe will add only the missing `render`
supplementary group to the scene service and record the render-node access
check. Static inspection shows `/dev/dri/renderD128` is `root:render 0660`,
while the current scene unit has no `SupplementaryGroups=render`; this is a
hypothesis to verify, not yet a runtime pass.
