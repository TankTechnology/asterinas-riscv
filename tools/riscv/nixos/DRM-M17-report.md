# DRM-M17: Atomic Modesetting + virgl Raw-Ioctl Verification

**Status:** M17 atomic modesetting **PASS (55/55)**; virgl 3D raw-ioctl
path **PASS (15/15, incl. pixel round-trip through host virglrenderer)**.
The later Debian Mesa path also passes direct DRI3 rendering; the historical
Alpine packaging limitation is retained below for context.

## 2026-08-27 atomic UAPI correction

The original 24-check M17 gate used a private flattened interpretation of
`drm_mode_atomic`: its second field was treated as a total property count and
`count_props_ptr` was ignored.
The kernel and test therefore agreed with each other but not with the Linux
wire ABI.

The corrected 56-byte structure now uses `count_objs`, an object-id array, a
per-object property-count array, and flattened property/value arrays exactly
as Linux UAPI clients send them.
CRTC, connector, encoder, and primary-plane ids are globally unique
(`1`, `2`, `3`, and `4`), so each atomic property can be validated against one
unambiguous object type.
The parser bounds objects and properties, rejects null arrays, duplicate
objects/properties, nonzero reserved fields, invalid object references, and
property/object mismatches before changing state.

The replacement guest gate now passes 55/55 checks.
It additionally proves the Linux ioctl number `0xc03864bc`, unique object
enumeration, TEST_ONLY state preservation, object/property mismatch rejection,
reserved-field rejection, complete proposed-state validation, transactional
pipeline disable/restore, and coherent property read-back.
The complete Debian Mesa/Xorg/Xfce virgl regression also passed with DRI3,
direct rendering, a correct output pixel, no command-stream errors, and
`XFCE_DRM_PASS`.

## What changed since M16

### Atomic modesetting (`kernel/src/device/drm/`)

The M16 tree had the atomic ioctls wired into the dispatch table
(`atomic.rs`, `plane.rs`, `property.rs`) but the property enumeration side
was a stub: `MODE_OBJ_GETPROPERTIES` always returned `count_props = 0` and
there was no `MODE_GETPROPERTY` / `MODE_GETPROPBLOB`, so no real userspace
(wlroots, modetest) could discover property ids and the atomic path was
unusable dead code. This milestone completes the discovery surface:

- `MODE_GETPLANERESOURCES` / `MODE_GETPLANE` (0xb5/0xb6) — single primary
  plane (id 4), XR24/AR24 formats, `possible_crtcs = 1`.
- `MODE_OBJ_GETPROPERTIES` (0xb9) — real per-object-type property lists:
  CRTC {ACTIVE, MODE_ID}, connector {CRTC_ID}, plane {type, FB_ID, CRTC_ID,
  SRC_X/Y/W/H, CRTC_X/Y/W/H}; values read back from the property store with
  coherent disabled defaults (ACTIVE=0, MODE_ID=0, object references=0,
  type=Primary).
- `MODE_GETPROPERTY` (0xaa) — name/flags/range; UAPI flag bits
  (RANGE/ENUM/BLOB/OBJECT/SIGNED_RANGE) mapped from the internal property
  type; plane `type` is ENUM + IMMUTABLE with Overlay/Primary/Cursor entries.
- `MODE_GETPROPBLOB` (0xac) / `MODE_CREATEPROPBLOB` (0xbd) /
  `MODE_DESTROYPROPBLOB` (0xbe) — blob round-trip for `MODE_ID`.
- `MODE_ATOMIC` (0xbc) — TEST_ONLY validation (property existence,
  object-type applicability, range checks) and real commit
  (`ALLOW_MODESET` consumes the MODE_ID blob; FB_ID commits present via
  the existing `present_fb` path).
- `MODE_ADDFB2` (0xb8) — explicit fourcc + pitches; validates the GEM
  object dimensions.

### virgl 3D fixes (`kernel/src/device/drm/virtio_gpu.rs`)

- **GEM ↔ resource id namespace split.** `TRANSFER_TO/FROM_HOST_3D` used
  the GEM object id as the virtio resource id, but resources are allocated
  from a separate counter (`GpuDevice::next_resource_id`). A
  `gem_resources` map (GEM object id → resource id) is now maintained by
  `RESOURCE_CREATE` and consulted by the transfer ioctls.
- **Double resource creation.** `RESOURCE_CREATE` with a backing buffer
  issued both `RESOURCE_CREATE_2D` (hardcoded X8R8G8B8) and
  `RESOURCE_CREATE_3D` for the same id. Now only `RESOURCE_CREATE_3D` is
  issued (gallium pipe target passed through), with `ATTACH_BACKING`.
- **`RESOURCE_CREATE` returned `size = 0`**, which makes Mesa allocate
  zero-sized buffers. Now returns the GEM object size and `width * 4`
  stride.
- **`GETPARAM SUPPORTED_CAPSET_IDS` returned 0** (no capsets). Now returns
  the virgl|virgl2 bitmask (6).
