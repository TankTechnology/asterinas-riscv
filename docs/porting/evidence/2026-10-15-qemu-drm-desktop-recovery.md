# QEMU DRM desktop recovery gate

Date: 2026-10-15 (Asia/Shanghai)

## Gate

```text
desktop_m4_drm_recovery_gate.py
```

The gate uses the same v2 derived rootfs and GL-enabled QEMU as the one-boot
DRM gate, with `asterinas.reboot_after=60` and QEMU reboot enabled. It waits
for the first virgl desktop readiness marker, then requires a fresh OpenSBI
epoch, U-Boot banner, and U-Boot prompt before closing QEMU.

Result: **PASS**

Receipt:

`target/desktop-m4-current-overlay-20261015-v2/recovery-gate-final/result.json`

Receipt SHA-256:

`8b2498b08e9e32033dce2e53de4d88f6cdf5ce63624be69ec78ba4eab9788eee`

Serial receipt:

`target/desktop-m4-current-overlay-20261015-v2/recovery-gate-final/desktop-m4-drm-recovery.serial.log`

Serial SHA-256:

`6c48b1e8d3afa344584ebf686a33c84486ac0bfdfdb1c1e500b7d009c4313e15`

## Recovery evidence

The serial receipt contains the first DRM desktop markers and then a second
firmware epoch:

```text
DEBIAN_DESKTOP_M4_DRM_SYSFS card=226:0 render=226:128 driver=virtio_gpu
PIXEL_PROBE ok=yes ... renderer='virgl'
DEBIAN_DESKTOP_M4_DRM_X11_SCREENSHOT width=1280 height=800 ...
DEBIAN_DESKTOP_M4_READY user=asterinas display=:0
OpenSBI v1.7
U-Boot 2026.07 ...
=>
```

This proves the DRM desktop path and the software-reboot recovery boundary
within one QEMU process. It does not claim physical-board DRM or Firefox
WebRender support.
