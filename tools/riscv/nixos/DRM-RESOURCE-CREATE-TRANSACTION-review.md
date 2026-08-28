---
date: 2026-08-28
mode: files
files: kernel/src/device/drm/dumb.rs,kernel/src/device/drm/gem.rs,kernel/src/device/drm/virtio_gpu.rs,kernel/src/device/drm/virtio_gpu/resource_create.rs,tools/riscv/nixos/m22/resource_stress.c
head: b3822fd7a-dirty
branch: codex/drm-main-sync
title: "Transactional virtio-gpu resource creation"
---

# Summary

The review found three major boundary/lifetime defects, two major wait/fence
defects, and a set of maintainability and documentation issues. All confirmed
findings were addressed before publication: resource creation is now a typed
RAII transaction, known linear formats require sufficient backing, unproven
direct-transfer layouts are rejected, capset versions are checked, and
resource waits are bounded and interruptible while consuming completed or
failed fence associations. The smaller naming, unit, helper, documentation,
and test-maintainability findings were fixed in the same patch.

Verification completed with targeted RISC-V ktests, the M22 resource-lifetime
gate (`55` passes, `0` failures, `32/32` rounds), and the complete Mesa/virgl
gate. Legacy and atomic KMS, PRIME, syncobj, raw virgl, explicit synchronization,
GPU readback, and four distinct EGL frames all passed, ending in
`MINI_VIRGL_PASS`.

## Maintainability

### `kernel/src/device/drm/dumb.rs` line 24

> ```diff
> pub(super) struct DumbPool {
>     capacity: usize,
>     state: Mutex<DumbPoolState>,
> }
> ```

`encode-units` (minor): The pool stores byte counts in plain `usize` values named `capacity` and `size`, and `allocate` propagates the same ambiguity through `size` and `allocated_size`. This is inconsistent with the explicitly unit-qualified `used_bytes` and `high_water_bytes` fields.

**Fix.** Rename these internal values to `capacity_bytes`, `size_bytes`, `requested_size_bytes`, and `allocated_size_bytes`, including the `PoolAllocation` accessor.

### `kernel/src/device/drm/dumb.rs` line 30

> ```diff
> pub(super) struct DumbPoolUsage {
>     pub(super) used_bytes: usize,
>     pub(super) high_water_bytes: usize,
> }
> ```

`getter-encapsulation` (nit): `DumbPoolUsage` exposes both bookkeeping fields to its parent module even though consumers only need to read them. This also lets that module construct states where `high_water_bytes` is less than `used_bytes`.

**Fix.** Keep `used_bytes` and `high_water_bytes` private and expose read-only getters such as `used_bytes()` and `high_water_bytes()`.

### `kernel/src/device/drm/gem.rs` line 155

> ```diff
> /// GEM_CLOSE: drop a per-file handle, decrementing the object's ref count.
> pub(super) fn gem_close(handle: &super::DriHandle, gem_handle: u32) -> Result<()> {
> ```

`rfc1574-summary` (nit): The `gem_close` documentation begins with the imperative label ``GEM_CLOSE: drop`` rather than a third-person verb describing what the function does.

**Fix.** Change the summary to something like ``Closes a `GEM_CLOSE` handle and decrements the object's reference count.``

### `kernel/src/device/drm/gem.rs` line 175

> ```diff
> /// GEM_FLINK: return the object's id as a global 32-bit name.
> pub(super) fn gem_flink(handle: &super::DriHandle, gem_handle: u32) -> Result<u32> {
> ```

`rfc1574-summary` (nit): The `gem_flink` documentation begins with ``GEM_FLINK: return`` rather than a third-person verb.

**Fix.** Change the summary to something like ``Returns the object's global `GEM_FLINK` name.``

### `kernel/src/device/drm/gem.rs` line 202

> ```diff
> /// GEM_OPEN: look up a global name and create a per-file handle.
> pub(super) fn gem_open<'a>(
> ```

`rfc1574-summary` (nit): The `gem_open` documentation begins with ``GEM_OPEN: look up`` rather than a third-person verb.

**Fix.** Change the summary to something like ``Opens a global `GEM_OPEN` name and reserves a per-file handle.``

### `kernel/src/device/drm/virtio_gpu.rs` line 12

> ```diff
> use aster_virtio::device::gpu::{VIRTIO_GPU_CAPSET_VIRGL, VIRTIO_GPU_CAPSET_VIRGL2};
> ```

`qualified-fn-imports` (nit): The free constants `VIRTIO_GPU_CAPSET_VIRGL` and `VIRTIO_GPU_CAPSET_VIRGL2` are imported directly, hiding their module origin at each comparison.

**Fix.** Import `aster_virtio::device::gpu` and refer to the constants as `gpu::VIRTIO_GPU_CAPSET_VIRGL` and `gpu::VIRTIO_GPU_CAPSET_VIRGL2`.

