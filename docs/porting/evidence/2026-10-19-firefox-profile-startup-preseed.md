# Firefox DRM profile startup preseed (2026-10-19)

Static comparison of the known browser-web launcher and the DRM scene launcher
showed that the DRM profile lacked the bounded-startup preferences already
used by the browser-web path. The dedicated DRM profile now disables Firefox
background discovery/update traffic and Safe Browsing settings while keeping
WebRender/WebGL enabled and leaving sandboxing unchanged.

Added preferences include captive-portal/connectivity services, region and
top-sites discovery, updates, system add-on updates, Safe Browsing remote
checks, and DoH (`network.trr.mode=5`). These changes affect only the disposable
scene profile; they are not Firefox package patches.

Validation: `test_firefox_drm_scene_gate.py` and related static checks pass;
no QEMU, Firefox, or physical board was started in this step.

