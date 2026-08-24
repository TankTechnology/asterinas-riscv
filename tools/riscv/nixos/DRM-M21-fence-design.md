# DRM-M21 — virtio-gpu fence signaling: implementation plan

Status: **implemented + verified** (2026-08-24). Branch `track/drm`.

> **Result:** eglrender2 now runs to completion — `M19_FRAME 0..3` with distinct checksums,
> `M19_FRAME_SAVED` (1280×800 PPM), `M19_EGL_DONE`, `MINI_EGL_RC=0`. ioctltrace shows
> `POLL [fd=7/8]` (valid fence fds, never `-1`). One simplification vs. the design below:
> the device **defers** a fenced command's response until the render completes, so the
> existing synchronous `submit_control` busy-wait already blocks until done — no separate
> `drain_fence_signals()` was needed. `virtgpu_wait` remains a no-op (Mesa's virgl uses
> `fence_fd`, not `WAIT`).

## 1. Problem

virgl now activates (`M19_GL_RENDERER virgl`, `OpenGL ES 3.0`), but the render→scanout
path hangs. `eglrender2` gets past EGL init, KMS discovery, and `MODE_CREATEPROPBLOB`,
then blocks in a `ppoll` inside `glFinish()` / `eglSwapBuffers()` waiting for a fence
that never signals. The kernel has two stubs that are the direct cause:

- `virtgpu_execbuffer` always writes back `fence_fd = -1` (`// no fence support for now`).
- `virtgpu_wait` is a no-op (`// Always succeed — no async GPU operations to wait for`).

Mesa's virgl winsys synchronizes against **either** a pollable `fence_fd` returned by
`EXECBUFFER`, **or** the blocking `VIRTGPU_WAIT` ioctl. Both are absent, so the swap
never completes. The remaining work is to implement virtio-gpu fences.

**Confirmed by the debug boot (2026-08-24).** With fstat-based fd detection + poll
logging added to `ioctltrace`, the stall is pinned down exactly. After KMS setup the
render path is:

```
IOCTL[5] VIRTGPU_RESOURCE_CREATE -> 0
IOCTL[5] VIRTGPU_MAP           -> 0
IOCTL[5] VIRTGPU_TRANSFER_TO_HOST -> 0
IOCTL[5] VIRTGPU_EXECBUFFER    -> 0
POLL nfds=1 timeout=-1 [fd=-1 ev=0x1]     ← the hang
```

Two facts fall out of this:

1. **Mesa uses `fence_fd`, not `WAIT`.** There is no `VIRTGPU_WAIT` and no `drm_syncobj`
   ioctl in the trace. The virgl winsys reads the `fence_fd` we write back and polls it
   unconditionally.
2. **The hang is `poll(fd=-1, POLLIN, -1)`.** `fence_fd` came back as `-1` (our stub), and
   `poll()` with a `-1` fd and infinite timeout blocks forever. So the fix is simply to
   return a **valid, pollable `fence_fd`** from `EXECBUFFER` — Phase 2 is the critical
   path, not Phase 1.

## 2. What already exists (verified against `track/drm`)

The fence work sits on top of machinery that is already in the tree:

**Transport (`kernel/comps/virtio/src/device/gpu/device.rs`, `mod.rs`)**
- `submit_3d(size, data)` submits `VIRTIO_GPU_CMD_SUBMIT_3D` and busy-waits
  (`submit_control` → `loop { pop_used(); spin_loop() }`) for the response. **All**
  control-queue commands are synchronous busy-wait today; there is no IRQ-driven
  response handler.
- `VirtioGpuCtrlHdr { type_: u32, flags: u32, fence_id: u64, ctx_id: u32, padding: u32 }`
  already carries `flags` + `fence_id`, but `ctrl_hdr()` zeroes both.
- `VIRTIO_GPU_FLAG_FENCE` is **not defined yet** (Linux value is `1 << 0`).
- `VirtioGpuCmdSubmit { hdr, size, padding }` is the SUBMIT_3D wire struct.

**DRM ioctl layer (`kernel/src/device/drm/virtio_gpu.rs`, `mod.rs`)**
- `virtgpu_execbuffer` (`mod.rs:829`) → reads the cmd buffer, validates GEM handles,
  calls `gpu.submit_3d()`, writes `fence_fd = -1`.
- `virtgpu_wait` (`mod.rs:856`) → no-op over `DrmVirtgpu3dWait { handle, flags }`.
- `GpuManager` (`mod.rs:153`) holds `gpu`, `gem_objects`, `gem_resources`
  (`object_id → 3D resource id`), the dumb pool, and `flip_sequence`. A fence table and a
  per-resource last-fence map slot in here.

**Pollable-file infrastructure (the key enabler)**
- `Pollee` / `PollHandle` / `Pollable` / `IoEvents` (`kernel/src/process/signal`) and
  `ostd::sync::WaitQueue` + `wait_events()` — a complete poll primitive.
- `EventFile` (`kernel/src/syscall/eventfd.rs`) is a ready-made model for a pollable
  anonymous inode (counter + `Pollee` + `WaitQueue` + `AnonInodeFs::new_path`).
