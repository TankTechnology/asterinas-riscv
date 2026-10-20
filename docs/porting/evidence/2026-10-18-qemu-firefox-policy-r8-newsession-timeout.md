# E3 official Firefox policy-r8 runtime boundary (2026-10-18T13:44:45Z)

This is the one bounded runtime after the lifecycle publication fix. It used
the official Debian trixie riscv64 Firefox package and the GL-capable QEMU
path; no physical board was accessed.

## Ordered guest evidence

The serial transcript contains, in order:

```
FIREFOX_DRM_ACCESS device=renderD128 ... read=1 write=1
FIREFOX_DRM_EGL_PROBE ... renderer='virgl' gl='3.1 Mesa 25.0.7-2+deb13u1'
FIREFOX_DRM_GLXTEST ... RENDERER virgl ... DRM_RENDERDEVICE /dev/dri/renderD128
DEBIAN_DESKTOP_M4_READY user=asterinas display=:0
FIREFOX_DRM_PHASE name=new-session
FIREFOX_DRM_FAIL_TYPE type=TimeoutError
FIREFOX_DRM_FAIL_REASON reason=timed_out
```

No `FIREFOX_DRM_SUPPORT`, `FIREFOX_DRM_GL`, fixed-scene screenshot,
frame sample, or Firefox recovery marker was emitted. The independent EGL/GLX
probes prove the guest virgl path, but the official Firefox Marionette session
still does not complete. The receipt is therefore blocked at the application
boundary; it is not a claim that virgl failed.

## Durable host artifacts

- `lifecycle.json`: `status=failed`, `phase=finished`, `reason=quit`;
  SHA-256 `087dd61c5b88fdd532f8e6f8588025651d8da2eccf74b9e8d104a607d5a5bc70`.
- `result.json`: `passed=false`; SHA-256 `2c0e9c693a043f362354bb601b06861a8acf834f201e86f504c2817bc596f16a`.
- Serial transcript: `target/e3-render-policy-r1/qemu-r2-export/firefox-drm-recovery.serial.log`; SHA-256 `d20e7620e755f0f78ef3221c4e6368f3966a5af282daa800e7c6ec08d80a3e4e`.
- Output directory: `target/e3-render-policy-r1/qemu-r2/` (the export above
  contains only small readable copies; the original run-private root remains
  unchanged).

## Inputs

- manifest.lock SHA-256: `68645b154bb876db055a7e49b32a55b0f26e3a4bab44e89be4b2223f7abd39f8`
- QEMU executable: transient custom GL build; no retained canonical hash.
- kernel SHA-256: `cabc01961888055d64b5f0d59a1a15a1d797e95e969fca7de7d61dfe2429328d`
- root image SHA-256: `aa26ef744b3c0e8568bbbaafbbfe300199ef14e354a698410b8b73bab4b80ad7`
- DTB SHA-256: `1bcec907d28a9ede2c0020d9024de93640d2d0ba9bdfc5f09f20e81f4777a9eb`
- Stage1 SHA-256: `ce7fb996e9a6ac48160d81c29347a68d88e99204adcc147051df76f645031c84`
- scene HTML SHA-256: `fde6bed9e11dcaa4e988dee369cc75490293d1236f6df8fd8c93481dd159ad04c`

## Gate decision

`official_firefox_scene=blocked`: GPU evidence is present, while the required
Firefox application evidence and recovery are absent. No automatic rerun is
permitted for this same input set.