### `kernel/src/device/drm/virtio_gpu.rs` line 190

> ```diff
> let object_id = {
>     let inner = handle.inner.lock();
>     *inner
>         .handles
>         .get(&req.bo_handle)
>         .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown GEM handle"))?
> };
> ```

`dry` (minor): The same per-file handle lookup, lock, copy, and `EINVAL` construction is repeated in `virtgpu_resource_info`, both transfer functions, `virtgpu_map`, `virtgpu_wait`, and `PreparedBacking::retain`. Any future change to handle-resolution semantics must therefore be repeated consistently in at least six places.

**Fix.** Add a narrowly visible helper such as `DriHandle::object_id_for_handle` returning `Result<u32>`, and use it at each of these call sites.

### `kernel/src/device/drm/virtio_gpu/resource_create.rs` line 55

> ```diff
> let decoded = DecodedResourceCreate::read(cmd.read()?)?;
> ```

`accurate-names` (minor): `DecodedResourceCreate::read` neither reads input nor merely decodes it: it accepts an already-read `DrmVirtgpuResourceCreate` and validates it. The nested `DecodedResourceCreate::read(cmd.read()?)` call obscures which operation performs I/O and which performs validation.

**Fix.** Rename the type and constructor to reflect the validated state, for example `ValidatedResourceCreate::validate(cmd.read()?)`.

### `kernel/src/device/drm/virtio_gpu/resource_create.rs` line 105

> ```diff
> device_addr: u64,
> size: u32,
> owner: Arc<PoolAllocation>,
> ```

`encode-units` (minor): `PreparedBacking::size` is a plain `u32` byte count, but its name does not convey that unit even though it is passed as the byte length to `attach_backing` and copied into the wire `size` field.

**Fix.** Rename the internal field and related locals to `size_bytes` or `backing_size_bytes`; keep the ABI-mandated wire field named `size` only at the copy boundary.

### `kernel/src/device/drm/virtio_gpu/resource_create.rs` line 133

> ```diff
> const BPP: u32 = 32;
> let pitch = if request.stride == 0 {
>     request
>         .width
>         .checked_mul(BPP / 8)
> ```

`descriptive-names` (nit): The local constant `BPP` is ambiguous between bits per pixel and bytes per pixel until the reader reaches `BPP / 8`.

**Fix.** Rename it to `BITS_PER_PIXEL`, and optionally bind the derived `BYTES_PER_PIXEL` value used for the pitch calculation.

### `kernel/src/device/drm/virtio_gpu/resource_create.rs` line 265

> ```diff
> fn create_host_resource(&mut self) -> Result<()> {
>     self.handle
>         .gpu_manager
>         .gpu
>         .resource_create_3d(self.create)
>         // ...
>     self.handle
>         .gpu_manager
>         .gpu
>         .attach_backing(
>         // ...
>     self.handle
>         .attach_resource_to_context(self.create.resource_id)?;
> ```

`single-responsibility` (minor): `create_host_resource` performs three separately fallible transaction stages: `resource_create_3d`, `attach_backing`, and `attach_resource_to_context`. Its name hides the latter two operations, and the interleaved state transitions make rollback reasoning depend on reading the entire method.

**Fix.** Split the method into named stages such as `create_resource`, `attach_backing`, and `attach_to_context`, keeping each `ResourceCreateState` transition adjacent to the stage it records.

### `kernel/src/device/drm/virtio_gpu/resource_create.rs` line 322

> ```diff
> self.state = ResourceCreateState::Published;
> let pending_buffer = self.backing.pending_buffer.take();
> drop(self);
> if let Some(pending_buffer) = pending_buffer {
>     pending_buffer.publish();
> }
> ```

`design-decisions` (minor): The explicit `drop(self)` imposes a subtle publication order: the GEM-resource entry becomes live, `PendingDumbBuffer` is extracted, the transaction guard and reference are dropped, and only then is the handle published. The reason this ordering is required is not documented.

**Fix.** Add a design-decision comment above `drop(self)` explaining why `_resource_creation` and `transaction_object` must be released before `PendingDumbBuffer::publish`, or encode that phase boundary in a helper whose name states the ordering.

### `tools/riscv/nixos/m22/resource_stress.c` line 265

> ```diff
> *dumb = (struct drm_mode_create_dumb) {
>     .width = 64,
>     .height = 64,
>     .bpp = 32,
> };
> ```

`dry` (minor): The test's `64` × `64` geometry and `32`-bit pixel format are repeated across `create_dumb`, the boundary tests, the rollback test, and `run_round`; boundary literals such as `x = 63` separately encode the same geometry. Changing the test resource now requires synchronized edits in many places.

