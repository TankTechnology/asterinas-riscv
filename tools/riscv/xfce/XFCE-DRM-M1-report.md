# RISC-V Xfce on DRM — M1 report

**Date:** 2026-08-25

## Result

The main/NixOS persistent-root boot path and the DRM track's virtio-gpu virgl
implementation now run together on RISC-V. Xorg uses the modesetting driver,
enables glamor on virgl, opens a 1280x800 scanout, and starts the Xfce session.

The acceptance boot reported all of the following:

- `modeset(0): using default device`;
- `glamor X acceleration enabled on virgl`;
- `XFCE_DRM_X11_CONNECT_OK` and `XFCE_DRM_XORG_READY`;
- the Xfce session service started;
- the `Asterinas DRM Xfce Desktop` target was reached;
- no kernel panic and no DRM framebuffer rejection.

## Build and run

From the repository root:

```sh
bash tools/riscv/nixos/m19/fetch_debian_rootfs.sh
python3 tools/riscv/xfce/build_xfce_drm.py
python3 tools/riscv/xfce/boot_xfce_drm.py --interactive
```

Interactive mode opens a native QEMU GTK window with OpenGL enabled. The
headless default performs the same serial acceptance test and exits after the
desktop is ready.

The builder currently uses the existing systemd/Xfce base archive from the
RISC-V workspace and the Debian trixie RISC-V Xorg/Mesa runtime staged under
`target/m19/rootfs`. It produces a small stage-1 initramfs plus a persistent
ext2 desktop root, avoiding the old multi-hundred-megabyte initramfs unpack.

## Kernel fixes required by the workload

Two defects were found only after running real Xorg glamor traffic:

1. `VIRTIO_GPU_CMD_SUBMIT_3D` used the fixed one-page control buffer. Large
   command streams now receive a right-sized temporary DMA buffer.
2. Legacy `DRM_IOCTL_MODE_ADDFB` calculated scanline size by rounding the full
   bit count instead of rounding bytes per pixel. A 1280x800, 32-bpp buffer was
   therefore incorrectly rejected. The ioctl now honors the requested pitch
   and computes the final scanline correctly.

Virgl resource creation also honors the userspace-provided stride and size.

## Experience and limitations

The display path is no longer the old software-only fbdev path: Mesa renders
through virgl and Xorg uses glamor. Window composition and drawing therefore
exercise the host GPU-backed virtio-gpu path.

The RISC-V CPU is still emulated by QEMU TCG on this host. The measured
acceptance run took about 37 seconds of kernel time and 9 minutes of guest
userspace time before reaching the desktop. This startup cost and CPU-heavy
desktop operations should not be described as native-speed or fully smooth.
The next performance milestone is reducing the userspace closure and replacing
remaining TCG-bound startup work; it is separate from the now-working DRM path.

VNC is intentionally not used by this launcher. With QEMU's accelerated
`egl-headless` scanout, the simultaneous VNC backend exposes a separate black
surface. The GTK/OpenGL backend presents the actual accelerated scanout for
interactive use.
