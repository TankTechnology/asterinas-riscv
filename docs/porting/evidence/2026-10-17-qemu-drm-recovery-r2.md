# QEMU DRM desktop recovery gate r2

Date: 2026-10-17 (Asia/Shanghai)

## Result

**PASS.** The current Stage1 delimiter-fix initramfs and current desktop root
image completed one DRM modesetting desktop epoch and then returned to a fresh
OpenSBI/U-Boot epoch. The gate result is `passed=true, reason=pass`.

The guest receipt proves the DRM-only handoff and a virgl renderer:

```text
DEBIAN_DESKTOP_M4_DRM_FBDEV dependency=none action=skipped
DEBIAN_DESKTOP_M4_DRM_CONFIG driver=modesetting kmsdev=/dev/dri/card0 fbdev=none
DEBIAN_DESKTOP_M4_DRM_SYSFS card=226:0 render=226:128 driver=virtio_gpu
DEBIAN_DESKTOP_M4_DRM_NODES card=/dev/dri/card0 render=/dev/dri/renderD128
DEBIAN_DESKTOP_M4_XORG provider=drm display=:0 renderer=virgl
PIXEL_PROBE ok=yes left=ffffff right=000000 expect=ffffff/000000 display=gbm renderer='virgl' gl='3.1 Mesa 25.0.7-2+deb13u1'
DEBIAN_DESKTOP_M4_DRM_X11_SCREENSHOT width=1280 height=800 bpp=32 stride=5120 bytes=4096000 sha256=b00063d6eadf9747639a6251864c6cbe9cafa38c4939c1eac0c074a715e265de
DEBIAN_DESKTOP_M4_READY user=asterinas display=:0
OpenSBI v1.7
U-Boot 2026.07 ...
```

The recovery boot argument contains exactly one bounded timer:
`asterinas.reboot_after=180`. An earlier attempt used two timers and the
shorter 60-second value fired before the DRM desktop marker; the gate now
replaces the inherited 300-second timer instead of appending another one.

## Boundary and receipt

This is QEMU-only, with `virtio-gpu-gl-device`, `egl-headless`, `--nic none`,
and `/dev/dri/renderD129` passed to the GL-capable container. It is not
physical-board or Firefox WebRender evidence.

- Result: `/mnt/mydisk/asterinas-gates/qemu-drm-recovery-20261017-original-gl-r2/result.json`
- Serial: `/mnt/mydisk/asterinas-gates/qemu-drm-recovery-20261017-original-gl-r2/desktop-m4-drm-recovery.serial.log`
- Result SHA-256: `9b49b200c4f6f6d16132b92e4abb5539d5d74caec7f32c31d3c6caac2d9d3d6b`
- Serial SHA-256: `dc8ef131cb3263a32c4697c30b84afe0a5c3a270b47539807906953ec13f2552`

