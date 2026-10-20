# QEMU fbdev VNC scanout gate r2

Date: 2026-10-17 (Asia/Shanghai)

## Result

**PASS.** A QEMU 10.2.1 binary was rebuilt with the original GL-capable
configuration (`--without-default-features`, OpenGL, virglrenderer, pixman)
and VNC as the only added display feature. The private Unix VNC scanout gate
then reached the guest scanout marker and completed the host QMP screenshot
check.

The guest receipt contains:

```text
DEBIAN_DESKTOP_M4_FB_SCANOUT_READY width=1280 height=1024 stride=5120 bands=3
DEBIAN_DESKTOP_M4_FB_IOCTL width=1280 height=1024 stride=5120 bpp=32 format=BgrReserved
DEBIAN_DESKTOP_M4_FB_COLORS distinct=3 non_background=2688
DEBIAN_DESKTOP_M4_READY user=asterinas display=:0
```

The host PPM has 1,310,720 pixels, three sampled colors, and 874,496 pixels
different from the background. Its hashes are:

```text
pixel_sha256=44b1d582f4d56d27b4c1715bfbc317f01dd2f8ed3c0c5742f2c2bda30f4b6b44
ppm_sha256=3fc3cfaebf37f1f6000c34ac9f344f7673360d09102f1d70a2c38a7b4dd97105
```

The machine-readable result is `passed=true, reason=pass`.

## Inputs and boundary

The run used `asterinas/qemu-gl-env:20261014`, `--network=none`, `-nic none`,
`bochs-display,xres=1280,yres=1024`, and a private VNC Unix socket. The
kernel, U-Boot, DTB, Stage1 delimiter-fix initramfs, root image, manifest,
package lock, and package checksums are the same hashes recorded in the
previous Stage1 gate. No physical board, serial device, or board reboot was
used.

The minimal QEMU binary SHA-256 is:

```text
1fbcd1f4616baa64c10f1369c8acf68f046917d0a4577a959a45a76e1d17e610
```

## Receipt paths

- Result: `/mnt/mydisk/asterinas-gates/qemu-fbdev-scanout-20261017-qemu-minimal-vnc/result.json`
- Serial: `/mnt/mydisk/asterinas-gates/qemu-fbdev-scanout-20261017-qemu-minimal-vnc/desktop-m4-scanout.serial.log`
- PPM: `/mnt/mydisk/asterinas-gates/qemu-fbdev-scanout-20261017-qemu-minimal-vnc/desktop-m4-scanout.ppm`

This closes the one-boot fbdev-to-host-scanout transport gate in QEMU. It does
not by itself close the separate reboot/recovery leg, DRM Task 5, Firefox
WebRender Task 6, or physical-board validation.