- **`GETPARAM` wrote the result inline into the ioctl struct.** The UAPI
  field `value` is a userspace *pointer* to the `u64` result
  (`struct drm_virtgpu_getparam`); Mesa reads through the pointer and
  always saw 0, disabling the whole virgl winsys. Now written through the
  pointer.
- **UAPI layout mismatches fixed:** `drm_virtgpu_get_caps` (cap_set_id /
  cap_set_ver are `u64`, and the struct needs 4 bytes of tail padding to
  reach the UAPI `sizeof` of 32 — the mismatch made the typed ioctl
  dispatch reject `0xc0206449` as unknown), `drm_virtgpu_map` (offset field
  is first), and `drm_virtgpu_context_init` (ctx_set_params pointer is
  first).
- **`GET_CAPS` with `cap_set_ver == 0`** now resolves to the device's max
  capset version instead of asking the device for version 0.
- **Driver name.** `DRM_IOCTL_VERSION` reported `virtio-gpu`; Mesa's
  loader builds the DRI driver file name from this string
  (`<name>_dri.so`) and could not find `virtio-gpu_dri.so`, failing
  `eglInitialize`. Renamed to `virtio_gpu`.

### fcntl fix (`kernel/src/syscall/fcntl.rs`)

`F_DUPFD`/`F_DUPFD_CLOEXEC` wrongly returned `EINVAL` when `arg == fd`
("target fd equals the source fd"). Real Linux accepts it and returns the
lowest free fd ≥ `arg` (verified against the host kernel). Mesa's GBM EGL
init calls `fcntl(gbm_fd, F_DUPFD_CLOEXEC, 3)` where the gbm fd is 3 —
this single check made `eglInitialize` fail with "DRI2: failed to fcntl()
existing gbm device".

### Warning cleanup and drive-by fixes

The in-progress M16/M17 code carried ~40 warnings (CI lints with
`-Dwarnings`): unused imports/constants, redundant `prelude` imports,
`ioctl!`-style braces in `Ioctl<...>` const generics, the duplicated
`ioctl_defs` module in `virtio_gpu.rs`, a stale `#[expect(dead_code)]`
in `device/registry/char.rs`, and a handful of pre-existing lints in
`snd/`/`rseq.rs`. All fixed; clippy is clean for `aster-kernel` +
`aster-virtio` under `-Dwarnings`.

The loop-device `open()` path in `device/registry/block.rs` was disabled
by a prior workaround; it is restored via a proper
`r#loop::lookup_device(DeviceId)` helper instead of an impossible
`Arc<dyn BlockDevice>` downcast.

## Verification

### M17: atomic modesetting — PASS (55/55)

`tools/riscv/nixos/m17/atomictest.c` runs as `/init` on a minimal
initramfs (`build_m17.sh`, `boot_m17.py`, artifacts in `target/drm-m17/`),
QEMU `virtio-gpu-device`, smp=4 (the DTB carries 4 CPUs; smp mismatch is
the known M10 failure mode).

Checks: open/SET_MASTER, SET_CLIENT_CAP UNIVERSAL_PLANES+ATOMIC, globally
unique KMS object ids, plane resources/plane info, property counts per object
type (2/1/11), property
discovery by name, GETPROPERTY details (ACTIVE range [0,1]; type enum with
Primary entry + IMMUTABLE), CREATE_DUMB + ADDFB2, bounded blob create/read-back,
per-file blob ownership, committed-blob lifetime, Linux-layout ATOMIC
TEST_ONLY, TEST_ONLY state preservation, two-stage array capacity handling,
invalid object/property and reserved-field rejection, explicit NONBLOCK
rejection, per-file client-capability gates, rejection of an unimplemented
writeback capability, TEST_ONLY commit-equivalent validation, exact mode-blob
and timing validation, full-frame source/destination geometry validation,
rejection of an active CRTC without its primary plane, transactional pipeline
disable/restore and full disconnect, ATOMIC ALLOW_MODESET commit,
OBJ_GETPROPERTIES read-back of committed MODE_ID/ACTIVE, and render-node
rejection (EOPNOTSUPP) of KMS ioctls.

```
Summary: PASS=55 FAIL=0
M17_ATOMIC_PASS
```

### virgl EGL bring-up — in progress

With the fresh kernel the eglrender client (Alpine rootfs, GBM + EGL/GLES2
on `/dev/dri/renderD128`) now reaches Mesa initialization:

- Before: kernel built from a stale backup tree panicked at boot
  (`NotIncludeAllComponent`), evidence run never reached userspace.
- Run 1 (fresh kernel): `M16_GBM_BACKEND drm` OK, then
  `MESA-LOADER: failed to retrieve device information` and
  `M16_EGL_FAIL eglInitialize` — root-caused to the `virtio-gpu` vs
  `virtio_gpu` driver-name mismatch.
- Run 2 (driver name fixed): still `eglInitialize` — root-caused to the
  `F_DUPFD_CLOEXEC` `arg == fd` EINVAL bug (see above).
