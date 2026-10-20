# QEMU Firefox DRM scene r20

r20 replaced synchronous scene polling with one
`WebDriver:ExecuteAsyncScript` command. The script waits for the fixed scene's
own timer and has a 180-second script timeout inside the 600-second gate
budget. It ran only in QEMU with `egl-headless`, `virtio-gpu-gl-device`, and
`-nic none`.

The full serial receipt is
[serial.log](2026-10-16-qemu-firefox-drm-scene-r20/serial.log).

## Identity

- injected gate SHA-256:
  `4bbf7e4198766715e080c6edad4f847752d0570667d2ea7c13b87d4c583d0c18`
- QEMU binary SHA-256:
  `d867a49eda903ff33cdc9138b6f1b0596e94c8445b912a9a83c7337ddca1f31a`
- U-Boot SHA-256:
  `3e5ae37dcd7071a274780414bc37c74ac1df12b916d77532b290e4eb61a5fc80`
- serial SHA-256:
  `ebf1943a80d9681bdbc04743a9eed5fc0d21dbb2e6376874b55968e4afc2e2d9`

## Boundary

Firefox reached NewSession, window selection, and the transport probe:

```text
FIREFOX_DRM_TRANSPORT probe=pass
FIREFOX_DRM_PHASE name=scene
A_WEB_MARIONETTE_TRANSPORT ... command="WebDriver:ExecuteAsyncScript" ... send_complete=true
```

The async command never returned a response or a `FIREFOX_DRM_SCENE_STATE`
record before the outer 1300-second runner bound expired. No renderer,
screenshot, frame, or recovery marker was emitted. This is a fail-closed
runtime boundary showing that the Firefox content/scene execution path does
not complete even when the Marionette transport probe succeeds; it is not a
WebRender or virgl pass.

