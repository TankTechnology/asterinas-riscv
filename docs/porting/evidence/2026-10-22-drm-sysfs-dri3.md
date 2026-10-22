# The DRM sysfs view, and DRI3 on top of it

Closes DoD 2 of the shared-DRM goal: `xdpyinfo` advertises DRI3 and PRESENT, and
a DRI3 client completes one buffer exchange with the server.

The blocker was not a DRM ioctl. Mesa's EGL could not create a GBM device, so
glamor never initialized and the X server never enabled DRI3, while every KMS,
PRIME and sync ioctl underneath worked. `libEGL` reported
`EGL_NOT_INITIALIZED` from `DRI2: failed to create gbm device`.

## What libdrm reads before it trusts a DRM fd

`drmGetDevice2()` and the helpers around it answer from `/sys`:

| helper | path | needed for |
|---|---|---|
| `drmGetMinorType` | `/sys/dev/char/M:m/device/drm/<node>` | deciding card vs render |
| `drmParseSubsystemType` | `/sys/dev/char/M:m/device/subsystem` (readlink) | bus type |
| `drmParseOFBusInfo` | `/sys/dev/char/M:m/device/uevent` → `MODALIAS` | platform identity |
| `drmGetDeviceNameFromFd2` | `/sys/dev/char/M:m/uevent` → `DEVNAME` | `/dev/dri/<name>` |
| enumeration | `/dev/dri/*`, `/sys/class/drm/*` | device listing |

With any of those missing `drmGetDevice2()` fails with `ENODEV`/`ENOENT`, and
`gbm_create_device()` returns `NULL`. Nothing above it can recover.

## Kernel

- `kernel/core/comps/device` is the device model that owns `/sys/dev`,
  `/sys/class`, `/sys/bus` and `/sys/devices`. DRM is not ported to it yet, so
  it gained three legacy entry points next to the existing
  `register_legacy_virtual_node` bridge:
  `register_legacy_dev_entry`, `register_legacy_class_dir` and
  `register_legacy_bus_dir`.
- `kernel/core/src/device/drm_sysfs.rs` builds the per-device view and attaches
  it through those entry points:

  ```
  /sys/dev/char/226:0/
      uevent                 MAJOR/MINOR/DEVNAME=dri/card0/DEVTYPE=drm_minor
      device/
          uevent             DRIVER=virtio_gpu, MODALIAS=virtio:d00000010
          subsystem -> ../../../../bus/platform
          drm/card0
          drm/renderD128     (only when the driver exposes a render node)
  /sys/class/drm/card0/
      uevent                 MAJOR/MINOR/DEVNAME
  /sys/bus/platform/
  ```

- `DrmDevice::sysfs_modalias()` lets a driver state its platform identity;
  `drm-virtio-gpu` returns `virtio:d00000010` (virtio spec 5.7).
- `aster_drm::register_device()` publishes the view after the character nodes
  are registered. A failure is logged and does not fail device registration:
  the ioctl surface stays usable without it.

The first attempt built its own `/sys/dev` and `/sys/class` roots and collided
with the device model (`AlreadyExists`); the probe run showed the kernel warning
and no nodes. The current version attaches into the existing tree.

## Acceptance: the sysfs view

`tools/drm/sysfs_probe.c` mounts sysfs as PID 1, then checks each path above.
Both architectures print the same transcript:

```
SYSFSPROBE_NODE card0=226:0
SYSFSPROBE_SUBSYSTEM ../../../../bus/platform
SYSFSPROBE_UEVENT DRIVER=virtio_gpu
MODALIAS=virtio:d00000010
SYSFSPROBE_CHAR_UEVENT MAJOR=226
MINOR=0
DEVNAME=dri/card0
DEVTYPE=drm_minor
SYSFSPROBE_PASS
```

```sh
tools/drm/initramfs_probe_check.sh --arch amd64 \
  --kernel target/prime/kernels/x86.elf --probe tools/drm/sysfs_probe.c \
  --pass SYSFSPROBE_PASS --workspace target/conformance/sysfs-x86
tools/drm/initramfs_probe_check.sh --arch riscv64 \
  --kernel target/prime/kernels/riscv.Image --probe tools/drm/sysfs_probe.c \
  --pass SYSFSPROBE_PASS --workspace target/conformance/sysfs-riscv
```

The probe is part of `tools/drm/conformance_matrix.sh` (`sysfs`).

## Acceptance: DRI3

`tools/drm/dri3_probe.c` speaks the X11 wire protocol itself (no libX11): it
opens the display, queries the extension, exports a dumb buffer as a dma-buf
over `DRM_IOCTL_PRIME_HANDLE_TO_FD`, sends `DRI3PixmapFromBuffer` with the
descriptor in `SCM_RIGHTS`, and verifies with `GetGeometry` that the server
imported the pixmap at the expected size and depth.


Both gates run it against a server that has DRI3 enabled, and both pass:

```
DRI3PROBE_SETUP root=0x144 pixmap=0x200001
DRI3PROBE_VERSION 1.2 opcode=149
DRI3PROBE_EXPORT pitch=1024 fd=5
DRI3PROBE_PIXMAP 256x256 depth=24
DRI3PROBE_PASS
```

