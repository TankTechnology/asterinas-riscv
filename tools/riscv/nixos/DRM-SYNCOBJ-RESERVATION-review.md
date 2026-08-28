---
date: 2026-08-28
mode: files
files: kernel/comps/virtio/src/device/gpu/control_queue.rs,kernel/comps/virtio/src/device/gpu/device.rs,kernel/src/device/drm/fence.rs,kernel/src/device/drm/gem.rs,kernel/src/device/drm/syncobj.rs,kernel/src/device/drm/virtio_gpu.rs,kernel/src/device/drm/mod.rs,kernel/src/lib.rs,tools/riscv/nixos/m19/syncobjtest.c,tools/riscv/nixos/m19/boot_mini_debug.py,book/src/kernel/linux-compatibility/syscall-flag-coverage/file-descriptor-and-io-control/README.md,book/src/kernel/linux-compatibility/syscall-flag-coverage/file-descriptor-and-io-control/ioctl.scml,book/src/kernel/linux-compatibility/syscall-flag-coverage/file-descriptor-and-io-control/eventfd_and_eventfd2.scml
head: 139c9cc8f-dirty
branch: codex/drm-main-sync
title: "DRM syncobj reservation and fence lifetime review"
---

# Summary

The reviewed change substantially improves explicit DRM synchronization: it
uses prepared publication state, cancellable callbacks, bounded iterative
fence chains, fallible userspace-array decoding, and extensive RISC-V ioctl and
Mesa integration coverage. Four isolated persona passes produced 26 comments.

Five major correctness/security findings were confirmed and fixed during the
review: a consumed cached ticket could stop chain polling; execbuffer memory
quota ended before the ticket-owned DMA copy; and resource-fence association,
syncobj waiter registration, and execbuffer syncobj decoding could allocate
infallibly from user-controlled counts. The implementation now caches a
persistent control-queue poll handle, transfers the 64 MiB quota into the
fence, pre-reserves association storage with a 262,144 system bound, and uses
fallible waiter/descriptor reservations. The resolved findings are retained
below as an audit trail of the review-driven changes.

No confirmed critical defect remains. The highest-priority outstanding work is
structural: split the oversized DRM root and syncobj UAPI modules, and express
resource creation and execbuffer submission as narrower typed transactions.
The remaining minor/nit comments concern visibility, boolean policies, units,
test organization, and semantic line breaks; they do not invalidate the tested
runtime behavior.

## Maintainability

### `kernel/src/device/drm/mod.rs` line 47

> ```diff
> use aster_virtio::device::{
>     VirtioDeviceError,
>     gpu::{device::GpuDevice, first_device},
> };
> ```

`qualified-fn-imports` (minor): `first_device` is a free function imported directly and called without its module qualifier, obscuring its origin at each call site.

**Fix.** Import the parent module as `gpu` and call `gpu::first_device()`.

### `kernel/src/device/drm/mod.rs` line 240

> ```diff
> const DUMB_POOL_SIZE: usize = 64 * 1024 * 1024;
> ```

`encode-units` (minor): `DUMB_POOL_SIZE` is a plain `usize`, so its byte unit is absent at uses such as `DumbPool::new` and `VmoOptions::new`.

**Fix.** Rename it to `DUMB_POOL_SIZE_BYTES` and update its uses.

### `kernel/src/device/drm/mod.rs` line 727

> ```diff
> fn retry_pending_ids(
>     pending_ids: &SpinLock<BTreeSet<u32>>,
>     mut try_cleanup: impl FnMut(u32) -> bool,
> ) {
> ```

`closure-fn-suffix` (nit): `try_cleanup` holds an `FnMut` but lacks the required `_fn` suffix, unlike nearby bindings such as `condition_fn` and `decode_fn`.

**Fix.** Rename the parameter to `try_cleanup_fn` and invoke that name in the loop.

### `kernel/src/device/drm/mod.rs` line 1984

> ```diff
> cmd @ ModePageFlip => {
>     let _page_flip_operation = self.lock_page_flip_operation()?;
>     let mut kms_state = self.lock_kms_as_master()?;
>     let req = cmd.read()?;
>     if req.crtc_id != CRTC_ID {
> ```

