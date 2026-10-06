# QEMU desktop DRM gate, 2026-10-06

This is a bounded QEMU-only validation of the Debian `desktop-drm` profile. It
does not start the physical board and it does not claim PowerVR support.

## Change under test

The rootfs builder now wires the `desktop-drm` profile to the DRM display
provider. The generated session uses the Xorg modesetting driver and exposes
`/dev/dri/card0` and `/dev/dri/renderD128` to the desktop clients. The fbdev
no-input configuration remains restricted to the fbdev provider.

## Inputs

The gate used `virtio-gpu-device`, no network, and `-display none` with QEMU's
QMP screenshot capture:

| Input | SHA-256 |
| --- | --- |
| release kernel | `d1ce9520d14e8444114277a72ac3a9e546c7a53798a588d16fbc25d15a97f183` |
| U-Boot | `b34728358ee292ae57d066578a71503b7ccb634827a4df7538c558a5f3cc9efd` |
| DTB | `d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c2` |
| Stage1 initramfs | `cae8f55fcb6b08211c942a56ed2a9bdd7fcfdf28a63190c824fa684019b6e7e6` |
| rootfs image | `6402e8277aa9bcaee108689cf7377f46991abeb8f70ff0b67920cbc2315bc2b4` |
| rootfs manifest | `13e4aed748a652e82e15f42e63d39635e954ca1a8bfbe42a7637645df2465e7d` |
| packages.lock | `056e97583bff43880d09e13790ee7a7a69f25f8535a1cdde8e5203e0060aa30d` |
| package checksums | `ae23d6027775567962360b009efb304b5b66158446fd8b0e4bcb8cb32bcf03ff` |

The rootfs contract verifier passed before the boot. Stage1 selected the root
on `/dev/vdb` and completed the root, `/dev`, API-directory, `/run`, `/tmp`,
chroot, and exec handoff steps.

## Result

The `desktop_drm_gate.py` result was `passed: true` and `reason: pass`. It
observed all required markers:

```text
DEBIAN_DESKTOP_DRM_UDEV state=active
DEBIAN_DESKTOP_DRM_LOGIND state=active
DEBIAN_DESKTOP_DRM_SESSION user=asterinas tty=tty1
DEBIAN_DESKTOP_DRM_INPUT keyboard=evdev pointer=evdev
DEBIAN_DESKTOP_DRM_XORG driver=modesetting device=virtio_gpu drm=active display=:0
DEBIAN_DESKTOP_DRM_CLIENTS window-manager=openbox file-manager=pcmanfm panel=lxpanel terminal=xterm
DEBIAN_DESKTOP_DRM_READY user=asterinas display=:0
```

The screenshot was 1280x800 with 986,526 non-background pixels and 256
distinct sampled colors. This verifies that the desktop produced a non-empty
scanout image, not only that Xorg started.

## Acceleration boundary

The same transcript reports:

```text
modeset(0): Refusing to try glamor on llvmpipe
modeset(0): glamor initialization failed
DEBIAN_DESKTOP_DRM_GL_XORG accel=llvmpipe
DRI3_OPEN node=/dev/dri/card0 device_name=/dev/dri/card0 reopen=ok
DRI3_OPEN node=/dev/dri/renderD128 device_name=/dev/dri/renderD128 reopen=ok
```

Therefore this gate proves the DRM/modesetting desktop path and pixel output,
but it deliberately does not claim hardware 3D acceleration. QEMU's current
`virtio-gpu-device` run is using the software `llvmpipe` renderer. A separate
virgl run must report a hardware-backed renderer before it can be used as a
Firefox GPU-acceleration result.