`xdpyinfo -queryExtensions` on the same server lists `DRI2`, `DRI3` and
`Present` (opcode 149 for DRI3).

The fence half of the same DoD — sync-object export/import as a sync-file,
cross-process waits, and atomic commits taking an `IN_FENCE_FD` and publishing
an `OUT_FENCE_PTR` — is covered by
[`2026-10-22-x86-fence.md`](2026-10-22-x86-fence.md) and re-verified on both
architectures with this kernel.

Two further kernel-side details were needed, and both were found from this
probe rather than guessed:

- **A render node.** Linux's `virtio_gpu` sets `DRIVER_RENDER`, so the DRM core
  always creates `renderD128`, with or without virgl. Mesa's EGL device setup
  takes a card-node fd, asks `loader_is_device_render_capable()`, and when the
  answer is no it looks for a *compatible render-only device*; with no render
  node anywhere it fails with `DRI2: failed to get compatible render device`
  before the DRI driver loads. `drm-virtio-gpu` now advertises
  `DrmFeatures::RENDER`, so the node exists and the device's `drm/` directory
  lists it.
- **`lseek(fd, 0, SEEK_END)` on a dma-buf.** Linux's dma-buf `llseek` returns
  `offset + size`. Mesa's `kms_swrast` import path sizes an imported buffer
  with exactly that call and bails out silently when it comes back wrong; the
  X server then reports `BadAlloc` for `DRI3PixmapFromBuffer`. `DmaBufFile`
  now implements `seek_end()`, and `prime_probe` checks the contract
  (`PRIMEPROBE_LSEEK size=16384`).

## The software renderer

QEMU runs virtio-gpu without virgl, so there is no 3D engine. Xorg refuses
glamor outright on llvmpipe ("Refusing to try glamor on llvmpipe"), so both
gates start the server with:

```
MESA_LOADER_DRIVER_OVERRIDE=kms_swrast
LIBGL_ALWAYS_SOFTWARE=1      # makes EGL use the EGLDevice software path
GALLIUM_DRIVER=softpipe      # renderer string "softpipe", not "llvmpipe"
```

`glamor X acceleration enabled on softpipe` is the line that shows the choice
took effect. Rendering is software, as it must be on this transport; what the
gate demonstrates is that the DRI3/dma-buf protocol works end to end.

## Gates

| gate | target | result |
|---|---|---|
| x86 graphical | `make test_x86_graphical` | serial password login, apt install, Xorg, xterm, DRI3+Present, DRI3 buffer exchange, red screendump, power-button power-off, clean `e2fsck -fn` |
| RISC-V graphical | `make test_riscv_graphical` | root serial autologin, Xorg, xterm, DRI3+Present, DRI3 buffer exchange, red screendump, in-guest power-off, clean `e2fsck -fn` |
| RISC-V legacy Xorg | `make test_riscv_xorg` | unchanged gate on the same kernel: `screendump 1024x768 red=1.000`, power-off, clean `e2fsck -fn` |

The RISC-V `desktop-drm` image ships no `sudo` and its `login` did not finish
within agetty's 60 s window, so its session runs as `root` through
`agetty --autologin`. The x86 gate keeps the unprivileged password login.
Both gates also retire units an older gate left enabled in the base images
(`xorgprobe.service` on x86, `asterinas-desktop-drm*.service` on RISC-V);
each starts its own X server on `:0` and would otherwise win the socket.

## Convergence

- `kernel/src/device/dri.rs` and `ScanoutBackend` are gone
  (`chore(drm): retire the in-tree DRM driver in favour of aster-drm`); the
  dead tree's `sysfs.rs` keeps only the driver name.
- The conformance matrix is green on both architectures with the same kernel
  sources and the same probe binaries:

  ```
  $ make test_drm_conformance_x86
  PROBE legacy   PASS
  PROBE prime    PASS
  PROBE atomic   PASS
  PROBE sync     PASS
  PROBE flip     PASS
  PROBE sysfs    PASS
  $ make test_drm_conformance_riscv
  (same six lines)
  ```

## Reproduction

```sh
make kernel TARGET_ARCH=riscv64 FEATURES=riscv_sv39_mode   # inside the build container
cp target/osdk/asterinas/asterinas-osdk-bin.Image target/prime/kernels/riscv.Image
make test_riscv_graphical RISCV_GRAPHICAL_KERNEL=$PWD/target/prime/kernels/riscv.Image
make test_x86_graphical \
  X86_DEBIAN_KERNEL=$PWD/target/prime/kernels/x86.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/final-matrix/xwrap/image18.ext2
make test_drm_conformance
```

## Boundaries

- The sysfs tree is published through the device model's legacy entry points
  rather than by porting DRM to `aster-device`; that port is the natural next
  step and would also give the model its own `uevent` support.
- The `subsystem` link points at `/sys/bus/platform` for both transports. On
  x86 the device really is virtio-pci; libdrm only reads the link's last
  component, and a PCI bus view needs PCI sysfs, which this kernel does not
  publish yet.
- The probe covers the `PixmapFromBuffer` exchange only: `DRI3Open`, DRI3
  fences and `PixmapFromBuffers` (explicit modifiers) are not exercised, and
  the client speaks the wire protocol rather than libdri3.
- Rendering is softpipe; there is no virtio-gpu 3D (virgl) uAPI in the kernel.
