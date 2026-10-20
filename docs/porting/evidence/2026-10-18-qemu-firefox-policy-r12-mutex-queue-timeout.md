# E3 lock-safe virtio-gpu queue r12 (2026-10-18T15:21:49Z)

This one bounded runtime used the lock-safe queue implementation: the virtio
gpu control and cursor queues use the existing wait-queue `Mutex`, and bounded
completion polling yields only while a scheduler task exists. The original
official Debian rootfs and GL-capable QEMU were used; no physical board was
accessed.

## Ordered evidence

```
FIREFOX_DRM_EGL_PROBE rc=timeout ...
FIREFOX_DRM_GLXTEST ... RENDERER virgl ... DRM_RENDERDEVICE /dev/dri/renderD128
DEBIAN_DESKTOP_M4_READY user=asterinas display=:0
FIREFOX_DRM_PHASE name=new-session
FIREFOX_DRM_BROWSER_DIAG ... NewRenderer::Run is slow: 4932
FIREFOX_DRM_FAIL_REASON reason=timed_out
```

There was no `MIGHT_SLEEP_VIOLATION` or kernel panic, confirming the lock-safe
change removed the r11 safety failure. Firefox still does not return a
Marionette session; no scene, screenshot, frame, or recovery marker was
produced. The remaining E3 boundary is Firefox/virgl render-thread progress,
not the previous lock violation.

## Artifacts and inputs

- serial: `target/e3-render-policy-r1/qemu-r6-export/firefox-drm-recovery.serial.log` (SHA-256 `9fb494a598b04107b35d187ed2e230f422d6351a24871750e8c09a3722219a41`)
- result: `target/e3-render-policy-r1/qemu-r6-export/result.json` (SHA-256 `aa9158bb720471f96611808cea9745a5ca06813e5197149a992ff115e17f8124`; `passed=false`)
- lifecycle: `target/e3-render-policy-r1/qemu-r6-export/lifecycle.json` (SHA-256 `c3c74d0c7ca138597954537d1dd9bd7fa944fc1071ce67285af446ca05461bb6`; terminal failed)
- kernel SHA-256: `1660a4606f88d3131ad030a489b978a0f3a24fed4d17beb6db31c2829029a02d`
- root image SHA-256: `aa26ef744b3c0e8568bbbaafbbfe300199ef14e354a698410b8b73bab4b80ad7`
- manifest.lock SHA-256: `68645b154bb876db055a7e49b32a55b0f26e3a4bab44e89be4b2223f7abd39f8`

The source change is compile-checked for riscv64 with and without `ktest`.
No claim of Firefox acceleration is made.
