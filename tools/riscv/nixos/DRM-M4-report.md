# DRM-M4 — hardware cursor, upstream PR rollup, and virgl feasibility

Date: 2026-08-15
Branch: `track/drm`
Status: **PASS** — the legacy hardware-cursor path (`MODE_CURSOR` /
`MODE_CURSOR2`) now drives a dumb-buffer-backed cursor through virtio-gpu's
cursor queue, verified end-to-end against QEMU. The M1–M3 kernel work has been
rolled up into a review-focused PR against `main`, and the virgl 3D path has
been assessed (conclusion: feasible but a large, orthogonal effort — deferred).

## Goal

DRM-M3 delivered a real Xorg `modesetting` desktop on `/dev/dri/card0`. This
milestone has three parts:

1. **Upstream rollup** — extract the kernel-side DRM commits (virtio-gpu 2D
   driver + the full KMS ioctl surface) from `track/drm` into a clean PR branch
   against `main`, so the DRM workstream is reviewable and mergeable
   independently of the test harness/report commits.
2. **Hardware cursor** — implement the legacy cursor ioctls so the X server's
   `modesetting` driver can use a hardware cursor instead of software-rendering
   it into the shadow framebuffer (a smoother, lower-latency desktop).
3. **virgl 3D feasibility** — assess (not implement) whether virtio-gpu 3D
   acceleration is a realistic next step for this kernel + QEMU stack.

## 1. Upstream PR rollup (kernel-side commits → `main`)

`track/drm` sits exactly 9 commits on top of `origin/main` (`4e935503d`): six
`feat` (kernel) commits interleaved with three `test` (harness/report) commits.
The kernel commits touch only `kernel/comps/virtio/...` and `kernel/src/...`:

| commit | scope |
|---|---|
| `feat(virtio): add virtio-gpu 2D device driver` | control-queue 2D ops |
| `feat(device): expose /dev/dri/card0 via a minimal DRM char device` | DRM node |
| `feat(virtio): present external framebuffers via a reusable 2D pipeline` | `present_framebuffer` |
| `feat(vm): expose contiguous VMO paddr and allow char-device VMO mmap` | `Vmo::paddr()` + mmap relaxation |
| `feat(device): implement KMS ioctls with dumb buffers and mmap` | KMS + dumb buffers |
| `feat(device): add DRM ioctls required by the Xorg modesetting driver` | `SET_MASTER`/`PAGE_FLIP`/`DIRTYFB`/… |

A branch `drm/virtio-gpu-kms` was created from `origin/main` and the six commits
cherry-picked in order (zero conflicts — they are the exact descendants of that
base). The result is **byte-identical** to `track/drm`'s kernel subset
(verified with `cmp`). PR: **https://github.com/TankTechnology/asterinas-riscv/pull/41**.
The `test(drm)` commits are deliberately kept out to keep the PR review-focused;
the harness + reports remain on `track/drm`.

## 2. Hardware cursor (cursor plane)

### virtio-gpu side (`kernel/comps/virtio/src/device/gpu/`)

The cursor queue (`VQ_CURSOR = 1`) was already created at init but dormant. The
driver now uses it for the two cursor commands (5.7.6.7):

- `VIRTIO_GPU_CMD_UPDATE_CURSOR` (0x0300) — `virtio_gpu_update_cursor`:
  `ctrl_hdr` + `cursor_pos {scanout_id, x, y}` + `resource_id` + `hot_x` +
  `hot_y`. `resource_id = 0` hides the cursor.
- `VIRTIO_GPU_CMD_MOVE_CURSOR` (0x0301) — same wire struct; only the
  `hdr`+`pos` prefix is meaningful (Linux queues the full struct for both).