- Run 3 (fcntl fixed): **`eglInitialize` succeeds** (EGL 1.5, vendor
  "Mesa Project"), but `eglChooseConfig` finds zero configs. Kernel-side
  ioctl tracing shows Mesa never issues a single virtgpu ioctl; the
  process maps show no DRI driver was loaded. Root cause: **Alpine's Mesa
  does not build the virgl gallium driver on any architecture**
  (`_gallium_drivers="r300,r600,radeonsi,nouveau,llvmpipe,zink"` in
  main/mesa/APKBUILD; `virtio_gpu_dri.so` is a stub symlink to
  `libdril_dri.so`). A virgl-capable Mesa must be built from source to go
  further on the EGL path.

### virgl raw ioctl verification — PASS (15/15)

`tools/riscv/nixos/m16/virgltest.c` (static, no libdrm/Mesa) exercises the
kernel's 3D ioctl path directly against the host virglrenderer
(`-device virtio-gpu-gl-device`, egl-headless):

- GETPARAM 3D_FEATURES=1, SUPPORTED_CAPSET_IDS=0x6 — PASS
- GET_CAPS virgl v1 (308 bytes, real capset data) — PASS
- CREATE_DUMB + MAP_DUMB + mmap — PASS
- RESOURCE_CREATE (PIPE_TEXTURE_2D + backing) — PASS
- RESOURCE_INFO — PASS
- TRANSFER_TO_HOST 64x64 pattern upload — PASS
- EXECBUFFER (single VIRGL_CMD_NOP dword) — PASS
- TRANSFER_FROM_HOST + WAIT — PASS
- **pixel round-trip through the host: 4096/4096 pixels match** — PASS

Host quirk: this host's virglrenderer 1.3.0 returns zeroed data for virgl
capset **version 2** (v1 is fine). The kernel passes the requested version
through, mapping 0 → the device-advertised max version, like Linux.

### virtio control-queue fix (`kernel/comps/virtio/.../gpu/device.rs`)

`submit_control` required the device to fill exactly `resp_len` bytes, but
`GET_CAPSET` responses carry the *actual* capset size (308), not the
maximum (1424) — the mismatch spun the queue forever. `submit_control` now
returns the actual used length (minimum: the header) and `get_capset`
truncates to it.

Also observed: `Unimplemented syscall number: 258` (`riscv_hwprobe`) from
the Mesa loader — non-fatal so far, but a candidate if CPU-feature probing
turns out to be required.

## Known limitations

- Atomic commits are synchronous; `DRM_MODE_ATOMIC_NONBLOCK` is rejected with
  `EOPNOTSUPP` until a true asynchronous path exists.
- Page-flip completion events are supported, but are queued immediately after
  synchronous presentation rather than from a hardware-vblank IRQ.
- Modes are validated exactly, including timing relationships and agreement
  with the framebuffer dimensions. The virtio-gpu backend still exposes one
  fixed scanout rather than dynamic connector mode programming.
- The primary plane currently implements only an unscaled, uncropped,
  origin-zero full-frame scanout. Other valid DRM plane geometries return
  `EOPNOTSUPP` instead of being silently ignored.
- `RESOURCE_BLOB`/`HOST_VISIBLE`/`CONTEXT_INIT` GETPARAMs report 0;
  Mesa uses the default virgl context (ctx_id 0).
- Alpine Mesa cannot drive the virgl path: its gallium driver set
  (`r300,r600,radeonsi,nouveau,llvmpipe,zink`) has no virgl on any arch,
  so `eglChooseConfig` comes up empty. A source-built Mesa with virgl is
  needed for the EGL/kmscube demo; the kernel side is verified by
  virgltest's raw ioctl run instead.

## Files

| File | Purpose |
|------|---------|
| `tools/riscv/nixos/m17/atomictest.c` | Atomic modesetting verification (runs as /init) |
| `tools/riscv/nixos/m17/build_m17.sh` | Build atomictest + initramfs + boot disk |
| `tools/riscv/nixos/m17/boot_m17.py` | QEMU boot + evidence check |
| `tools/riscv/nixos/m16/virgltest.c` | Raw virgl 3D ioctl verification |
| `tools/riscv/nixos/m16/eglrender.c` | GBM/EGL bring-up probe (blocked on Alpine Mesa) |
| `kernel/src/device/drm/property.rs` | Property store + enumeration ioctls |
| `kernel/src/device/drm/atomic.rs` | MODE_ATOMIC validation + commit |
| `kernel/src/device/drm/plane.rs` | Plane resources/plane info ioctls |
| `kernel/src/device/drm/virtio_gpu.rs` | virgl 3D ioctls (UAPI + resource id fixes) |
| `kernel/src/device/drm/mod.rs` | Dispatch + `virtio_gpu` driver name |
| `kernel/comps/virtio/src/device/gpu/device.rs` | Variable-length control responses |
| `kernel/src/syscall/fcntl.rs` | F_DUPFD arg==fd fix |