`single-responsibility` (minor): The `ModePageFlip` arm embeds validation, framebuffer lookup, reservation, presentation, property updates, and completion scheduling inside `DriHandle::ioctl`; the dispatcher therefore owns page-flip implementation details while neighboring complex operations delegate to focused modules.

**Fix.** Move this flow into a focused method such as `kms::page_flip` and leave the dispatch arm as a single delegation.

### `kernel/src/device/drm/mod.rs` line 2253

> ```diff
> // ---------------------------------------------------------------------------
> // Wire types (structs matching Linux UAPI)
> // ---------------------------------------------------------------------------
>
> /// `struct drm_version`; `size_t` is 8 bytes on RISC-V.
> ```

`single-responsibility` (major): `mod.rs` serves simultaneously as the subsystem root, device-state implementation, ioctl dispatcher, helper collection, and a large UAPI wire-layout catalog. The `Wire types` block alone contains many unrelated request structures, making the module root require archaeology for routine changes.

**Fix.** Move the wire structures into a focused `uapi.rs` module, re-exporting only the types needed by `ioctl.rs` and the individual handler modules. Keep `mod.rs` focused on subsystem composition and shared device state.

### `kernel/src/device/drm/syncobj.rs` line 70

> ```diff
> pub(super) struct DrmSyncobjCreate {
>     pub handle: u32,
>     pub flags: u32,
> }
> ```

`narrow-visibility` (minor): The fields of the `DrmSyncobj*` wire structures are exposed to the parent module even though all field access occurs inside `syncobj`; `ioctl.rs` only needs to name the structure types.

**Fix.** Retain `pub(super)` on the structure types needed by `ioctl.rs`, but remove `pub` from their fields.

### `kernel/src/device/drm/syncobj.rs` line 356

> ```diff
> pub(super) fn query_point(&self, last_submitted: bool) -> u64 {
>     self.poll_timeline_completion();
>     let mut state = self.state.lock();
> ```

`no-bool-args` (minor): `query_point` selects between two distinct queries through `last_submitted`, leaving calls such as `query_point(false)` unable to communicate which timeline value is requested.

**Fix.** Split it into named methods such as `signaled_point()` and `last_submitted_point()`. These methods can also be private because their current callers are contained in this module.

### `kernel/src/device/drm/syncobj.rs` line 376

> ```diff
> pub(super) fn wait_for_fence(
>     self: &Arc<Self>,
>     point: u64,
>     wait_for_submit: bool,
> ) -> Result<Arc<Fence>> {
> ```

`no-bool-args` (minor): `wait_for_fence` uses `wait_for_submit` to select immediate failure versus a bounded wait. Call sites pass raw `true`, `false`, or a flag expression, hiding that behavioral choice.

**Fix.** Use a typed mode such as `SubmitWait::Immediate` and `SubmitWait::Wait`, or expose separate `find_submitted_fence` and `wait_for_submitted_fence` methods.

### `kernel/src/device/drm/syncobj.rs` line 772

> ```diff
> Timeline {
>     point: u64,
>     prepared_chain: Option<PreparedFenceChain>,
>     notification: Option<SyncobjNotification>,
>     active: bool,
> },
> ```

`rust-type-invariants` (minor): `SyncobjPublicationKind::Timeline` independently stores `prepared_chain`, `notification`, and `active`, permitting contradictory states such as `active: true` with either resource set to `None`. Correctness consequently depends on manual `take()` and flag updates.

**Fix.** Represent publication state explicitly, for example with `Reserved { point, prepared_chain, notification }` and `Published` variants, or place the reservation count in an RAII guard whose presence alone indicates activity.

### `kernel/src/device/drm/syncobj.rs` line 906

> ```diff
> pub(super) fn read_handles(pointer: u64, count: u32) -> Result<Vec<u32>> {
>
> pub(super) fn read_points(pointer: u64, count: u32) -> Result<Vec<u64>> {
>
> pub(super) fn wait_many(
> ```

`narrow-visibility` (minor): `read_handles`, `read_points`, and `wait_many` are declared `pub(super)` but have no consumers outside `syncobj.rs`; even the tests are descendants that can access private parent items.