`GpuDevice` gained `present_cursor` (create an **ARGB** 2D resource →
`ATTACH_BACKING` on the dumb buffer's guest memory → `UPDATE_CURSOR`),
`move_cursor`, and `hide_cursor`, plus a `cursor_resource` id tracker so a
re-present unrefs the previous cursor resource. `resource_create_2d` now takes a
`format` so the cursor uses `B8G8R8A8_UNORM` (with alpha) while the scanout
keeps `B8G8R8X8_UNORM`. Unlike the scanout path there is no
`TRANSFER_TO_HOST_2D`: the host reads the cursor pixels straight out of the
attached backing memory, so a guest mmap write is visible immediately.

### DRM side (`kernel/src/device/dri.rs`)

Two new ioctls, both backing the legacy cursor API the modesetting driver uses:

| ioctl | nr | struct |
|---|---|---|
| `DRM_IOCTL_MODE_CURSOR` | 0xa3 | `drm_mode_cursor` (28 B) |
| `DRM_IOCTL_MODE_CURSOR2` | 0xbb | `drm_mode_cursor2` (36 B, + hotspot) |

A shared `set_cursor(...)` handler interprets the flags:
`DRM_MODE_CURSOR_BO` sets the buffer (a dumb-buffer handle whose guest memory
backs a new ARGB cursor resource; `handle == 0` hides), and
`DRM_MODE_CURSOR_MOVE` repositions. Both may be set in one call.
`DRM_CAP_CURSOR_WIDTH`/`HEIGHT` now report 64×64.

### Gotcha — the cursor queue returns a zero-length used buffer

The first run hung: QEMU parsed the `UPDATE_CURSOR` correctly (its trace showed
`scanout 0, x 0, y 0, update, res 0x2`) but recycled the cursor-queue buffer
with a **0-byte** used entry, tripping `pop_used_with_min_bytes(24)` →
`invalid used length: 0 (expected 24..=24)` and a busy-loop. The control queue
answers with a real 24-byte `OK_NODATA`; the cursor queue does not. Linux's
`virtio_gpu_dequeue_cursor_func` likewise ignores the cursor response length, so
the fix is a cursor-specific submit path that waits for the buffer to come back
(`pop_used()`, min 0 bytes) and never reads a response body.

### Verification

Because the cursor overlay is not composited into QEMU's console surface, a
`screendump` cannot see it. Verification is two-pronged instead: the per-step
ioctl markers prove QEMU answered `OK_NODATA` (i.e. accepted each command), and
QEMU's `virtio_gpu_update_cursor` trace proves the command reached the device.

```
=== DRM-M4 guest result ===
[DRM] open: OK
[DRM] get_resources: OK            (crtc_id=1)
[DRM] create_dumb: OK              (64x64, pitch=256, size=16384)
[DRM] map_dumb: OK
[DRM] mmap: OK
[DRM] draw_cursor: OK
[DRM] set_cursor2: OK   __DRM_CURSOR_SET_OK__
[DRM] move_cursor: OK   __DRM_CURSOR_MOVE_OK__
[DRM] hide_cursor: OK   __DRM_CURSOR_HIDE_OK__
  set_cursor2: OK
  move_cursor: OK
  hide_cursor: OK
  device trace (virtio_gpu_update_cursor): 4 event(s)
=== DRM-M4: PASS (smp=1) ===
```

Four trace events = one `UPDATE_CURSOR` + one `MOVE_CURSOR` for the `BO|MOVE`
set call, one for the standalone move, and one for the hide (`resource_id=0`).

The X server's `modesetting` driver calls exactly these ioctls
(`drmModeSetCursor2` → `drmModeMoveCursor` → `drmModeSetCursor(handle=0)`), so
this makes the hardware cursor available to the existing M3 desktop. (A visual
confirmation requires a real QEMU display backend — gtk/sdl/spice — that
renders the cursor overlay; the trace + ioctl acceptance is the strongest
signal available under `-display none`.)

## 3. virgl 3D acceleration — feasibility assessment

**Conclusion: technically feasible, but a large and largely orthogonal effort —
not blocked by anything fundamental, and not recommended until the 2D desktop is
stable. Defer.**

virgl accelerates guest OpenGL by translating it into a Gallium command stream
that `virglrenderer` on the host turns back into host GL calls. The RISC-V guest
side is not a blocker — virgl is endianness-sensitive only in that it needs a
little-endian guest, which RISC-V is. What it needs instead is two substantial,
independent pieces the current stack does not have:

**Kernel** (a second ioctl surface + a 3D command path):

1. Negotiate `VIRTIO_GPU_F_VIRGL` (feature bit 0) — today
   `GpuDevice::negotiate_features` clears *every* device feature.
2. Capset query — `GET_CAPSET_INFO`/`GET_CAPSET` for the `VIRGL`/`VIRGL2`
   capsets (command codes exist in `mod.rs`, no plumbing yet).
3. A 3D command path — `RESOURCE_CREATE_3D`, `CTX_CREATE/DESTROY`,
   `CTX_ATTACH/DETACH_RESOURCE`, and `SUBMIT_3D` over a shared guest↔host
   command buffer, plus virtio-gpu **fencing** (`fence_id` in the ctrl hdr) so
   a submit's completion is observable to userspace.
4. The virtio-gpu-specific DRM ioctl surface the userspace driver speaks —
   `DRM_IOCTL_VIRTGPU_EXECBUFFER`, `RESOURCE_CREATE`, `CONTEXT_INIT`,
   `GET_CAPS`, `GETPARAM`, `WAIT`, … (major 226, `_IOWR('d', 0x01..)`) — which
   is entirely separate from the KMS/modesetting ioctls implemented in M1–M4.

**Userspace**: a cross-compiled **Mesa** virgl gallium driver for RISC-V
(`libgbm`/`libEGL`/`virgl_dri.so`), which pulls in the full Mesa + LLVM cross
toolchain — an order of magnitude larger than the libdrm/Xorg cross-builds done
for M2/M3. The guest Mesa version must match the host `virglrenderer` capset
(OpenGL pass-through needs guest Mesa ≥ 16.0; the modern Venus/Vulkan and
DRM-native-context capsets need blob + hostmem and are even heavier).

**Host**: QEMU built with `virglrenderer` and run as
`-device virtio-gpu-gl-pci` (or `virtio-gpu,virgl=on`) on an EGL-capable host —
the current harness runs `-device virtio-gpu-device` with no GL.

**Effort shape.** The kernel piece is roughly "port the core of Linux's virtgpu
driver" (3D command path + a second DRM ioctl surface + fencing), and the
userspace piece is "stand up a Mesa/LLVM RISC-V cross build". Neither is small,
and neither overlaps much with what M1–M4 already built. A sensible de-risking
order would be: EDID (a trivial capset) → `RESOURCE_BLOB`/host memory → then
virgl. The 2D modesetting + shadowfb + hardware-cursor desktop is the right
base to stabilize first.

## Files

New/changed this milestone:

- `kernel/comps/virtio/src/device/gpu/mod.rs` — `VirtioGpuCursorPos` /
  `VirtioGpuUpdateCursor` wire structs.
- `kernel/comps/virtio/src/device/gpu/device.rs` — cursor queue usage,
  `present_cursor`/`move_cursor`/`hide_cursor`, cursor submit path, format
  parameter on `resource_create_2d`.
- `kernel/src/device/dri.rs` — `MODE_CURSOR`/`MODE_CURSOR2` ioctls + `set_cursor`,
  `DRM_CAP_CURSOR_WIDTH/HEIGHT`, dropped the stale `NoData` import.
- `tools/riscv/nixos/drm/init_m4.c` — static cursor smoke test (hardcoded
  ioctls; the cross sysroot can't compute DRM ioctl numbers).
- `tools/riscv/nixos/drm/build_drm_m4.sh` — builds the initramfs and re-packs
  the independent `/tmp/drm-m4` boot disk.
- `tools/riscv/nixos/drm/boot_drm_m4.py` — boots with `-trace
  virtio_gpu_update_cursor` and checks the per-step markers + trace count.

## Next steps

- Merge PR #41, then open a follow-up PR for the M4 cursor commit.
- Optional: visually confirm the Xorg hardware cursor under a real QEMU display
  (gtk/sdl/spice) — the ioctl path is proven; only the overlay render is not.
- If/when 3D is wanted, start with EDID + `RESOURCE_BLOB` before virgl.
