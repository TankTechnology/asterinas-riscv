# QEMU Firefox DRM scene r27

r27 retained the r26 DOM-backed scene contract and changed the screenshot
contract to the measured 1152x584 content viewport of the fixed 1280x800
browser window. The scene reached 32 DOM-backed samples and passed the
geometry check, but the unprivileged gate could not write the screenshot to
the root-owned `browser-web-evidence` directory.

The full serial receipt is
[serial.log](2026-10-16-qemu-firefox-drm-scene-r27/serial.log).

```text
FIREFOX_DRM_SCENE_STATE location=scene ready=complete scene_type=undefined done=0 samples=0 dom_marker=started dom_done=1 dom_samples=32 scripts=1 script_bytes=1361 timeout_type=function errors=0 last_error=none
FIREFOX_DRM_FAIL_REASON reason=[Errno_13]_Permission_denied:_'/home/asterinas/browser-web-evidence/firefox-drm-scene.png'
```

This remains fail-closed. No screenshot hash, renderer, frame, or restart
receipt is claimed from r27. The next run stores the temporary PNG below the
profile directory created by the same user that runs the gate.
