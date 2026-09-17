# DRM-M19: Real Mesa Userspace — GBM/EGL + kmscube on Asterinas

**Status:** PASS. Real Mesa 25.0.7 (Debian riscv64) renders end-to-end on
the kernel's DRM: EGL/GLES2 windowed pipeline with atomic commits and
page-flip events (eglrender2, 4/4 frames), plus kmscube running its
render loop (9 frames reported before the harness timeout).

## Why Debian instead of Alpine

Alpine's Mesa does not build the virgl gallium driver on any architecture
(`_gallium_drivers="r300,r600,radeonsi,nouveau,llvmpipe,zink"` in
main/mesa/APKBUILD), so its `virtio_gpu_dri.so` is a stub symlink.
Debian trixie riscv64 Mesa 25.0.7 does include virgl
(`debian/rules`: `GALLIUM_DRIVERS += nouveau r300 r600 virgl`).

`fetch_debian_rootfs.sh` resolves the runtime dependency closure of
kmscube + Mesa against the Debian Packages index and extracts the .debs
into `target/m19/rootfs/` (~190 MB after trimming locale/doc).

## Kernel fixes found by real Mesa

- **ADDFB2/ADDFB accepted only exact-dimension buffers.** llvmpipe
  over-allocates (1280x800 surface → 1280x832 dumb buffer) and registers
  the fb at the surface size. Both paths now validate "fits within the
  buffer" instead of exact-match.
- **Dumb pool 16 MiB → 64 MiB.** kmscube's GBM surface + shadow buffers
  exceeded 16 MiB (CREATE_DUMB ENOMEM mid-run).

## What was verified

- `virgltest` (raw ioctls, static): 15/15 — the 3D data path against the
  host virglrenderer, incl. 64x64 pixel round-trip. (Same as M17 run.)
- `eglrender2` (kmscube-style, glibc): GBM surface → EGL window surface
  (llvmpipe GLES 3.2) → 4 frames rendered, each ADDFB2 → atomic commit
  with `PAGE_FLIP_EVENT` → flip event read back (seq 0..3), per-frame
  pixel checksums all distinct, final frame dumped as PPM.
- `kmscube -D /dev/dri/card0`: full legacy KMS init (GETRESOURCES,
  GETCONNECTOR, SETCRTC) + GBM + EGL + continuous PAGE_FLIP loop —
  `Rendered N frames` reports until the 60 s harness timeout (expected
  SIGTERM, RC=143).
- A/B reference (`boot_linux_ref.py`): the same initramfs boots on stock
  Debian Linux 6.12 (QEMU direct kernel boot) — virtio-gpu module chain
  loads but the device reports status FAILED there; noted for future
  comparison, not on the critical path.

## Known open item: Mesa picks llvmpipe, not virgl

On both Alpine and Debian stacks, Mesa never issues a single virtgpu
ioctl — it silently falls back to kms_swrast/llvmpipe. The evidence says
this is Mesa-side driver selection, not our ioctl surface:

- The kernel's virgl path is fully exercised and verified by virgltest
  (raw ioctls, host pixel round-trip).
- The Linux A/B reference currently fails earlier (QEMU device status
  FAILED), so it could not yet produce the comparison sequence.
- Forcing `GALLIUM_DRIVER=virgl` makes `gbm_create_device` fail with
  ENOENT (a module lookup), pointing at Mesa's pipe-loader plumbing on
  this rootfs.

Rendering works correctly via llvmpipe; virgl activation is a
performance optimization to revisit later (next step: get the Linux A/B
reference working and diff the two ioctl sequences).

## Iteration speed notes

- A QEMU cycle is ~8–14 min (TCG riscv64 emulation of a 190 MB userland);
  the kernel `drm ioctl cmd=` trace was essential for attribution and is
  removed again from the committed kernel.
- `tools/riscv/nixos/m19/ioctltrace.so` (LD_PRELOAD) decodes DRM ioctls
  from userspace without kernel rebuilds.

## Files

| File | Purpose |
|------|---------|
| `tools/riscv/nixos/m19/fetch_debian_rootfs.sh` | Debian dep-closure fetcher (target/m19/) |
| `tools/riscv/nixos/m19/build_m19.sh` | Build test binaries + initramfs + boot disk |
| `tools/riscv/nixos/m19/boot_m19_virgl.py` | QEMU boot + evidence verdict |
| `tools/riscv/nixos/m19/boot_linux_ref.py` | Stock-Linux A/B reference boot |
| `tools/riscv/nixos/m19/eglrender2.c` | kmscube-style EGL/atomic/flip-event client |
| `tools/riscv/nixos/m19/ioctltrace.c` | LD_PRELOAD DRM ioctl logger |
| `tools/riscv/nixos/m19/init.sh` | In-guest init (insmod chain is Linux-only) |
| `kernel/src/device/drm/kms.rs` | ADDFB/ADDFB2 fit-within-buffer validation |
| `kernel/src/device/drm/mod.rs` | 64 MiB dumb pool |
