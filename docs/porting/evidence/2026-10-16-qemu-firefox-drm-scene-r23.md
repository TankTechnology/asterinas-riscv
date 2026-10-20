# QEMU Firefox DRM scene r23

r23 added redacted script diagnostics to the scene state. It injected the
current gate, HTML, and service sources and reached the scene after a 30-second
settle period.

The full serial receipt is
[serial.log](2026-10-16-qemu-firefox-drm-scene-r23/serial.log).

## State evidence

```text
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=undefined done=0 samples=0 scripts=1 script_bytes=999 timeout_type=function
```

The browser sees exactly one script element and the expected 999-byte current
script, and `setTimeout` exists. The page script nevertheless did not create
`window.__asterinasDrmScene`. The run was stopped after this bounded diagnostic
and produced no renderer, screenshot, frame, or recovery record. This is not a
Task 6 pass.

