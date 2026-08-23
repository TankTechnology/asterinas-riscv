# DRM-M20: virgl enablement groundwork — streaming initramfs, PRIME, sysfs, virtio-pci fixes

**Status:** kernel-side virgl is complete and verified; the remaining blocker is
userspace (Debian's riscv64 Mesa is missing the virgl DRM winsys).

## Summary

This milestone chases "why does Mesa never select virgl?" and, along the way,
fixes several real kernel defects. The end state:

- The **kernel** side of virgl (virtio-gpu 3D ioctls) is complete and verified:
  `virgltest` passes 15/15, including a host-side pixel round-trip through
  virglrenderer.
- Mesa still renders with `llvmpipe` and issues **zero** `DRM_IOCTL_VIRTGPU_*`
  ioctls, because the **Debian riscv64 Mesa 25.0.7 build lacks the virgl DRM
  winsys** (see "Root cause" below).

## Kernel changes (all verified by booting the kernel in QEMU)

### 1. Streaming initramfs gzip decompression (`kernel/src/fs/rootfs.rs`)

The old code used `zune_inflate::DeflateDecoder::decode_gzip()`, which allocates
the **entire** decompressed archive as one contiguous `Vec`. For a ~430 MiB
initramfs this hung the kernel (or failed with a large-slot allocation error).
This is the Linux-style "治本" fix: decompress streamingly.

- `zune-inflate` (one-shot, explicitly non-streaming) replaced with
  `libflate::gzip::Decoder` (`#![no_std]`, `forbid(unsafe_code)`, already in the
  tree), which feeds the CPIO parser incrementally.
- `kernel/libs/cpio-decoder/src/lib.rs`: bump the file-copy buffer from 4 KiB to
  `min(64 KiB, file_size)` so large files do fewer read/write round-trips without
  wasting memory on small files.

### 2. virtio-pci: clamp the virtio-gpu cursor queue (`kernel/comps/virtio/src/device/gpu/device.rs`)

Booting with `-device virtio-gpu-gl-pci` (virtio-pci transport) exposed
`Device initialization error: Err(InvalidQueueArgs)`: QEMU's virtio-gpu cursor
queue max size is **16**, but the driver hard-coded **64** for both queues.
The driver now clamps each queue to the device's `max_queue_size`.

### 3. sysfs DRM device info (`kernel/src/fs/fs_impls/sysfs/dev.rs`)

Mesa's loader reads the PCI vendor/device id from
`/sys/dev/char/<major>:<minor>/device/{vendor,device}` to select the GPU driver.
Expose that path for the DRM card0 (`226:0`) as `0x1af4` / `0x1050`
(virtio-gpu), which makes Mesa's loader successfully retrieve the device info.

### 4. Fix DRM version name truncation (`kernel/src/device/drm/mod.rs`)

`copy_field` reserved one byte for the NUL terminator by copying
`min(len, buf_len - 1)` bytes. When Mesa passed a buffer sized exactly to the
name, `"virtio_gpu"` (10 chars) was truncated to `"virtio_gp"`, so Mesa looked
for `virtio_gp_dri.so` / `virtio_gp_gbm.so` and fell back to software. Fixed to
match Linux's `drm_version`: copy `min(len, buf_len)` bytes and only append NUL
when there is room.

### 5. PRIME fd<->handle ioctls (`kernel/src/device/drm/{prime.rs,ioctl.rs,mod.rs}`)

Implemented `DRM_IOCTL_PRIME_HANDLE_TO_FD` (0x2d) and
`DRM_IOCTL_PRIME_FD_TO_HANDLE` (0x2e) with a `DmaBufFile` (`FileLike`) wrapping
the dumb-buffer pool. Isolated-verified with `primetest`
(`M20_PRIME_PASS`). These are the right complement for GBM buffer sharing, but
turn out **not** to be the virgl blocker.

## Test harness

- `tools/riscv/nixos/m19/primetest.c` — static-musl PRIME round-trip test.
- `tools/riscv/nixos/m19/ioctltrace.c` — LD_PRELOAD DRM ioctl + `dlopen` logger
  (decodes `VIRTGPU_*`, `PRIME_*`, and logs Mesa's `.so` loads).
- `tools/riscv/nixos/m19/build_mini_rootfs.py` — builds a ~164 MiB minimal
  virgl test rootfs (eglrender2 + Mesa core + LLVM) from the Debian rootfs.
- `tools/riscv/nixos/m19/boot_m19_virgl.py` — now boots `virtio-gpu-gl-pci`.

## Root cause: Debian riscv64 Mesa lacks the virgl DRM winsys

With the kernel fixes in place, the `dlopen` trace shows Mesa correctly using
`virtio_gpu` (not `virtio_gp`) and reading `0x1af4:0x1050`, yet it still renders
with `llvmpipe` and issues zero `DRM_IOCTL_VIRTGPU_*` ioctls.

Searching every Mesa `.so` (`libgallium`, `libdril_dri.so`, `dri_gbm.so`,
`libEGL_mesa.so`) for the `DRM_IOCTL_VIRTGPU_*` command bytes finds **nothing**.
The `virgl` gallium driver is present (and the vtest winsys, for
`virgl_test_server`), but the **DRM winsys** — the part that talks to virtio-gpu
via `DRM_IOCTL_VIRTGPU_*` — is not compiled. This mirrors openSUSE's Mesa spec,
which excludes `virgl` on riscv64.

So the kernel is ready; the userspace Mesa must be rebuilt with the virgl DRM
winsys:

```sh
meson build --cross-file riscv64-cross.txt \
  -Ddri-drivers= \
  -Dgallium-drivers=virgl,swrast \
  -Dvulkan-drivers= \
  -Dllvm=disabled \
  -Degl=true -Dgbm=true -Dgles2=true \
  -Dplatforms=x11,wayland \
  -Dglx=dri -Dshared-glapi=true \
  -Dbuildtype=release
ninja -C build
```

## References

- [Bug 107309](https://bugs.freedesktop.org/show_bug.cgi?id=107309) — the
  "failed to retrieve device information" loader warning is harmless (libdrm
  falls back to the DRM name for virtio-gpu).
- [Mesa loader patch](https://lists.freedesktop.org/archives/mesa-dev/2018-April/191216.html)
  — virgl's first probe is `DRM_VIRTGPU_GETPARAM(3D_FEATURES)`.
- [mesa-dev 2025](https://lists.freedesktop.org/archives/mesa-dev/2025-June/226514.html)
  — the GBM backend lookup path (`<driver>_gbm.so`).
- [openSUSE Mesa.spec](https://src.opensuse.org/pool/Mesa/blame/commit/3f7c112887519cc64350db1e41e2efa7d3249068235d2f406e3dcacb2652b0af/Mesa.spec)
  — riscv64 omits `virgl`.
- [freedesktop-sdk mesa.bst](https://freedesktop-sdk.gitlab.io/documentation/reference/api-reference/fdsdk/elements/extensions/mesa/mesa.html)
  — riscv64 recipe that does include `virgl`.
