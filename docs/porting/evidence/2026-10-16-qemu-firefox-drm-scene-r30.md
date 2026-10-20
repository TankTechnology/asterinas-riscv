# QEMU Firefox DRM scene r30

r30 moved both Firefox logs to guest `/tmp` and retained the successful DOM
scene and screenshot path. The strict renderer check still found no WebRender
compositor evidence. The gate did not emit a screenshot/frame/restart receipt
because renderer proof is a prerequisite.

The full serial receipt is
[serial.log](2026-10-16-qemu-firefox-drm-scene-r30/serial.log).

```text
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=undefined done=0 samples=0 dom_marker=started dom_done=1 dom_samples=32 scripts=1 script_bytes=1361 timeout_type=function errors=0 last_error=none
FIREFOX_DRM_FAIL_REASON reason=Firefox_browser_log_has_no_WebRender_compositor_evidence
```

The next run adds a bounded keyword/size/hash diagnostic for the MOZ log so
that a missing file can be distinguished from a real software-renderer log.
