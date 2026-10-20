# QEMU Firefox DRM scene r50

Date: 2026-10-17 (Asia/Shanghai)

## Result

**FAIL CLOSED (diagnostic only).** This run used the fallback rootfs with two
temporary environment entries in `firefox-drm-scene.service`:

```text
Environment=MESA_LOADER_DRIVER_OVERRIDE=virpipe
Environment=GALLIUM_DRIVER=virpipe
```

The image contained an older copy of the scene gate than the current repository
gate, so this run is not comparable to r47/r51 and is not used as a Task 6
runtime receipt. It reached the DRM provider, Xorg socket, Marionette new
session, window sizing, and about:support, but Firefox did not expose a
compositing, WebRender, or renderer result before the bounded gate deadline:

```text
FIREFOX_DRM_SUPPORT_SOURCE bytes=2785 compositing=0 webrender=0 renderer=0
FIREFOX_DRM_FAIL_TYPE type=TimeoutError
FIREFOX_DRM_FAIL_REASON reason=timed_out
FIREFOX_DRM_FAIL r
```

The strict gate therefore stopped before screenshot, frame, and restart
acceptance. `virpipe` environment selection alone is not a Task 6 fix. The
current-gate rerun is recorded separately as r51.

## Evidence

- Serial log: `r50/serial.log`
- Serial SHA-256: `d9040f9104bc1d6721512531a23f667bea0234a7ed8d9c7e2200c9f4809b383f`
- Temporary service file SHA-256:
  `f84cc0e5e12565851dc89ad37e5b475bceff80852c14338ea7d467410297ebdc`
- QEMU: `asterinas/qemu-gl-env:20261014`, `virtio-gpu-gl-device`,
  `egl-headless`, `-nic none`
- Physical board: not used

The temporary rootfs was based on the previously used r47 fallback image and
was not published as a rootfs artifact. This receipt is a QEMU runtime result,
not native PowerVR evidence.
