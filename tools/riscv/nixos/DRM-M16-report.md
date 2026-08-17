# DRM-M16 Report — VT Node Verification + GEM/Render-Node + virgl 3D

**Date:** 2026-08-18  
**Branch:** `track/drm`  
**Commits:** `0d5c443f8` (VT report), `a7106b425` (GEM+render node), `949557eeb` (virgl wire types)  

## Phase 1: /dev/ttyN VT Node Verification

### Results

| Device | Present | Type | Major | Minor |
|--------|---------|------|-------|-------|
| `/dev/tty0` | **YES** | char | 4 | 0 |
| `/dev/tty1`–`/dev/tty63` | **YES** (63/63) | char | 4 | 1–63 |
| `/dev/tty` | **YES** | char | 5 | 0 |
| `/dev/console` | **YES** | char | 5 | 1 |
| `/dev/dri/card0` | **YES** | char | 226 | 0 |
| `/dev/dri/renderD128` | **YES** | char | 226 | 128 |

**Critical QEMU args:** `-cpu rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true` —
plain `-cpu rv64` causes OpenSBI Load Page Fault. Bootargs: `console=ttyS0` (not `tty0`).

## Phase 2a: GEM Object Model + Render Node

### Architecture

```
kernel/src/device/drm/
├── mod.rs    — GpuManager (shared state), DriPrimary (card0), DrmRender (renderD128),
│              DriHandle (per-open-file), ioctl dispatch, wire types
├── gem.rs    — GEM_CLOSE, GEM_FLINK, GEM_OPEN
├── dumb.rs   — CREATE_DUMB, MAP_DUMB, DESTROY_DUMB (via GEM objects)
├── kms.rs    — KMS ioctls (SETCRTC, PAGE_FLIP, DIRTYFB, cursor, etc.)
└── ioctl.rs  — ioc!() type aliases for all ioctls
```

### What changed

1. **GEM object wrapping**: Dumb buffers are now wrapped in `GemObject` with
   `Arc`-based reference counting. Each object has a `name: AtomicU32` for
   global FLINK names and a `ref_count: AtomicU32`.

2. **Shared state**: `GpuManager` (singleton) holds:
   - The dumb-buffer pool (`Arc<Vmo>`)
   - The GEM object table (`BTreeMap<u32, Arc<GemObject>>`)
   - The global FLINK name→id map (`BTreeMap<u32, u32>`)

3. **Render node**: `/dev/dri/renderD128` (major=226, minor=128) registered
   alongside `/dev/dri/card0`. Both share the same `GpuManager`. KMS ioctls
   are rejected on the render node with `EOPNOTSUPP`.

4. **GEM ioctls**: `GEM_CLOSE` (0x09), `GEM_FLINK` (0x0a), `GEM_OPEN` (0x0b)
   implemented. FLINK uses the object's own id as its global name.

5. **`DRM_CAP_PRIME`**: Advertises `DRM_PRIME_CAP_IMPORT | DRM_PRIME_CAP_EXPORT` (0x3).
   Actual PRIME fd import/export is deferred — the kernel has no dma-buf subsystem.

