# QEMU Firefox DRM scene r29

r29 completed the fixed DOM scene and wrote the 1152x584 screenshot to guest
storage without an ext2 error. The gate then stopped because the Firefox MOZ
log contained no WebRender compositor evidence, so the strict renderer rule
failed closed before emitting a screenshot/frame/recovery receipt.

The full serial receipt is
[serial.log](2026-10-16-qemu-firefox-drm-scene-r29/serial.log).

```text
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=undefined done=0 samples=0 dom_marker=started dom_done=1 dom_samples=32 scripts=1 script_bytes=1361 timeout_type=function errors=0 last_error=none
FIREFOX_DRM_FAIL_REASON reason=Firefox_browser_log_has_no_WebRender_compositor_evidence
```

The next run moves the Firefox stdout and MOZ log files to writable `/tmp`
tmpfs so the renderer diagnostic itself is independent of ext2 writeback.
Task 6 remains incomplete until the log proves WebRender plus virgl and the
screenshot, frame, and restart records are emitted.