**Fix.** Make these three functions private. Keep `lookup_syncobjs` at `pub(super)` because `virtio_gpu` actually consumes it.

### `kernel/src/device/drm/virtio_gpu.rs` line 38

> ```diff
> pub(super) struct DrmVirtgpuExecbuffer {
>     pub flags: u32,
>     pub size: u32,
>     pub command: u64,
> ```

`narrow-visibility` (minor): The fields of the `DrmVirtgpu*` wire structures are exposed to sibling modules despite being read and written only by handlers in `virtio_gpu`; `ioctl.rs` merely needs the structure types for ioctl aliases.

**Fix.** Keep the required structures `pub(super)`, but make their fields private.

### `kernel/src/device/drm/virtio_gpu.rs` line 202

> ```diff
> const MAX_EXECBUFFER_SIZE: usize = 16 * 1024 * 1024;
> const MAX_EXECBUFFER_HANDLES: usize = 4096;
> const MAX_SYSTEM_EXECBUFFER_BYTES: usize = 64 * 1024 * 1024;
> ```

`encode-units` (minor): `MAX_EXECBUFFER_SIZE` and `command_size` are plain `usize` values representing bytes, while the nearby system-wide counterpart correctly encodes `BYTES` in its name.

**Fix.** Rename them to `MAX_EXECBUFFER_BYTES` and `command_size_bytes` so quota and allocation arithmetic carry their unit at each use.

### `kernel/src/device/drm/virtio_gpu.rs` line 389

> ```diff
> let mut resource_created = false;
> let mut context_attached = false;
> let operation = (|| -> Result<()> {
>     handle
>         .gpu_manager
>         .gpu
>         .resource_create_3d(create)?;
>     resource_created = true;
> ```

`rust-native` (major): `virtgpu_resource_create` tracks partial setup through `resource_created` and `context_attached`, mutates them inside an immediately invoked closure, and hand-codes corresponding rollback below. Every new setup step must update several manually coupled paths.

**Fix.** Introduce a `PendingVirtgpuResource` RAII transaction that owns the resource, backing attachment, and context attachment as they are created. Its `Drop` implementation should perform or defer cleanup, while a `publish()` or `commit()` method disarms it after successful copyout.

### `kernel/src/device/drm/virtio_gpu.rs` line 591

> ```diff
> pub(super) fn virtgpu_execbuffer(
>     handle: &super::DriHandle,
>     cmd: crate::util::ioctl::Ioctl<
>         b'd',
>         0x42,
>         true,
>         crate::util::ioctl::InOutData<DrmVirtgpuExecbuffer>,
>     >,
>     file_table: &mut FileTableRefMut,
> ) -> Option<Result<i32>> {
> ```

`single-responsibility` (major): `virtgpu_execbuffer` combines ioctl decoding, userspace byte copying, fence waits, syncobj reservation, GEM validation, context setup, GPU submission, payload publication, input reset, fd installation, and response copyout in one large function. It mixes syscall-level flow with byte-level parsing and transaction bookkeeping.

**Fix.** Extract focused helpers such as `read_execbuffer_bo_handles`, `collect_execbuffer_resources`, and `install_execbuffer_out_fence`, and encapsulate submission/publication state in a transaction object. Keep `virtgpu_execbuffer` as a top-down orchestration of those phases.

### `tools/riscv/nixos/m19/boot_mini_debug.py` line 23

> ```diff
> REPO = Path(__file__).resolve().parents[4]
> ```

`no-magic-number` (minor): `parents[4]` embeds the script's current directory depth as an unexplained repository-layout invariant; moving the script silently selects a different directory.

**Fix.** Find the repository root by walking ancestors for a stable marker such as `Cargo.toml` and `.git`, or accept it through an explicit command-line option.

### `tools/riscv/nixos/m19/boot_mini_debug.py` line 48

> ```diff
> (b"ext4load virtio 0:0 0x80200000 /asterinas.booti", b"bytes read", 30),
> (b"ext4load virtio 0:0 0x90000000 /qemu-virt.dtb", b"bytes read", 10),
> (b"ext4load virtio 0:0 0x83000000 /initramfs.cpio.gz", b"bytes read", 120),
> (b"booti 0x80200000 0x83000000:${initrd_size} 0x90000000", b"Starting kernel", 30),
> ```

