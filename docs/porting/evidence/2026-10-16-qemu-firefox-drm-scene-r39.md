# QEMU Firefox DRM scene r39

r39 tested the concrete render-node permission hypothesis with one temporary
unit change: `SupplementaryGroups=render` was added to the scene service. The
gate also emitted the process groups and node access before spawning Firefox:

```text
FIREFOX_DRM_ACCESS groups=992,1000 owner=0 group=992 mode=0660 read=1 write=1
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=undefined done=0 samples=0 dom_marker=started dom_done=1 dom_samples=32 scripts=1 script_bytes=1990 timeout_type=function webgl_context=0 webgl_debug=0 errors=0 last_error=none
FIREFOX_DRM_BROWSER_DIAG ... WebglAllowWindowsNativeGl:false ... FEATURE_FAILURE_WEBGL_EXHAUSTED_DRIVERS
FIREFOX_DRM_FAIL_REASON reason=Firefox_WebGL_renderer_was_not_reported_context=0_debug=0
```

The process can read and write `root:render 0660` `/dev/dri/renderD128`, so the
missing supplementary group was a real configuration defect but not the sole
cause. The run remains fail-closed with no WebRender, virgl, screenshot, frame,
or restart receipt. The Firefox failure is now narrowed to GL driver option
exhaustion after node access succeeds; the permanent service fix and the next
probe must retain this access marker.
