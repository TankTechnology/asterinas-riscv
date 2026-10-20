# QEMU DRM desktop r10 (2026-10-19)

This is the first complete DRM desktop runtime after fixing host launch
handoff. The whole gate ran inside the fixed `asterinas/qemu-gl-env:20261014`
container so QEMU, the guest fd paths, `/tmp`, Mesa EGL, and `/dev/dri` shared
one namespace. It used the retained official Debian rootfs, kernel, DTB,
Stage1, and U-Boot inputs.

## Gate result

`desktop_m4_drm_gate.py` exited `0`; `result.json` reports `passed: true` and
`reason: pass`. The guest reached the DRM desktop marker and the lifecycle
closed cleanly.

Ordered runtime markers include:

```text
DEBIAN_DESKTOP_M4_DRM_CONFIG driver=modesetting kmsdev=/dev/dri/card0 fbdev=none
DEBIAN_DESKTOP_M4_DRM_SYSFS card=226:0 render=226:128 driver=virtio_gpu
DEBIAN_DESKTOP_M4_DRM_NODES card=/dev/dri/card0 render=/dev/dri/renderD128
DEBIAN_DESKTOP_M4_XORG provider=drm display=:0 renderer=virgl
PIXEL_PROBE ok=yes left=ffffff right=000000 expect=ffffff/000000 display=gbm renderer='virgl' gl='3.1 Mesa 25.0.7-2+deb13u1'
DEBIAN_DESKTOP_M4_DRM_X11_SCREENSHOT width=1280 height=800 bpp=32 stride=5120 bytes=4096000 sha256=a17336696bd7287d60a8087ea0be918418555bedfbcc5dc01fafd44370e9304f4
DEBIAN_DESKTOP_M4_READY user=asterinas display=:0
```

This is a QEMU DRM desktop proof, not a physical-board or Firefox WebRender
proof. The serial log also records the existing `systemd-sysctl.service`
warning; it did not prevent the gate's required desktop markers.

## Bound inputs and artifacts

```text
kernel  1660a4606f88d3131ad030a489b978a0f3a24fed4d17beb6db31c2829029a02d
rootfs  3e0f6c4ebc373ef3d67ec4e27f4b99a1ce93e246ca0efa6e859dbe0f1bcdb901
dtb     1bcec907d28a9ede2c0020d9024de93640d2d0ba9bdfc5f09f20e81f4777a9eb
stage1  ce7fb996e9a6ac48160d81c29347a68d88e99204adcc147051df76f645031c84
u-boot  18c34ed87dd053ad012a0d3f8acef7445d40b0199c4da54ec04ba8cbba841710
serial  f6d7e68e281e6d7a459ebc9d720a942831d45817ff81efeeaa5a8e8d88c465ff
result  469e2df4b0eb52f6ec65e500f3c2556ef8d39764e9221c4e6bb19004757c476f
lifecycle dfe6a48446ed475be7281c3798008a8b4186b5a47b31308a15752368fec3f9d2
final writable root 1bc2fd8c5b163003e935535c44beb2a682fb78f420aa92aa03b8ed32d6c3e7c5
```

Artifacts are retained as a group at
`/mnt/mydisk/qemu-desktop-m4-r10-container/`.

