# QEMU Firefox DRM scene r17 — 2026-10-16

This is one bounded QEMU-only run using the GL-capable QEMU backend
(`egl-headless` + `virtio-gpu-gl-device`) and no NIC. It did not access the
physical board. The disposable rootfs contains the current Firefox scene gate
and fixed DOM/CSS scene; the source and image hashes are in `result.json`.

## Ordered boundary

The desktop provider reached Xorg and the gate started Firefox. Firefox
published its Marionette listening socket, but the first `WebDriver:NewSession`
command did not return before the gate's command deadline:

```text
FIREFOX_DRM_PHASE name=profile
FIREFOX_DRM_PHASE name=spawn-first
FIREFOX_DRM_PHASE name=connect
FIREFOX_DRM_PHASE name=connect-state
FIREFOX_DRM_PROCESS state=running
FIREFOX_DRM_PHASE name=new-session
FIREFOX_DRM_BROWSER_LOG text=... Marionette INFO Listening on port 2828 ...
FIREFOX_DRM_FAIL_TYPE type=TimeoutError
FIREFOX_DRM_FAIL_REASON reason=timed_out
```

No `FIREFOX_DRM_SUPPORT`, `FIREFOX_DRM_GL`, screenshot, frame, or recovery
marker was emitted. This is a fail-closed Firefox session boundary; it is not
WebRender or virgl evidence. The previous r16 run had reached
`NewSession`/`SetWindowRect` before stalling in scene execution; r17 shows that
cold session creation is not deterministic in the same QEMU/Firefox workload.
The next investigation must instrument Firefox startup/session timing before
attempting the renderer gate again.

Raw serial and structured result are stored beside this document.
