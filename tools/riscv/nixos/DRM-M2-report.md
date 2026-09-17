# DRM-M2 — a usable KMS device: dumb buffers, mmap, and mode setting

Date: 2026-08-15
Branch: `track/drm`
Status: **PASS** — a user-space program drives `/dev/dri/card0` *entirely* through
the standard DRM ioctls (no private/out-of-band hooks): it enumerates resources,
creates and mmaps a dumb buffer, draws into it, registers a framebuffer, and
flips it onto the scanout with `MODE_SETCRTC`. Two QEMU `screendump`s confirm
the frames reached the host display — a 1280x800 green→blue gradient, then a
640x400 solid-red frame after a **mode switch** that actually resizes the QEMU
scanout from 1280x800 to 640x400.

## Goal

DRM-M1 delivered the first rendered frame (virtio-gpu 2D + `/dev/dri/card0` +
a boot-time gradient). This milestone makes that device usable by a real
graphics client by completing the KMS ioctl surface and adding the two things
software rendering cannot work without: **dumb buffers** and **mmap**.

## What was implemented

### 1. KMS ioctl set (`kernel/src/device/dri.rs`)

`DriHandle` now owns per-open-file state (GEM/dumb handles and framebuffer ids
are namespaced per file, matching Linux's per-`drm_file` handle space) and
implements:

| ioctl | nr | what it does |
|---|---|---|
| `DRM_IOCTL_VERSION` | 0x00 | (unchanged) driver identity |
| `DRM_IOCTL_GET_CAP` | 0x0c | advertises `DUMB_BUFFER`, `DUMB_PREFERRED_DEPTH=24`, `DUMB_PREFER_SHADOW=0` |
| `DRM_IOCTL_SET_CLIENT_CAP` | 0x0d | accepts the common client caps and ignores the value |
| `DRM_IOCTL_MODE_GETRESOURCES` | 0xa0 | one CRTC / connector / encoder |
| `DRM_IOCTL_MODE_GETCONNECTOR` | 0xa7 | one preferred mode (native scanout size), `DRM_MODE_CONNECTOR_VIRTUAL` |
| `DRM_IOCTL_MODE_GETENCODER` | 0xa6 | `DRM_MODE_ENCODER_VIRTUAL`, crtc 1 |
| `DRM_IOCTL_MODE_GETCRTC` | 0xa1 | current fb + mode |
| `DRM_IOCTL_MODE_SETCRTC` | 0xa2 | present a framebuffer (the MODESET path) |
| `DRM_IOCTL_MODE_CREATE_DUMB` | 0xb2 | carve a buffer out of the pool |
| `DRM_IOCTL_MODE_MAP_DUMB` | 0xb3 | return the mmap offset for a buffer |
| `DRM_IOCTL_MODE_DESTROY_DUMB` | 0xb4 | drop a handle |
| `DRM_IOCTL_MODE_ADDFB` | 0xae | register a framebuffer over a dumb handle |
| `DRM_IOCTL_MODE_RMFB` | 0xaf | remove a framebuffer (dispatched by raw command; its arg is by-value) |

All ioctl numbers and struct layouts mirror the Linux `uapi/drm/drm.h` /
`drm_mode.h` definitions (magic `'d'`, `_IOWR`/`_IOW` encodings).

### 2. Dumb buffers and mmap

Dumb buffers are carved out of a **single physically-contiguous 16 MiB `Vmo`
pool** (bump allocator). This shape was chosen to satisfy two constraints at
once:

- `mmap` maps one `Mappable::Vmo` per file and selects a buffer by its *byte
  offset* within that VMO — so all buffers must live in one VMO. `MAP_DUMB`
  returns the buffer's page-aligned offset in the pool.
- `virtio-gpu`'s `RESOURCE_ATTACH_BACKING` takes a single guest-physical span,
  so each buffer must be physically contiguous. A contiguous pool guarantees
  every sub-range is too.

The pool's base physical address comes from a new `Vmo::paddr()` (page 0's
address, only meaningful for `VmoFlags::CONTIGUOUS` VMOs). `MODE_SETCRTC` then
passes `pool_paddr + buffer_offset` to the device.

### 3. virtio-gpu present path (`kernel/comps/virtio/src/device/gpu/`)

- `GpuDevice::present_framebuffer(addr, size, width, height)` runs
  `RESOURCE_CREATE_2D → ATTACH_BACKING → SET_SCANOUT → TRANSFER_TO_HOST_2D →
  FLUSH` for a caller-provided buffer, unref'ing the previously-presented
  resource first so repeated present calls (page flips / mode switches) don't
  leak.
- New `VirtioGpuResourceUnref` wire struct + `resource_unref` control command
  (`VIRTIO_GPU_CMD_RESOURCE_UNREF = 0x0102`).

### 4. One mmap invariant fix (`kernel/src/vm/vmar/vmar_impls/map.rs`)