- `DriHandle` already uses this for page-flip events: `queue_flip_event` →
  `pollee.notify(IoEvents::IN)`, `read_events`, and a `Pollable` impl
  (`mod.rs:399`–`459`).
- **Returning an fd from an ioctl** is already done for PRIME: `PrimeHandleToFd` lives in
  `ioctl_with_table` and does `file_table.write().insert(file, fd_flags)` then writes the
  fd back into the reply (`mod.rs:878`–`893`). `prime::DmaBufFile` is the anon-inode
  `FileLike` to copy. `VirtgpuExecbuffer` currently lives in `ioctl` (no table) and must
  **move to `ioctl_with_table`** to hand back a `fence_fd`.

## 3. The virtio-gpu fence protocol

When the driver wants a command to be fenced it sets `VIRTIO_GPU_FLAG_FENCE` (bit 0) in
`VirtioGpuCtrlHdr.flags` and a driver-chosen `fence_id` in `hdr.fence_id`. The device
then produces **two** control-queue responses:

1. The normal completion ack (`VIRTIO_GPU_RESP_OK_NODATA`), with the `FENCE` bit **clear**
   and the `fence_id` echoed — this is the "command received" response the current
   synchronous path already waits for.
2. A **second** `VIRTIO_GPU_RESP_OK_NODATA` with `FENCE` **set** and the same `fence_id`,
   delivered later, when the command's effects complete (the host GL has finished the
   render). This is the fence signal.

Linux mirrors this in `virtio_gpu_queue_ctrl_buffer` + `virtio_gpu_ctrl_irq` →
`virtio_gpu_fence_event_process` (`drivers/gpu/drm/virtio/virtgpu_vq.c`): the request sets
the flag, the ack clears it, and the fence-signal response has the flag set.

> **Verify empirically first (Phase 0).** The exact flag/echo behaviour against the
> running `virglrenderer` (QEMU `-device virtio-gpu-gl-pci`) is the one thing not yet
> confirmed on this host. A raw `virgltest` probe (below) pins it down before any kernel
> change is committed.

## 4. Design

### 4.1 Fence object

A `Fence` is the kernel-side handle for one in-flight GPU command:

```
Fence { fence_id: u64, signaled: AtomicBool, pollee: Pollee, wait_queue: WaitQueue }
```

`signal()` sets `signaled` and calls `pollee.notify(IoEvents::IN)` + `wait_queue.wake_all()`.

### 4.2 Fence table + per-resource last fence (`GpuManager`)

Add to `GpuManager`:

- `fences: SpinLock<BTreeMap<u64, Arc<Fence>>>` — by `fence_id`.
- `next_fence_id: AtomicU64` — monotonic allocator.
- `resource_fence: SpinLock<BTreeMap<u32, Arc<Fence>>>` — last fence per 3D resource id,
  so `WAIT` can wait for a specific resource.

`virtgpu_resource_create` already records `object_id → res_handle` in `gem_resources`; the
`WAIT` lookup goes `handle → object_id → res_handle → resource_fence`.

### 4.3 Fenced submit (`GpuDevice`)

Add `submit_3d_fenced(size, data, fence_id)` that mirrors `submit_3d` but sets
`hdr.flags = VIRTIO_GPU_FLAG_FENCE` and `hdr.fence_id = fence_id` before
`submit_control`. The existing `submit_3d` becomes a thin wrapper with `fence_id = 0`.

### 4.4 Fence-signal draining (`GpuDevice`)

Add `drain_fence_signals()`: pop used control-queue buffers (same `control_queue` lock as
`submit_control`); for each `VIRTIO_GPU_RESP_OK_NODATA` response with `FENCE` set, look up
`fence_id` in the fence table and `signal()` it. Non-fence responses are ignored (they are
already consumed by the synchronous `submit_control` path).

`drain_fence_signals()` is reachable from every point that can block on a fence:
`virtgpu_wait`, and `FenceFile`'s blocking read/wait. Because Asterinas is SMP and the
control queue is shared, all of these must go through the one `control_queue` lock —
which is already the model for the busy-wait submit path.

### 4.5 `FenceFile` (the `fence_fd`)

A new `FileLike` (new file `kernel/src/device/drm/fence.rs`), modelled on `EventFile` /
`DmaBufFile`:

- Anon inode path `anon_inode:[sync_file]` via `AnonInodeFs::new_path`.
- `Pollable::poll` reports `IoEvents::IN` once `signaled`.
- `read()` (blocking) waits on the fence's `wait_queue` / `pollee` until signaled, then
  returns `0` (or a `dma_fence` status word if Mesa expects one — confirm in Phase 0).
- `FileOps`/`FileLike` boilerplate copied from `eventfd.rs`.

`virtgpu_execbuffer`, when the caller requests an out-fence (`fence_fd` slot present),
allocates a `Fence`, fenced-submits the command, and returns a `FenceFile` fd via
`file_table.insert(...)`, exactly like `PrimeHandleToFd`.