`no-magic-number` (minor): The U-Boot command table embeds the kernel, device-tree, and initramfs load addresses as raw literals, and repeats those literals in the final `booti` command. Their memory-layout roles are not visible and future address changes require synchronized string edits.

**Fix.** Define named constants such as `KERNEL_LOAD_ADDR`, `DTB_LOAD_ADDR`, and `INITRD_LOAD_ADDR`, then construct every command from those constants.

### `tools/riscv/nixos/m19/syncobjtest.c` line 197

> ```diff
> int main(void) {
>     int fd = open("/dev/dri/renderD128", O_RDWR | O_CLOEXEC);
>     if (fd < 0) fail("open");
>     if (cap(fd, DRM_CAP_SYNCOBJ) != 1 || cap(fd, DRM_CAP_SYNCOBJ_TIMELINE) != 1)
>         fail("caps");
>     stage("caps");
> ```

`single-responsibility` (minor): `main` contains every syncobj scenario—capability checks, binary and timeline operations, sharing, eventfd, threading, execbuffer submission, stress testing, limits, and cleanup—in one long function. Shared variables named `first` through `sixth` make each stage depend on distant setup.

**Fix.** Extract one function per reported stage, using a small fixture structure for the DRM fd and shared handles. Keep `main` as ordered orchestration plus final cleanup.

## Correctness findings resolved during review

### `kernel/src/device/drm/fence.rs` line 551

> ```diff
> let source = self.poll_source.lock().clone();
> if let Some(source) = source {
>     source.poll_own_ticket();
> } else {
>     self.poll_own_ticket();
> }
> ```

Resolved during review (major): `poll_source` permanently cached one device-backed `Fence`. If that fence completed first and another waiter consumed its `GpuCommandTicket` through `finish_completed()`, another dependency could remain undispatched.

**Applied.** Fence chains now cache a cloneable `GpuCommandPollHandle` whose lifetime is independent of any individual ticket; polling it drains every visible completion on the shared control queue in O(1).

### `kernel/src/device/drm/virtio_gpu.rs` line 762

> ```diff
> let ticket = handle
>     .gpu_manager
>     .gpu
>     .submit_3d_fenced_async(ctx_id, req.size, &cmd_buf, fence_id, fence.clone())?;
> fence.attach(ticket);
> drop(cmd_buf);
> drop(command_quota);
> ```

Resolved during review — `raii` (major): `command_quota` was released immediately after `submit_3d_fenced_async()`, even though the ticket-owned DMA stream remained alive.

**Applied.** `Fence::attach` now takes ownership of `ExecbufferMemoryQuota` and releases it only after consuming the completed ticket, or after the completion-retained fence itself is dropped.

## Security findings resolved during review

### `kernel/src/device/drm/mod.rs` line 652

> ```diff
> let mut tracked = self.tracked_fences.lock();
> tracked.push(fence.clone());
> // ...
> for object_id in object_ids {
>     let fences = resource_fences.entry(*object_id).or_default();
>     // ...
>     fences.push(fence.clone());
> }
> ```

Resolved during review (major): `associate_resource_fence()` ran after irreversible submission but could allocate through the tracked and per-object fence vectors.

**Applied.** Every GEM object now owns its fence-tracking entry from creation. Execbuffer prunes and fallibly reserves all vector slots before submission, enforces a 262,144 association bound, and publishes only into guaranteed capacity afterward.

### `kernel/src/device/drm/syncobj.rs` line 441

> ```diff
> fn register_waker(&self, waker: &Arc<Waker>) {
>     let mut watchers = self.watchers.lock();
>     // ...
>     watchers.push(Arc::downgrade(waker));
> }
> ```

Resolved during review (major): `register_waker()` used infallible `Vec::push()` for every syncobj in a wait request.

**Applied.** Registration now uses `try_reserve(1)` and propagates `ENOMEM`. Earlier entries hold only weak references to the same waiter, so an aborted wait drops their strong owner and they are pruned on the next registration or notification.