The existing `Mappable::Vmo` path had a `debug_assert!` requiring the mapped VMO
to equal the file inode's page cache. That holds for regular/memfd files but not
for a char device returning a standalone VMO. The assertion is now skipped when
the inode has no page cache.

### 5. Harness (`tools/riscv/nixos/drm/`)

- `init_m2.c` — static riscv64 `/init` exercising the full ioctl flow above and
  drawing two frames (green→blue gradient, then half-resolution solid red).
- `build_drm_m2.sh` — packs it into a cpio.gz initramfs.
- `boot_drm_m2.py` — boots the U-Boot/`booti` flow with `virtio-gpu-device`,
  screendumps after each phase, and checks the gradient and the red frame.

## Design decisions

- **One contiguous VMO pool rather than per-buffer VMOs**: forced by the mmap
  API — `mappable()` returns a single `Mappable` for the file and the mmap
  offset is a byte offset into it. Per-buffer VMOs cannot be selected by offset
  without changing the core mmap plumbing.
- **`Mappable::Vmo` (not `IoMem`) for the dumb buffer**: `IoMem::acquire` only
  maps MMIO-region addresses, not ordinary RAM, so a RAM dumb buffer has to go
  through the VMO page-fault path.
- **The bump allocator never reclaims**: destroying a dumb buffer leaks its pool
  span. Fine for the handful of buffers a client allocates; noted as future
  work.
- **`ADDFB` validates** that the framebuffer's width/height/pitch/bpp match the
  referenced dumb buffer (the virtio-gpu resource is tightly packed, so any
  mismatch would corrupt the transfer).

## Gotchas found (and worth remembering)

1. **`debug_assert!` in `map.rs` panics for a char-device VMO mmap.** The
   `Mappable::Vmo` path asserted `vmo == inode.page_cache()`, which is `None`
   for `/dev/dri/card0`. Symptom: `Uncaught panic: called Option::unwrap() on a
   None value at .../map.rs:354`, reached only after every ioctl had already
   succeeded. Fix: skip the check when the inode has no page cache.
2. **The cross sysroot cannot compute DRM ioctl numbers.** `drm.h` pulls in
   `<asm/ioctl.h>`, which is absent from `/usr/riscv64-linux-gnu/include`, so
   `_IOWR(...)` expands to 0 and `DRM_IOCTL_*` macros are all zero. The C test
   hardcodes the numbers (as M1 did for `VERSION`). Verified by hand against
   the `_IOC` bit layout: `_IOWR` = dir `3<<30`, size `<<16`, magic `'d'=0x64<<8`.
3. **The scanout is 1280x800 by default** (not 1024x768) for `virtio-gpu-device`
   in this QEMU build; `GET_DISPLAY_INFO` remains the source of truth.
4. **`MODE_SETCRTC` genuinely resizes the display.** QEMU's virtio-gpu honors
   the `SET_SCANOUT` rect: presenting a 640x400 framebuffer made the next
   screendump 640x400, so multi-resolution mode switching is real, not emulated.

## Verification

```
=== DRM-M2 guest result ===
[DRM] open: OK              [DRM] get_cap: OK
[DRM] set_client_cap: OK    [DRM] get_resources: OK (crtc=1 connector=1 encoder=1)
[DRM] get_connector: OK (mode 1280x800 type=15)
[DRM] get_encoder: OK
[DRM] create_dumb1: OK      [DRM] map_dumb1: OK
[DRM] mmap1: OK             [DRM] draw1: OK
[DRM] addfb1: OK            [DRM] setcrtc1: OK
[DRM] modeset2: OK
__DRM_DONE__ __DRM_PASS__
  phase1 gradient (1280x800): left=(0,243,12) right=(0,13,242) OK
  phase2 red modeset (640x400): center=(255,0,0) OK
=== DRM-M2: PASS (smp=1) ===
```

Run:

```bash
# build the kernel (see drm-build-env: OSDK_TARGET_ARCH=riscv64)
cd kernel && OSDK_TARGET_ARCH=riscv64 cargo osdk build --scheme riscv --features riscv_sv39_mode
# pack the M2 initramfs
bash tools/riscv/nixos/drm/build_drm_m2.sh
# repack /tmp/drm-m2/boot.ext4 with asterinas.booti + initramfs.cpio.gz + qemu-virt.dtb
python3 tools/riscv/nixos/drm/boot_drm_m2.py
```

## Next steps (DRM-M3+)

- **Cursor queue**: `UPDATE_CURSOR`/`MOVE_CURSOR` over the already-set-up cursor
  queue.
- **`MODE_PAGE_FLIP` / `MODE_DIRTYFB`**: incremental present without a full
  modeset; currently every redraw is a `SETCRTC`.
- **`MODE_ADDFB2`** for clients (Xorg modesetting/glamor) that prefer the
  fourcc-based API.
- **Pool reclaim + sizing**: free destroyed buffers and size the pool to the
  mode instead of a fixed 16 MiB.
- **Cursor/atomic client caps**: stop accepting `DRM_CLIENT_CAP_ATOMIC`
  until atomic modesetting exists.
