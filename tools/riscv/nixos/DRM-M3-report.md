# DRM-M3 — Xorg's modesetting driver on /dev/dri/card0

Date: 2026-08-15
Branch: `track/drm`
Status: **PASS** — the stock Xorg `modesetting` driver runs against Asterinas's
`/dev/dri/card0`, enumerates the KMS object graph, sets the 1280x800 mode, and
presents a software-rendered desktop. A QEMU `screendump` confirms the rendered
frame reached the host display (left half red, right half blue), proving the
DRM/KMS path end-to-end — not just a boot-time test pattern.

## Goal

DRM-M1/M2 delivered a usable KMS device (dumb buffers, mmap, mode setting).
This milestone is the **real acceptance**: a genuine graphics client — the
X.Org server with its standard `modesetting` driver — drives `/dev/dri/card0`
through the same ioctl surface a Linux kernel exposes. That exercises the
driver's full startup path (`SET_MASTER`, resource/connector/CRTC enumeration,
property probing, `ADDFB`, `SETCRTC`, and the shadowfb present path), and every
kernel gap it revealed was fixed incrementally.

## What was implemented

### 1. Kernel gaps (`kernel/src/device/dri.rs`)

Five ioctls the modesetting driver requires that M2 did not implement:

| ioctl | nr | what it does |
|---|---|---|
| `DRM_IOCTL_SET_MASTER` | 0x1e | grant DRM master (no-op; required at `ScreenInit`) |
| `DRM_IOCTL_DROP_MASTER` | 0x1f | release DRM master (no-op) |
| `DRM_IOCTL_MODE_OBJ_GETPROPERTIES` | 0xb9 | report an empty property set (`count_props = 0`) |
| `DRM_IOCTL_MODE_PAGE_FLIP` | 0xb0 | present a framebuffer |
| `DRM_IOCTL_MODE_DIRTYFB` | 0xb1 | re-present the framebuffer (shadowfb present path) |

The three present ioctls (`SETCRTC`, `PAGE_FLIP`, `DIRTYFB`) now share a
`present_fb(fb_id)` helper: look up the framebuffer → dumb buffer → its
guest-physical address, and call `GpuDevice::present_framebuffer`, which runs
virtio-gpu `SET_SCANOUT → TRANSFER_TO_HOST_2D → FLUSH`. This is the crucial
virtio-gpu subtlety: a guest-side mmap write to the dumb buffer is never seen
by the host until a `TRANSFER_TO_HOST` re-pulls the pixels, so *every* present
must re-run that transfer.

- **`OBJ_GETPROPERTIES` returns zero properties.** The modesetting driver
  probes CRTC/connector/plane properties to decide whether to use atomic,
  gamma, or CTM paths; an empty set is valid and keeps it on the plain
  `SETCRTC`/`DIRTYFB` path. Returning success (rather than `ENOTTY`) makes the
  CRTC survive `drmmode_crtc_init`, which destroys it if the probe fails.
- **`DIRTYFB` accepts `fb_id == 0`.** The driver's capability probe is
  `drmModeDirtyFB(fd, fb_id, NULL, 0)` issued before the first framebuffer
  exists; returning success there flips on its damage-tracking present path
  (`Damage tracking initialized` in the log), so every shadow update pushes to
  the scanout.

### 2. Cross-compiled `modesetting_drv.so` + `libdrm`

The sibling tree's xserver cross-build had only the `fbdev` driver; the
`modesetting` driver is gated on `build_modesetting = libdrm_dep.found() and
dri2proto_dep.found()`, and neither was present.

- **libdrm 2.4.120** cross-compiled with meson (core only — all vendor drivers,
  tests, and man pages disabled) into `target/riscv-cross/usr`, yielding
  `libdrm.so.2.4.0` + `libdrm.pc` + `xf86drm.h`.
- **`dri2proto.pc`** was installed by xorgproto under `usr/share/pkgconfig/`,
  outside the cross file's `pkg_config_libdir`; a symlink into
  `usr/lib/pkgconfig/` made it visible.
- Reconfigured the xserver meson build; `libdrm found: YES`, `dri2proto found:
  YES`, and `modesetting_drv.so` built and linked against `libdrm.so.2`.
  Glamor stays disabled, so the driver uses the software/shadow path.

### 3. Harness (`tools/riscv/nixos/drm/`)

- `xorg-modesetting.conf` — `Driver "modesetting"` with `Option "ShadowFB"
  "true"` and `Option "PageFlip" "false"` (steer to the `DIRTYFB` present
  path), plus the evdev keyboard/pointer on the virtio input devices.
