# QEMU Firefox DRM scene r21

r21 tested the synchronous scene query after a 30-second settle period and a
2-second poll interval. Its state marker identified an artifact mistake in
the disposable image: the current gate was injected, but the scene file was
still the older fallback HTML.

The full serial receipt is
[serial.log](2026-10-16-qemu-firefox-drm-scene-r21/serial.log).

## Boundary

The gate reached the scene phase and reported:

```text
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=undefined done=0 samples=0
```

The stale fallback scene calls `canvas.getContext('2d')` before assigning
`window.__asterinasDrmScene`. The `scene_type=undefined` result is consistent
with that early canvas failure in this headless guest and explains why runs
that did not inject the current HTML never reached `done`; the next run must
inject and hash the current HTML explicitly. The run was stopped after this
diagnostic boundary, before any renderer, screenshot, frame, or recovery
claim. It is not a Task 6 pass.