**Fix.** Define constants such as `TEST_WIDTH`, `TEST_HEIGHT`, and `TEST_BITS_PER_PIXEL`, use them in every request, and derive boundary inputs such as `TEST_WIDTH - 1`.

### `tools/riscv/nixos/m22/resource_stress.c` line 683

> ```diff
> if (poll(&poll_fd, 1, 5000) <= 0 || !(poll_fd.revents & POLLIN) ||
> ```

`encode-units` (nit): The literal `5000` passed to `poll` is a timeout in milliseconds, but neither its name nor its type communicates that unit.

**Fix.** Introduce a semantic constant such as `FENCE_WAIT_TIMEOUT_MS` and pass it to `poll`.

## Correctness

### `kernel/src/device/drm/virtio_gpu.rs` line 465

> ```diff
> for fence in &fences {
>     if let Err(error) = fence.wait()
>         && wait_result.is_ok()
>     {
>         wait_result = Err(error);
>     }
> }
> ```

Uninterruptible wait (major): `virtgpu_wait()` calls the deadline-free, uninterruptible `Fence::wait()` for every tracked fence. If the virtio device stops completing a submitted command, blocking `VIRTGPU_WAIT` sleeps forever and cannot return on a signal or device timeout.

**Fix.** Add a cancellable, bounded fence-wait API and use it here, translating cancellation and expiry to the appropriate `EINTR` or timeout error. Add a regression test using a fence that never completes.

### `kernel/src/device/drm/virtio_gpu.rs` line 471

> ```diff
>         wait_result?;
>     }
>     handle.gpu_manager.clear_resource_fences(object_id, &fences);
> ```

Failed fence remains tracked (major): When `fence.wait()` returns `EIO`, `wait_result?` exits before `clear_resource_fences()`. Because a failed fence continues returning `EIO`, repeated `VIRTGPU_WAIT` calls cannot consume it and its `resource_fences` entry and `fence_associations` count remain until another submission or object teardown. The `try_finish()?` path has the same early-exit problem.

**Fix.** Track every signaled fence and clear its association before propagating `EIO` or `EBUSY`. In the blocking branch, clear the snapshot before `wait_result?`; in the nowait branch, clear all successfully or unsuccessfully completed fences before any early return.

### `tools/riscv/nixos/m22/resource_stress.c` line 515

> ```diff
> // Create the per-file virgl context before the snapshot. The failed ioctl
> // below must then return every resource, attachment, GEM, and pool counter
> // exactly to this warmed state.
> ```

`add-regression-tests` (minor): `run_resource_copyout_rollback_test()` is a regression test for transactional resource-creation rollback, but its explanatory comment contains no issue reference, so future readers cannot recover the original failure context.

**Fix.** Add the originating issue number or URL to the comment above the test scenario, for example `// Regression test for #NNNN: resource-create copyout failure leaked ...`.

## Security

### `kernel/src/device/drm/virtio_gpu.rs` line 258

> ```diff
> let capset_info = handle.gpu_manager.gpu.get_capset_info(cap_set_id)?;
> ...
> .get_capset(cap_set_id, req.cap_set_ver)
> ```

`validate-at-boundaries` (minor): `virtgpu_get_caps` obtains `capset_info` but never checks `req.cap_set_ver <= capset_info.capset_max_version`, so a hostile caller can send an unsupported version such as `u32::MAX` to the device instead of receiving `EINVAL` at the ioctl boundary. The [Linux reference implementation](https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/virtio/virtgpu_ioctl.c) performs this check before issuing `GET_CAPSET`.

**Fix.** Reject `req.cap_set_ver > capset_info.capset_max_version` with `EINVAL` before calling `get_capset`.

### `kernel/src/device/drm/virtio_gpu.rs` line 314

> ```diff
> resource.validate_transfer(req.box_.into_transfer_3d(req.level, req.offset))?;
> ...
> .transfer_to_host_3d(
>     ...
>     req.offset as u64,
>     ...
> )
> ```

`validate-at-boundaries` (major): `validate_transfer` proves the backing byte range only when `linear_bytes_per_pixel` recognizes the format, while `validate_create` accepts many other formats. For an unrecognized format, a caller can use a backing of `1` byte with a nonempty box and `offset = 0`; both `TRANSFER_TO_HOST` and `TRANSFER_FROM_HOST` then reach the device without proving that the DMA range remains inside the GEM allocation, risking disclosure or corruption of adjacent memory.

**Fix.** Extend transfer validation to calculate the byte footprint for every accepted format, including block-compressed formats and mip/layer layout. Until a format has a sound footprint calculation, reject transfers for it with `EINVAL`.

### `kernel/src/device/drm/virtio_gpu/resource_create.rs` line 142

> ```diff
> let size = if request.size == 0 {
>     (pitch as usize).checked_mul(request.height as usize)?
> } else {
>     request.size as usize
> };
> ```

