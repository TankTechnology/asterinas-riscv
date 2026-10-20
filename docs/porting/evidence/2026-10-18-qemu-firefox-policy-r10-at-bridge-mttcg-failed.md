# E3 official Firefox policy-r10 environment hypothesis (2026-10-18T14:30:53Z)

This bounded run tested two host-side changes together: explicit MTTCG
(`-accel tcg,thread=multi`) and `NO_AT_BRIDGE=1` in the official Debian
Firefox process. It used a separate derived rootfs and did not access the
physical board.

## Ordered evidence

```
FIREFOX_DRM_EGL_PROBE rc=timeout ...
FIREFOX_DRM_GLXTEST rc=timeout ...
FIREFOX_DRM_PHASE name=new-session
FIREFOX_DRM_BROWSER_DIAG ... RenderThread::InitDeviceTask is slow: 3348 ... NewRenderer::Run is slow: 10254
FIREFOX_DRM_FAIL_REASON reason=timed_out
```

The EGL/GLX preflight did not complete in its 20-second bounds, and Firefox
still failed before a Marionette response. No Firefox support, scene, fixed
screenshot, frame, or recovery marker was produced. This falsifies the
`NO_AT_BRIDGE` hypothesis as a sufficient fix; the environment entry must not
be carried into the final delivery path. The durable lifecycle ended as
`status=failed`, `phase=finished`.

## Artifacts and inputs

- serial: `target/e3-render-policy-r1/qemu-r4-export/firefox-drm-recovery.serial.log` (SHA-256 `536678f17ea4cf0e37eb7e36f47e4bc965596fd1665cd5956d63bdc3ede32fd2`)
- result: `target/e3-render-policy-r1/qemu-r4-export/result.json` (SHA-256 `b4707194caae746cb43fdf03d38fa73c97965391e13643da14811e52912e33a4`)
- lifecycle: `target/e3-render-policy-r1/qemu-r4-export/lifecycle.json` (SHA-256 `6e901e74ddd26f142870eac9e107574b333fcb1af3108b22a68f73faff0ae08e`)
- root image SHA-256: `943b332e7a71b1df84aba6f9d1855985ef45aa142026a2997be5656be6c36ca3`
- root manifest SHA-256: `27a41d37097fb5f090dc61ee06c6b63b63565b59fe06f2cc147120165662ecc9`
- manifest.lock SHA-256: `68645b154bb876db055a7e49b32a55b0f26e3a4bab44e89be4b2223f7abd39f8`
- kernel SHA-256: `cabc01961888055d64b5f0d59a1a15a1d797e95e969fca7de7d61dfe2429328d`
- DTB SHA-256: `1bcec907d28a9ede2c0020d9024de93640d2d0ba9bdfc5f09f20e81f4777a9eb`
- Stage1 SHA-256: `ce7fb996e9a6ac48160d81c29347a68d88e99204adcc147051df76f645031c84`

## Decision

`official_firefox_scene=blocked`. The next investigation boundary is the
Asterinas/virtio-gpu render-thread scheduling or ioctl path, not another
Firefox environment variable. No automatic rerun of this route is authorized.
