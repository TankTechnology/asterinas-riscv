# QEMU Firefox DRM scene r19

This is a bounded follow-up to r18 with one guest change: scene polling was
reduced from 100 ms to 500 ms so Marionette does not continuously occupy the
content thread under TCG. It ran only in QEMU with `egl-headless`,
`virtio-gpu-gl-device`, and `-nic none`.

The full serial receipt is
[serial.log](2026-10-16-qemu-firefox-drm-scene-r19/serial.log).
The compact runner result is in
[result.json](2026-10-16-qemu-firefox-drm-scene-r19/result.json).

## Identity

- injected gate SHA-256:
  `45ce89ce61b6b1752874eea248f1e38e84fb058a26f64b8cb8698c03527b1d68`
- disposable root image SHA-256:
  `50454a9fef8eca1fe1cf445602109d920827b97d11390c975786116d5fd61c6b`
- QEMU binary SHA-256:
  `d867a49eda903ff33cdc9138b6f1b0596e94c8445b912a9a83c7337ddca1f31a`
- U-Boot SHA-256:
  `3e5ae37dcd7071a274780414bc37c74ac1df12b916d77532b290e4eb61a5fc80`
- serial SHA-256:
  `bff3ef3c3980c0050553064501e5b1ef431502f7964001f726ffdf08aa1158dd`

## Boundary

The guest reached the same healthy setup boundary as r18:

```text
FIREFOX_DRM_PHASE name=new-session
FIREFOX_DRM_PHASE name=window-rect
FIREFOX_DRM_PHASE name=window-context
FIREFOX_DRM_PHASE name=transport-probe
FIREFOX_DRM_TRANSPORT probe=pass
FIREFOX_DRM_SUPPORT_FALLBACK reason=privileged_page_deferred
FIREFOX_DRM_PHASE name=scene
```

The scene queries returned complete Marionette frames, but the run ended
while the scene was still being polled. The runner observed QEMU exit before
the gate emitted either a success or a `FIREFOX_DRM_FAIL_*` terminal marker;
the last guest record is an `ExecuteScript` response for the scene state.
There is no renderer, screenshot, frame summary, or recovery marker. This is
an **incomplete/fail-closed runtime**, not a Task 6 pass. The exact shutdown
cause is not present in the serial tail; no physical hardware was involved.

