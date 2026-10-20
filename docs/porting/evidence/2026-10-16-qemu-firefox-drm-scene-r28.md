# QEMU Firefox DRM scene r28

r28 kept the DOM-backed scene and the explicit 1152x584 content viewport.
The page completed 32 samples, but writing the screenshot under the
user-owned profile directory returned `EIO` from the guest filesystem.

The full serial receipt is
[serial.log](2026-10-16-qemu-firefox-drm-scene-r28/serial.log).

```text
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=undefined done=0 samples=0 dom_marker=started dom_done=1 dom_samples=32 scripts=1 script_bytes=1361 timeout_type=function errors=0 last_error=none
FIREFOX_DRM_FAIL_REASON reason=[Errno_5]_Input/output_error:_'/home/asterinas/.mozilla/asterinas-firefox-drm-scene/firefox-drm-scene.png'
```

This is a fail-closed rootfs-write boundary, not a renderer pass. No
screenshot hash, renderer, frame, or restart receipt is claimed. The next run
uses the guest's writable `/tmp` tmpfs for this disposable screenshot.