### 4.6 `WAIT` (`virtgpu_wait`)

Resolve `handle → object_id → res_handle`, find `resource_fence[res_handle]`, and block
(busy-drain the control queue via `drain_fence_signals`, or wait on the fence's
`wait_queue`) until `signaled` or a timeout. `flags` in `DrmVirtgpu3dWait` is ignored for
now (we have no `dma_fence` timeout semantics yet).

## 5. Implementation plan (phased)

**Phase 0 — empirical probe (no kernel change).** Add a raw `virgltest` step that submits
a `SUBMIT_3D` with `FLAG_FENCE` + a `fence_id`, then reads the control queue and logs the
two responses (ack + fence-signal). Confirms: (a) virglrenderer sends the second response,
(b) its exact `flags`/`fence_id` shape. This is the only unknown in the protocol.

**Phase 1 — fence object + fenced submit + blocking `WAIT`.**
`VIRTIO_GPU_FLAG_FENCE` const; `Fence`; `GpuManager` fence table + `next_fence_id` +
`resource_fence`; `submit_3d_fenced`; `drain_fence_signals`; real `virtgpu_wait`. This
unblocks the synchronous `WAIT`-based sync path.

**Phase 2 — `fence_fd` (`FenceFile`).** Move `VirtgpuExecbuffer` to `ioctl_with_table`;
allocate a `Fence` + return a `FenceFile` fd when an out-fence is requested; keep
`fence_fd = -1` otherwise. This unblocks Mesa's poll-based wait (the `ppoll` we currently
hang on).

> **Simplest correct first cut.** Given the all-synchronous transport, the minimal
> correct implementation is: `virtgpu_execbuffer` fenced-submits (`FLAG_FENCE` +
> `fence_id`), then **busy-drains** the control queue until this fence's signal response
> arrives (so the render is provably done), then returns a **pre-signalled** `FenceFile`
> fd. Mesa's `poll(fd)` returns `POLLIN` immediately and the following
> `TRANSFER_FROM_HOST` reads correct pixels. This blocks the ioctl for the render
> duration — acceptable under TCG — and can be made async later (Phase 3).

**Phase 3 — (optional) IRQ-driven control queue.** Replace the busy-wait `submit_control`
with an IRQ handler that also processes fence-signal responses asynchronously. Removes the
spin-wait and is the "correct" long-term shape, but is not required for correctness given
the current all-synchronous transport.

## 6. Files to touch

- `kernel/comps/virtio/src/device/gpu/mod.rs` — add `VIRTIO_GPU_FLAG_FENCE`.
- `kernel/comps/virtio/src/device/gpu/device.rs` — `submit_3d_fenced`, `drain_fence_signals`,
  `next_fence_id` allocator.
- `kernel/src/device/drm/mod.rs` — `GpuManager` fence table + `resource_fence`;
  move `VirtgpuExecbuffer` to `ioctl_with_table`; register `FenceFile`.
- `kernel/src/device/drm/virtio_gpu.rs` — real `virtgpu_execbuffer` (fence + fd) and
  `virtgpu_wait` (blocking).
- `kernel/src/device/drm/fence.rs` — **new**: `Fence`, `FenceFile` (`FileLike` +
  `Pollable`), copied from `eventfd.rs` / `prime.rs`.
- `tools/riscv/nixos/m16/virgltest.c` — Phase 0 fence-signal probe.

## 7. Verification

Re-run `boot_mini_debug.py` (mini virgl rootfs + `-device virtio-gpu-gl-pci`). Success is
`eglrender2` progressing past the stall:

- `M19_FRAME <n> csum=…` for all `NUM_FRAMES`, then `M19_FRAMES_DISTINCT > 0` and
  `M19_EGL_DONE`.
- `ioctltrace` now showing `VIRTGPU_EXECBUFFER` → `VIRTGPU_WAIT` (or a `POLL` on the
  fence fd) instead of a hung `PPOLL`.

Then `kmscube -D /dev/dri/card0` renders through the GPU (virgl) path.

## 8. Risks / open questions

1. **Exact fence-signal response shape** (Phase 0) — the flag/echo details are the one
   unverified assumption.
2. **Mesa's actual sync path** — **resolved**: virgl waits via `fence_fd` (poll), not
   `WAIT`, and the trace shows no `drm_syncobj` ioctls. So Phase 2 (`FenceFile`) is the
   critical path; Phase 1 (`WAIT`) is only needed if a later consumer calls it. If a
   future Mesa version starts using syncobj timeline semaphores, a minimal
   `DRM_IOCTL_SYNCOBJ_*` shim is a possible follow-on.
3. **Control-queue concurrency** — `drain_fence_signals` shares the queue lock with the
   busy-wait `submit_control`; correctness under SMP + out-of-order fence responses needs
   the fence table to be matched by `fence_id`, never by queue position.
4. **Fence lifetime** — a fence referenced by both `fences` (draining) and `FenceFile`
   (fd) must be freed exactly once; use `Arc` and drop it from `fences` on signal.