- `init_m3.c` — `/init` launcher: forks Xorg (`-logfile /dev/ttyS0`) and a
  draw client.
- `xfill.c` — static X11 client: paints the root window solid blue with a solid
  red left half, then reports markers. Two color allocations keep the draw to
  two X round-trips (a 256-strip gradient was prohibitively slow under TCG).
- `build_drm_m3.sh` — assembles the initramfs from the cross tree (Xorg,
  modesetting + evdev modules, libdrm, libxcvt, glibc, xkbcomp, XKB data) and
  strips every ELF.
- `boot_drm_m3.py` — U-Boot/booti boot with `virtio-gpu` + keyboard/tablet,
  waits for the draw marker, screendumps, and checks the left-red/right-blue
  pattern.

## Verification

```
=== DRM-M3 guest result ===
(II) modeset(0): using default device
(==) modeset(0): Depth 24
(II) modeset(0): No glamor support in the X Server
(II) modeset(0): ShadowFB: preferred NO, enabled YES
(II) modeset(0): Output Virtual-1 connected
(II) modeset(0): Modeline "1280x800"x57.5   61.44  1280 1296 1312 1328 ...
(II) modeset(0): Output Virtual-1 using initial mode 1280x800 +0+0
(II) modeset(0): Damage tracking initialized
__DRM_XOPEN_OK__
__DRM_XCLIENT_OK__
  desktop pattern: left=(255, 0, 0) right=(0, 0, 255) ok=True
=== DRM-M3: PASS (smp=1) ===
```

Run:

```bash
# kernel (see drm-build-env: OSDK_TARGET_ARCH=riscv64)
cd kernel && OSDK_TARGET_ARCH=riscv64 cargo osdk build --scheme riscv --features riscv_sv39_mode
# initramfs
bash tools/riscv/nixos/drm/build_drm_m3.sh
# repack /tmp/drm-m3/boot.ext4 with asterinas.booti + initramfs.cpio.gz + qemu-virt.dtb
python3 tools/riscv/nixos/drm/boot_drm_m3.py
```

## Gotchas found (and worth remembering)

1. **`struct drm_mode_obj_get_properties` is 32 bytes, not 28.** Two `__u64`s
   followed by three `__u32`s leave 4 bytes of implicit trailing padding; the C
   `sizeof` (used in the `_IOWR` encoding) is 32. The Rust struct needs an
   explicit `pad: u32` field, or the `Pod` derive rejects it (`PaddingFree`
   not satisfied).
2. **The `dispatch_ioctl!` macro arm body must be a block.** `cmd @ X => Ok(0)`
   fails to compile ("no rules expected this token"); wrap it in `{ ... }`. For
   a `NoData` ioctl the binding is unused, so use `_cmd @ X => { ... }`.
3. **gzip decompression of a large initramfs hangs under QEMU TCG.** A 14 MB
   gzipped (35.6 MB raw) initramfs never finished decompressing (the kernel's
   `zune-inflate` path prints `unpacking initramfs` only *after* decompressing),
   and the uncompressed form stalled unpacking. **Stripping every ELF** cut it
   to 7.5 MB gzipped / 16 MB raw, which boots in well under a minute. This is a
   harness concern, not a kernel bug.
4. **The modesetting driver needs `libdrm` + `dri2proto` at build time.** The
   xserver's `build_modesetting` is gated on both; the cross tree had neither
   (libdrm had to be cross-compiled, and xorgproto's `dri2proto.pc` sat outside
   `pkg_config_libdir`).
5. **Non-glamor modesetting uses `drmModeAddFB`, not `ADDFB2`.** The driver
   falls through to the classic `DRM_MODE_FB_CMD` path when glamor is absent,
   so M2's `ADDFB` already covered the framebuffer registration.
6. **`failed to get plane resources: Inappropriate ioctl for device` is
   expected and non-fatal.** Without `MODE_GETPLANERESOURCES`, the driver logs
   this and continues without planes/atomic; the basic `SETCRTC`/`DIRTYFB` path
   does not need them.

## Next steps (DRM-M4+)

- **Real property enumeration** (`MODE_GETPROPERTY`, plane resources, `DPMS`/
  `CRTC_ID` props) so atomic/plane paths and RandR DPMS become available.
- **`MODE_ADDFB2`** for fourcc-based clients (glamor/EGL).
- **Cursor queue** (`UPDATE_CURSOR`/`MOVE_CURSOR`) over the already-set-up
  virtio-gpu cursor queue.
- **Page-flip vblank events** so `drmHandleEvent`-based flips complete instead
  of relying on the synchronous `DIRTYFB` present.
- **Pool reclaim** — destroyed dumb buffers still leak their span in the bump
  allocator.