### `kernel/src/device/drm/virtio_gpu.rs` line 859

> ```diff
> let handles: Vec<_> = wire.iter().map(|descriptor| descriptor.handle).collect();
> let syncobjs = syncobj::lookup_syncobjs(handle, &handles)?;
> wire.into_iter()
>     .zip(syncobjs)
>     .map(|(descriptor, syncobj)| { /* ... */ })
>     .collect()
> ```

Resolved during review (major): Both `collect()` operations allocated infallibly from the user-controlled descriptor count.

**Applied.** Both vectors now call `try_reserve_exact` and return `ENOMEM` before decoding or publishing descriptors.

## Documentation

### `book/src/kernel/linux-compatibility/syscall-flag-coverage/file-descriptor-and-io-control/README.md` line 158

> ```diff
> One command stream is limited to 16 MiB, and at most 64 MiB of command streams
> may be retained by concurrent submissions system-wide; exceeding those limits
> returns `EINVAL` and `ENOSPC`, respectively.
> ```

`semantic-line-breaks` (nit): Several new compatibility paragraphs break lines inside phrases rather than at semantic boundaries, including `command streams`/`may be retained`, `at most`/`4096`, and `GEM objects`/`references`. This makes future prose diffs noisier and violates the repository's semantic-line-break convention.

**Fix.** Reflow these paragraphs so lines end at sentence or clause boundaries, such as before `and`, after semicolons, or at the end of each sentence.

### `book/src/kernel/linux-compatibility/syscall-flag-coverage/file-descriptor-and-io-control/ioctl.scml` line 33

> ```diff
> drm_buffer_ops = DRM_IOCTL_GEM_OPEN | DRM_IOCTL_GEM_CLOSE |
> ...
> drm_auth_ops = DRM_IOCTL_GET_MAGIC | DRM_IOCTL_AUTH_MAGIC;
> ...
> drm_kms_update_ops = DRM_IOCTL_MODE_SETCRTC | DRM_IOCTL_MODE_PAGE_FLIP |
>                      ... | DRM_IOCTL_MODE_ATOMIC |
> ```

`linux-compat-docs` (minor): The SCML coverage omits implemented user-visible operations including `DRM_IOCTL_VERSION`, `DRM_IOCTL_SET_CLIENT_CAP`, `DRM_IOCTL_SET_MASTER`, `DRM_IOCTL_DROP_MASTER`, `DRM_IOCTL_GEM_FLINK`, and `DRM_IOCTL_MODE_DIRTYFB`, so the compatibility artifact understates the DRM ioctl surface exposed by `kernel/src/device/drm/mod.rs`.

**Fix.** Add these operations to the matching SCML groups, creating a core or master-control group if needed, and include those groups in the generic `ioctl` matcher.

### `kernel/src/device/drm/mod.rs` line 85

> ```diff
> /// Linux DRM character-device major number.
> ...
> /// Linux DRM character-device major number.
> ...
> /// DRI driver file name (`virtio_gpu_dri.so`), which Mesa's loader derives
> /// from this string.
> ```

`semantic-line-breaks` (nit): Doc comments throughout this file use column wrapping that splits cohesive phrases, for example `Mesa's` from `DRI driver`, `QEMU` from `virtio-gpu scanouts`, `GBM surface's` from its buffers, and `ownership remains` from `attached`. These are not semantic boundaries.

**Fix.** Reflow the affected `//!` and `///` prose so every inserted line break falls at a sentence or clause boundary.

### `kernel/src/device/drm/virtio_gpu.rs` line 63

> ```diff
> /// Note: `value` is a userspace **pointer** to a `u64` that the kernel writes
> /// through, not an inline value field.
> ```

`semantic-line-breaks` (nit): Multiple doc blocks split phrases across lines, including `writes through`, `GEM handle`, `capset version and feature bits`, `must complete`, and `pollable fd`. The `virtgpu_execbuffer` documentation similarly splits several verb and noun phrases instead of breaking at clauses.

**Fix.** Reflow these `///` blocks at sentence and clause boundaries, keeping verb phrases and noun phrases together.
