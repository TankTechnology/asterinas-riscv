# QEMU Firefox rootfs DRM desktop gate r14

Date: 2026-10-17 (Asia/Shanghai)

## Scope

This is a QEMU-only Task 5 compatibility receipt. It proves that the
provenance-checked, RISC-V Firefox package can coexist with the DRM desktop
rootfs and that the existing virgl/Xorg/recovery contract still passes. It is
not a Firefox WebRender receipt and says nothing about the physical board.

The physical board was not accessed, written, rebooted, or probed.

## Inputs and preparation

- Firefox package: `/mnt/mydisk/firefox-builder-snapshot-20261017/repack-stripped-20261017-v2/pkg/firefox-esr_140.16.0esr-1~deb13u1_riscv64-stripped.deb`
- Package SHA-256: `7f8c6707f168af5bccfca9e92414351afaf86af81f34ea0ab7c7097f0e636fcc`
- Package provenance gate: `FIREFOX_PACKAGE_PROVENANCE PASS`, version
  `140.16.0esr-1~deb13u1`, patches `2`
- Root profile: `desktop-m4-firefox` (schema 4, 2 GiB ext2, stable
  `ASTER_DEBIANM4` label, dedicated UUID)
- Root overlay image:
  `/mnt/mydisk/firefox-builder-snapshot-20261017/qemu-rootfs-firefox/debian-root-firefox-2g-r13.ext2`
- Root image SHA-256: `05b6a929cac19bf6d69595d6fd37c0d16dbbe7241ab3fccddca39d303575d2`
- Manifest SHA-256:
  `75df128dea63b9fc35b7799a55050479f0018514550d2a8d2371231419d2190c`
- `packages.lock` SHA-256:
  `f5b577860e9eb378d3020a9e11c9f58348b600be785810dac7142caf0a3635dd`
- `package-checksums` SHA-256:
  `502e1630ecd07ebeedb38cdf382fcd088175aa0c182d0294c1118a9ead761d6e`

The Firefox `libxul.so` was stripped before packaging; the package hash and
provenance were regenerated after that transformation. The overlay was
installed by `compat_overlay.py`, and `e2fsck -fn` passed before launch.

## Runtime command and receipt

The gate ran once in `asterinas/qemu-gl-env:20261014` with the custom
QEMU 10.2.1 binary, `/dev/dri/renderD128`, `-display egl-headless`,
`virtio-gpu-gl-device`, and `-nic none`.

Raw output directory:
`/mnt/mydisk/firefox-builder-snapshot-20261017/qemu-rootfs-firefox/gate-firefox-drm-r14/`

| Raw file | SHA-256 |
| --- | --- |
| `desktop-m4-drm-recovery.serial.log` | `f1e9fb00424823aef4e3e8b2c453acbbecf1a9cc79b72e3c269f2380ff82e741` |
| `result.json` | `b8bd85e06c51154f571ce4ff7d3c85ca7089dcbbb6032127a625c320d98a022b` |

`result.json` reports `passed=true`, `reason=pass`, and includes a fresh
OpenSBI/U-Boot epoch after the bounded software reboot. The ordered runtime
records include:

```text
DEBIAN_DESKTOP_M4_DRM_FBDEV dependency=none action=skipped
DEBIAN_DESKTOP_M4_DRM_CONFIG driver=modesetting kmsdev=/dev/dri/card0 fbdev=none
DEBIAN_DESKTOP_M4_DRM_SYSFS card=226:0 render=226:128 driver=virtio_gpu
DEBIAN_DESKTOP_M4_DRM_NODES card=/dev/dri/card0 render=/dev/dri/renderD128
DEBIAN_DESKTOP_M4_XORG provider=drm display=:0 renderer=virgl
PIXEL_PROBE ok=yes left=ffffff right=000000 expect=ffffff/000000 display=gbm renderer='virgl'
DEBIAN_DESKTOP_M4_DRM_X11_SCREENSHOT width=1280 height=800 bpp=32 stride=5120 bytes=4096000 sha256=b00063d6eadf9747639a6251864c6cbe9cafa38c4939c1eac0c074a715e265de
DEBIAN_DESKTOP_M4_READY user=asterinas display=:0
OpenSBI v1.7
U-Boot 2026.07
```

## Result and boundary

**PASS:** Task 5 remains green with the Firefox-compatible rootfs, including
DRM-only ownership, virgl EGL pixel evidence, X11 pixel hash, and recovery.
The new `desktop-m4-firefox` profile and the accepted-profile gate are covered
by the focused host tests. Firefox startup/WebRender, browser scene pixels,
and Task 6 recovery are still open; this receipt must not be used to claim
Firefox acceleration.
