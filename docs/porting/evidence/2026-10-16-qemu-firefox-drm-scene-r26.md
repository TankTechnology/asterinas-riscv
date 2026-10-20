# QEMU Firefox DRM scene r26

r26 ran the DOM-backed scene diagnostic. It proves that the local page script
executes and completes despite the Marionette `window` state being absent
across commands: the page reached `ready=complete`, set the DOM marker, and
recorded 32 samples.

The full serial receipt is
[serial.log](2026-10-16-qemu-firefox-drm-scene-r26/serial.log).

```text
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=undefined done=0 samples=0 dom_marker=started dom_done=1 dom_samples=32 scripts=1 script_bytes=1361 timeout_type=function errors=0 last_error=none
FIREFOX_DRM_FAIL_REASON reason=Firefox_screenshot_geometry_is_(1152,_584),_expected_1280x800
```

This closes the content-script execution diagnostic, but the gate remains
fail-closed because its screenshot contract still expected the 1280x800 outer
window rather than the 1152x584 content viewport returned by Marionette. No
renderer, screenshot hash, frame, or restart receipt is claimed from r26.
