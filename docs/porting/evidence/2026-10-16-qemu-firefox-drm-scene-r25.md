# QEMU Firefox DRM scene r25

r25 repeated the current local-scene gate with an explicit
`javascript.enabled=true` profile preference. The current gate, scene HTML,
and service sources were injected into the temporary rootfs and their hashes
were recorded in `result.json`. Marionette transport remained responsive and
the page reached `ready=complete`, but the page's own state object was still
absent. The JavaScript preference therefore did not close the boundary.

The full serial receipt is
[serial.log](2026-10-16-qemu-firefox-drm-scene-r25/serial.log).

```text
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=undefined done=0 samples=0 scripts=1 script_bytes=999 timeout_type=function errors=0 last_error=none
```

This is a fail-closed Task 6 result. It supplies no renderer, screenshot,
frame, or restart receipt and must not be treated as WebRender acceleration.
The next investigation must distinguish page-script execution from the
renderer path (for example by using an external script or a minimal script
that reports a direct DOM mutation), before changing Mesa or Xorg settings.
