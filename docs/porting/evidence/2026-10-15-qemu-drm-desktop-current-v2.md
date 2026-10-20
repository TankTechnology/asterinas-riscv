# QEMU DRM desktop gate with current X11/EGL probes

Date: 2026-10-15 (Asia/Shanghai)

This run used the GL-enabled QEMU 10.2.1 binary from
`asterinas/qemu-gl-env:20261014`:

`/root/qemu-gl-src/build/qemu-system-riscv64`

The immutable `desktop-m4-x11` rootfs was left unchanged. A private
single-purpose development overlay replaced the stale X11 screenshot helper,
updated the desktop evidence script, and added the current EGL pixel probe.
The derived rootfs and provenance are under
`target/desktop-m4-current-overlay-20261015-v2/rootfs/`.

## Gate

```text
desktop_m4_drm_gate.py
```

Result: **PASS**

Result receipt:

`target/desktop-m4-current-overlay-20261015-v2/gate-gl/result.json`

Receipt SHA-256:

`300261441ade90511563ebd6471031c426b7d02063f0e4461a2d5e89bde9f78c`

Serial receipt:

`target/desktop-m4-current-overlay-20261015-v2/gate-gl/desktop-m4-drm.serial.log`

Serial SHA-256:

`600d110b4af436764ff899f73112cbdf13a736f95beec5e8da9aefed9e9ec110`

Rootfs input SHA-256:

`db16a007e032729c4824a7d0a988cb5f4529ce70e294acaf41db0329b41e88f0`

The derived manifest records the same rootfs identity and the overlay
derivation. The run used `virtio-gpu-gl-device`, `egl-headless`, `-nic none`,
and four harts.

## Required evidence

The serial receipt contains:

```text
DEBIAN_DESKTOP_M4_DRM_SYSFS card=226:0 render=226:128 driver=virtio_gpu
DEBIAN_DESKTOP_M4_DRM_NODES card=/dev/dri/card0 render=/dev/dri/renderD128
PIXEL_PROBE ok=yes ... display=gbm renderer='virgl' gl='3.1 Mesa 25.0.7-2+deb13u1'
DEBIAN_DESKTOP_M4_DRM_X11_SCREENSHOT width=1280 height=800 bpp=32 stride=5120 bytes=4096000 sha256=b00063d6eadf9747639a6251864c6cbe9cafa38c4939c1eac0c074a715e265de
DEBIAN_DESKTOP_M4_READY user=asterinas display=:0
```

The first overlay attempt intentionally stopped at the next missing contract:
`reason="missing DRM EGL virgl pixel probe"`. That failure led to the v2
overlay; it did not modify the base image.
