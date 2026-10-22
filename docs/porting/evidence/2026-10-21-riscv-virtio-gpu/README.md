# virtio-gpu on the RISC-V kernel

This is the RISC-V half of milestone P1 in
[`docs/porting/drm-shared-core-design.md`](../../drm-shared-core-design.md), the
counterpart of
[`2026-10-21-x86-virtio-gpu`](../2026-10-21-x86-virtio-gpu/README.md).

The x86 half proved that the shared `aster-drm` core, the
`kernel/comps/drm-virtio-gpu` plugin and a raw legacy-KMS client drive a
virtio-gpu **PCI** device. This half runs the **same three pieces** unchanged
on RISC-V, where the device is a virtio-gpu **MMIO** device on the QEMU
`virt` machine. The only difference is the virtio transport the device layer
already abstracts over.

## What ran

- Kernel: the live tree (`kernel/core`, `kernel/comps/drm`,
  `kernel/comps/drm-virtio-gpu`), built for Sv39:

  ```sh
  make kernel TARGET_ARCH=riscv64 FEATURES=riscv_sv39_mode
  # target/osdk/asterinas-osdk-bin.Image
  ```

- QEMU: `-machine virt`, `-device virtio-gpu-device` (virtio-mmio), the
  generated device tree advertises the node as `virtio,mmio`, and
  `kernel/core/comps/virtio/src/transport/mmio` discovers it. No PCI, no
  firmware framebuffer.

- Userspace: `tools/drm/virtio_gpu_probe.c` compiled static for riscv64 and
  packed as a one-file initramfs (`tools/drm/build_probe.sh --arch riscv64`).
  As PID 1 it mounts devtmpfs, then drives
  `GETRESOURCES → GETCONNECTOR → GETENCODER → CREATE_DUMB → MAP_DUMB →
  mmap → ADDFB → SETCRTC → DIRTYFB` and paints four colour bars.

## Transcript

```
virtio: virtio-gpu: 1 scanout(s), using 1024x768
...
aster-drm-virtio-gpu: Component initialize complete
DRMPROBE_DEVICE /dev/dri/card0
DRMPROBE_RESOURCES crtcs=1 connectors=1 encoders=1
DRMPROBE_IDS crtc=15 connector=19 encoder=18
DRMPROBE_MODE 1024x768 1024x768@60
DRMPROBE_CONNECTOR encoder_id=0 type=15 status=1
DRMPROBE_CRTC 15
DRMPROBE_DUMB handle=1 pitch=4096 size=3145728
DRMPROBE_FILLED
DRMPROBE_ADDFB fb=20
DRMPROBE_SETCRTC ok
DRMPROBE_DIRTYFB ok
DRMPROBE_PASS
```

A monitor `screendump` taken after `DRMPROBE_PASS` is a 1024x768 P6 PPM with
four colours and nothing else: 756 sampled pixels, `red:189`, `green:189`,
`blue:189`, `white:189`, zero black. That is the same colour-bar acceptance
as the x86 half (the x86 probe used the same producer path and reported
8108/8108 non-black on a denser sampling grid).

The whole gate is reproducible:

```sh
make test_riscv_virtio_gpu
# or
tools/riscv/drm/virtio_gpu_check.sh --kernel target/osdk/asterinas-osdk-bin.Image
```

## Xorg modesetting on the same device

The probe is the raw path. The desktop path uses the same kernel pieces with a
Debian userland:

1. Build the image profile that has an X server and no fbdev driver fallback:

   ```sh
   ASTERINAS_EXPLICIT_QEMU=1 \
   tools/riscv/debian/rootfs/build_rootfs.sh --architecture riscv64 \
     --profile desktop-drm \
     --output-dir target/debian-riscv/desktop-drm/rootfs
   # target/debian-riscv/desktop-drm/rootfs/debian-root.ext2 (label ASTER_DEBIANDRM)
   ```

   (`ASTERINAS_EXPLICIT_QEMU=1` is for build hosts without a riscv64
   binfmt_misc registration; proot drives the staged `qemu-riscv64-static`.)

2. Build the Stage1 handoff initramfs and run the gate:

   ```sh
   tools/riscv/debian/rootfs/build_stage1.sh --architecture riscv64 --minimal
   make test_riscv_xorg
   ```

The gate copies the pristine image, injects a oneshot unit
(`xorg-gate.service`) that starts `/usr/lib/xorg/Xorg :0` on `/dev/dri/card0`
with `AutoAddDevices off`, and paints the root window through
`tools/drm/x11_paint.c` — a raw X11 client, because the profile does not
install `x11-xserver-utils` and therefore has no `xsetroot`. The request
sequence is the one `xsetroot -solid red` produces: `ChangeWindowAttributes`
(CWBackPixel) followed by `ClearArea` over the root, then a `GetInputFocus`
round trip. The image's own graphical session is deliberately not used: it
waits on udev/logind and a virtual terminal, while this gate needs only the X
server and the DRM node.

Transcript from QEMU (TCG; boot to the gate unit takes about four minutes):

```
xorg-gate.sh[67]: XORG_GATE_BEGIN
xorg-gate.sh[67]: XORG_GATE_SOCKET_WAIT=3
xorg-gate.sh[74]: XPAINT_SETUP root=0x224 depth=24
xorg-gate.sh[74]: XPAINT_DONE pixel=0xff0000
xorg-gate.sh[67]: XORG_GATE_PAINT=0
screendump 1024x768 red=1.000
PASS (RISC-V Xorg modesetting on virtio-gpu: red root window, screendump)
```

The scanout is 1024x768 (the mode from the connector probe) and every sampled
pixel is red, so `SETCRTC` and `DIRTYFB` reached the virtio-gpu resource and
the host side displays it.

## Findings worth keeping

- The KMS object store builds only static topology
  (`attach_connector_to_encoder`, `attach_encoder_to_crtc`). It does not set
  the dynamic links, so `GETCONNECTOR` reports `encoder_id=0` and
  `GETENCODER` reports `crtc_id=0` until a modeset happens. Linux clients
  tolerate this by falling back to the encoder's `possible_crtcs` mask;
  `virtio_gpu_probe.c` does the same and picks the first CRTC. Xorg's
  modesetting driver implements the same fallback, so this is not expected
  to block the desktop step, but it is a difference from Linux worth noting.
- `MODE_GETCONNECTOR`'s userspace ABI is the *current* Linux layout (four
  pointers, three counts, then the id/status fields). A hand-written client
  using the older pre-6.x field order shifts `connector_id` by 32 bytes and
  makes the ioctl return `ENOENT`; `virtio_gpu_probe.c` declares the struct in
  the current order, matching the kernel's `#[padding_struct]` definition.
- The RISC-V initramfs needs no busybox or systemd: a static `/init` that
  mounts devtmpfs is enough, which keeps the KMS gate independent of the
  Debian image work.
- Xorg takes about three minutes to become idle on this QEMU/host pair
  (software emulation), and a client that connects as soon as
  `/tmp/.X11-unix/X0` exists can wait through the tail of server
  initialization. The paint client's alarm is 180 s for that reason; an
  earlier 30 s alarm expired while the server was still initializing.

## Not yet done

The full desktop session (`asterinas-desktop-drm.service`) still fails in this
image: it wants udev/logind and a virtual terminal, and its failure predates
Xorg. The gate bypasses it deliberately. Bringing the session up unchanged is
a udev/VT question, not a DRM one.
