# DRM-M1 — virtio-gpu 2D from zero to a rendered scanout

Date: 2026-08-15
Branch: `track/drm`
Status: **PASS** — the guest 2D pipeline (`RESOURCE_CREATE_2D` → `ATTACH_BACKING`
→ `SET_SCANOUT` → `TRANSFER_TO_HOST_2D` → `FLUSH`) renders a red→blue gradient,
and a QEMU `screendump` confirms the host display received it (left edge red,
right edge blue). `/dev/dri/card0` opens and answers `DRM_IOCTL_VERSION`.

## Goal

Graphics was the last core subsystem with zero code. This milestone delivers the
first rendered frame:

1. a **virtio-gpu** device driver (`kernel/comps/virtio/src/device/gpu/`),
2. a minimal DRM character device (`/dev/dri/card0`),
3. QEMU verification via a host-side `screendump` gradient check.

## What was implemented

### 1. virtio-gpu spec + driver (`kernel/comps/virtio/src/device/gpu/`)

- `config.rs` — the 4-field config space (`events_read`/`events_clear`/
  `num_scanouts`/`num_capsets`). All device-specific feature bits (virgl, EDID,
  resource UUID, blob, context init) are cleared on negotiation — the MVP drives
  only the plain 2D path.
- `mod.rs` — wire types (`virtio_gpu_ctrl_hdr`, `virtio_gpu_rect`,
  `virtio_gpu_resource_create_2d`, `virtio_gpu_resource_attach_backing`,
  `virtio_gpu_mem_entry`, `virtio_gpu_set_scanout`,
  `virtio_gpu_transfer_to_host_2d`, `virtio_gpu_resource_flush`,
  `virtio_gpu_display_one`), request/response codes, and the global registry.
- `device.rs` — the `GpuDevice`:
  - control queue (idx 0): `GET_DISPLAY_INFO` → `RESOURCE_CREATE_2D` →
    `ATTACH_BACKING` → `SET_SCANOUT` → `TRANSFER_TO_HOST_2D` → `RESOURCE_FLUSH`;
  - cursor queue (idx 1) is set up for spec compliance but unused;
  - a synchronous, polling control path (like the sound/console drivers) — no IRQ
    handler is registered.

The device is wired into the existing virtio dispatch: `VirtioDeviceType::Gpu = 16`
was already present in `device/mod.rs`; `lib.rs` gained the
`init`/`negotiate_features`/dispatch arms.

### 2. DRM node (`kernel/src/device/dri.rs`)

A char device `/dev/dri/card0` (major 226, minor 0) backed by the first discovered
`GpuDevice`. `open()` succeeds when a virtio-gpu device was found at boot;
`DRM_IOCTL_VERSION` reports the driver name/date/description. Mode-setting and
buffer-sharing ioctls are left for later milestones. Registered only when a
virtio-gpu device exists (mirrors the fb/sound `init_in_first_kthread` pattern).

### 3. QEMU harness (`tools/riscv/nixos/drm/`)

- `init.c` — static riscv64 `/init` that opens `/dev/dri/card0`, queries the
  driver version, and prints `__DRM_*_OK__`/`__DRM_DONE__` markers.
- `build_drm.sh` — packs it into a cpio.gz initramfs.
- `boot_drm.py` — boots the U-Boot/`booti` flow with
  `-device virtio-gpu-device` + a unix monitor socket, waits for the guest
  markers, then `screendump`s and checks the gradient (left red, right blue).

## Design decisions

- **Synchronous, polling control queue** (like the sound driver's control path):
  each 2D command is a fixed or two-part request written into one DMA page, with
  the response read from a fixed offset. No IRQ handler is registered.
- **One backing entry**: the framebuffer is a single contiguous `DmaStream`
  (768×… frames), so `ATTACH_BACKING` uses one `virtio_gpu_mem_entry` with
  `addr = daddr()` and `length = width*height*4`.
- **Format** `B8G8R8X8_UNORM` (2), the default for the QEMU 2D backend.

## Gotchas found (and worth remembering)

1. **The 2D command codes are a contiguous enum from `0x0100`, not spaced-out
   constants.** `VIRTIO_GPU_CMD_RESOURCE_ATTACH_BACKING` is **`0x0106`**, not
   `0x0108`. `0x0108` is actually `GET_CAPSET_INFO`, so an ATTACH_BACKING sent as
   `0x0108` is rejected with `VIRTIO_GPU_RESP_ERR_UNSPEC` (`0x1200`) before the
   handler runs. Correct sequence:
   `GET_DISPLAY_INFO=0x0100, RESOURCE_CREATE_2D=0x0101, RESOURCE_UNREF=0x0102,
   SET_SCANOUT=0x0103, RESOURCE_FLUSH=0x0104, TRANSFER_TO_HOST_2D=0x0105,
   RESOURCE_ATTACH_BACKING=0x0106, RESOURCE_DETACH_BACKING=0x0107`.
   This was the "API 卡顿" — QEMU traces (`-trace virtio_gpu_*`) showed the
   command never reached `virtio_gpu_cmd_res_back_attach`.
2. **The control response is always the 24-byte header** for the 2D commands
   (NODATA response); `GET_DISPLAY_INFO` returns header + 16 `display_one`
   entries (408 bytes).
3. **`-trace virtio_gpu_*`** is the fastest way to see whether a command is
   recognized — each command has a trace event fired inside its handler after
   parsing.

## Verification

```
=== DRM-M1 guest result ===
__DRM_DONE__ __DRM_PASS__
  open node      : OK
  version ioctl  : OK
  screendump gradient: OK   (left=(243,0,12) right=(13,0,242))
=== DRM-M1: PASS (smp=1) ===
```

Run: `python3 tools/riscv/nixos/drm/boot_drm.py` (after building the kernel and
re-packing `/tmp/drm-m1/boot.ext4` with the DRM initramfs).

## Next steps (DRM-M2+)

- **Cursor queue**: `UPDATE_CURSOR`/`MOVE_CURSOR` over the (already-set-up) cursor
  queue.
- **Full DRM ioctls**: mode-setting (`DRM_IOCTL_MODE_GETRESOURCES`/…), dumb
  buffers, and mmap so Xorg's modesetting/glamor driver can use `/dev/dri/card0`.
- **Per-frame scanout updates** driven by a real display client instead of a
  one-shot boot-time test pattern.