6. **Per-file handle namespace**: `DriInner::handles: BTreeMap<u32, u32>`
   maps per-file handles → GEM object ids. Handles are namespace-per-file
   (matching Linux's per-`drm_file` semantics).

## Phase 2b: virtio-gpu 3D Wire Types + virgl Feature Negotiation

### What changed

1. **3D wire types** added to `kernel/comps/virtio/src/device/gpu/mod.rs`:
   - Command constants: CTX_CREATE (0x0200), CTX_DESTROY (0x0201),
     CTX_ATTACH/DETACH_RESOURCE (0x0202/0x0203), RESOURCE_CREATE_3D (0x0204),
     TRANSFER_TO/FROM_HOST_3D (0x0205/0x0206), SUBMIT_3D (0x0207)
   - Feature flags: VIRTIO_GPU_F_VIRGL (bit 0), F_EDID (bit 1),
     F_RESOURCE_UUID (bit 2), F_RESOURCE_BLOB (bit 3), F_CONTEXT_INIT (bit 4)
   - Capset IDs: VIRTIO_GPU_CAPSET_VIRGL=1, VIRTIO_GPU_CAPSET_VIRGL2=2
   - Structs: VirtioGpuGetCapsetInfo, VirtioGpuRespCapsetInfo,
     VirtioGpuGetCapset, VirtioGpuCtxCreate, VirtioGpuCtxDestroy,
     VirtioGpuCtxResource, VirtioGpuResourceCreate3d, VirtioGpuBox,
     VirtioGpuTransferHost3d, VirtioGpuCmdSubmit

2. **Feature negotiation**: `negotiate_features()` now returns
   `features & VIRTIO_GPU_F_VIRGL` — virgl is enabled if the device offers it.

### Still needed for full virgl

- DRM virtio-gpu ioctls in the DRM layer: `DRM_IOCTL_VIRTGPU_EXECBUFFER`,
  `DRM_IOCTL_VIRTGPU_RESOURCE_CREATE`, `DRM_IOCTL_VIRTGPU_CONTEXT_INIT`,
  `DRM_IOCTL_VIRTGPU_GET_CAPS`, `DRM_IOCTL_VIRTGPU_GETPARAM`
- Wire up the virtio-gpu 3D control commands in `GpuDevice`

## Phase 2c: Mesa virgl Cross-Compile (pending)

Cross-compile mesa virgl driver for riscv64 + glmark2 for rendering
pipeline verification. This is a separate user-space workstream that
does not require kernel changes beyond what's already done.

## Files

| File | Purpose |
|------|---------|
| `tools/riscv/nixos/m16/init.c` | VT verification init |
| `tools/riscv/nixos/m16/build_m16_vt.sh` | Build initramfs |
| `tools/riscv/nixos/m16/boot_m16_vt.py` | QEMU boot script |
| `tools/riscv/nixos/DRM-M16-report.md` | This report |
| `kernel/src/device/drm/mod.rs` | DRM module: GpuManager, devices, ioctl dispatch |
| `kernel/src/device/drm/gem.rs` | GEM ioctls |
| `kernel/src/device/drm/dumb.rs` | Dumb buffer allocation |
| `kernel/src/device/drm/kms.rs` | KMS ioctls |
| `kernel/src/device/drm/ioctl.rs` | ioctl type aliases |
| `kernel/comps/virtio/src/device/gpu/mod.rs` | virtio-gpu wire types (2D + 3D) |
| `kernel/comps/virtio/src/device/gpu/device.rs` | GpuDevice + feature negotiation |

## Previous Milestones

| Milestone | Summary |
|-----------|---------|
| M1 | virtio-gpu 2D bring-up |
| M2 | Full KMS ioctl set + dumb-buffer mmap |
| M3 | Xorg modesetting driver on `/dev/dri/card0` |
| M4 | Hardware cursor (MODE_CURSOR/2) |
| M5 | Xorg + ALSA + NetSurf integration boot |
| M6 | Rollup PR #43 + ACCEPTANCE.md |
| M7 | ext2 persistence (two-boot) |
| M8 | `/dev` auto-create fix + DRM desktop main chain |
| M9 | smp=4 root cause + xbench render harness |
| M10 | DTB -m/-smp mismatch root cause |
| M11 | smp=4 desktop PASS + SMP deep-dive |
| M12 | DTB self-consistency check (PR #55) |
| M14 | fbdev-vs-modesetting A/B benchmark + mode-switch + virgl pre-research |
| M15 | Multi-resolution SETCRTC matrix 6/6 + Weston Alpine smoke |
| **M16** | **VT nodes 63/63 PASS + GEM/render-node + virgl wire types** |