`validate-at-boundaries` (major): `PreparedBacking::allocate` accepts the untrusted `request.size` and `request.stride` without relating them to the resource geometry. For example, `width = 65535`, `height = 65535`, and `size = 1` allocates only one pool page while asking the host to create a multi-gigabyte resource. Repeating this through the unprivileged render node bypasses the pool's intended resource accounting and can exhaust host GPU memory.

**Fix.** Calculate a checked maximum resource footprint from `format`, dimensions, mip levels, samples, and array size before creating host state. Require `request.size` or an existing GEM buffer to cover that footprint, and reject dimensions or footprints exceeding the driver's resource limit.

## Documentation

### `kernel/src/device/drm/dumb.rs` line 6

> ```diff
> //! Each allocation is wrapped in a [`super::GemObject`] and assigned a
> //! per-file handle.
> ...
> /// Creates a dumb buffer, allocating from the global pool and wrapping it
> /// in a GEM object.
> ```

`semantic-line-breaks` (nit): The documentation hard-wraps sentences at arbitrary word boundaries, splitting `a per-file handle` and `wrapping it in a GEM object` across lines instead of keeping each line semantically coherent.

**Fix.** Keep each short sentence on one line, or break the compound sentence before `and assigned`.

### `kernel/src/device/drm/virtio_gpu.rs` line 26

> ```diff
> /// Note: `value` is a userspace **pointer** to a `u64` that the kernel writes
> /// through, not an inline value field.
> ...
> /// Mesa's virgl driver uses this to discover the capset version and
> /// feature bits supported by the host (virglrenderer).
> ```

`semantic-line-breaks` (nit): The doc comments split the semantic units `writes through` and `capset version and feature bits` across lines, making the line breaks reflect column wrapping rather than sentence or clause structure.

**Fix.** Reflow these comments at clause boundaries, keeping the short predicates and coordinated noun phrases together.

### `kernel/src/device/drm/virtio_gpu/resource_create.rs` line 5

> ```diff
> //! The ioctl is split into validation, reversible GEM preparation, host-side
> //! creation, userspace copyout, and publication. Any return before publication
> //! rolls back context membership and the host resource through [`Drop`].
> //!
> //! The wire layout follows Linux's `virtgpu_drm.h`. Linux treats `bo_handle` as
> //! output-only; Asterinas also accepts an existing GEM handle so a dumb/KMS
> //! buffer can become virgl backing without a second allocation.
> ```

`semantic-line-breaks` (nit): The module documentation is hard-wrapped inside semantic units such as `host-side creation` and `as output-only`, rather than breaking at the sentence, list-item, or semicolon boundaries.

**Fix.** Reflow the introductory paragraphs so each sentence or list clause occupies a coherent line.

### `kernel/src/device/drm/virtio_gpu/resource_create.rs` line 10

> ```diff
> //! The wire layout follows Linux's `virtgpu_drm.h`. Linux treats `bo_handle` as
> //! output-only; Asterinas also accepts an existing GEM handle so a dumb/KMS
> //! buffer can become virgl backing without a second allocation.
> ```

`linux-compat-docs` (minor): `DRM_IOCTL_VIRTGPU_RESOURCE_CREATE` accepts a nonzero input `bo_handle` to reuse an existing GEM buffer, despite Linux treating that field as output-only, but `book/src/kernel/linux-compatibility/syscall-flag-coverage/file-descriptor-and-io-control/README.md` and `ioctl.scml` only list the ioctl and do not record this supported argument or compatibility deviation.

**Fix.** Document the `bo_handle == 0` allocation path and the nonzero existing-handle extension on the `ioctl` compatibility page, and make the corresponding `DRM_IOCTL_VIRTGPU_RESOURCE_CREATE` argument coverage explicit in `ioctl.scml`.

## Retracted by verification

The `add-regression-tests` comment requesting an originating issue number is
retracted. This defect was discovered and fixed inside the present transaction
review, so no external issue exists to reference. The test comment names the
failed copyout phase and rollback invariant, while this review preserves the
full failure context; inventing an issue identifier would reduce traceability.

## Verification

Every confirmed comment above is resolved in the reviewed worktree. The
copyout rollback gate deliberately makes the ioctl response page read-only
after warming the per-file context; `EFAULT` is returned and every reclaimable
host-resource, context, GEM, fence, and pool counter returns to its warmed
baseline. The M16 raw gate was also corrected to advertise only the level-zero
texture backing it actually allocates.

The broad unfiltered ktest run is not claimed as green: it exposed three
pre-existing vblank-queue failures and later stalled in a wait-for-submit test.
The directly affected tests passed individually, including backing-size and
unproven-layout validation, bounded pending-fence waiting, and
`syncobj_regression`.
