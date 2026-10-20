# QEMU Firefox DRM scene r24

r24 added a window-level JavaScript error hook before navigation. The current
HTML was present and the page exposed the normal timer API, but no script error
was reported and the scene state remained absent.

The full serial receipt is
[serial.log](2026-10-16-qemu-firefox-drm-scene-r24/serial.log).

```text
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=undefined done=0 samples=0 scripts=1 script_bytes=999 timeout_type=function errors=0 last_error=none
```

This confirms the remaining boundary is content-script execution policy or
context, rather than a syntax error. No renderer, screenshot, frame, or
recovery marker was produced; Task 6 remains incomplete.

