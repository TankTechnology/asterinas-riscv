---
date: 2026-08-28
mode: files
files: kernel/src/device/drm/fence.rs,kernel/src/device/drm/syncobj.rs,kernel/src/device/drm/virtio_gpu.rs,tools/riscv/nixos/m19/syncobjtest.c,tools/riscv/nixos/m19/boot_mini_debug.py
head: ac9d8d660-dirty
branch: codex/drm-main-sync
title: "DRM syncobj reservation and lifetime final review"
---

# Summary

The review found real capacity, stale-readiness, IRQ-allocation, callback-
lifetime, and lock-scope defects. The implementation now uses RAII publication
and queue reservations, generation-bounded event notification, system-wide
watcher and chain quotas, cancellable fence callbacks, and userspace copies
outside device-wide resource locks. Kernel DRM tests and the complete public
RISC-V virgl gate pass after these corrections.

One hardening item remains: publication no longer returns a recoverable
capacity error after GPU submission, but several kernel allocations still use
the workspace's infallible allocation interfaces. Fully preallocating the
chain node, callback storage, and event notification work before transport
submission would also make allocator exhaustion non-fatal.

## Maintainability

### `kernel/src/device/drm/syncobj.rs` line 170

> ```diff
> struct SyncObjectState {
>     payload: Option<SyncPayload>,
>     points: VecDeque<TimelinePoint>,
>     reserved_point_count: usize,
> }
> ```

`rust-type-invariants` (minor): Timeline storage is separate from
`SyncPayload::Timeline` so capacity can be promised before publication. This
makes the cross-field invariant representable but not enforced by the type.

**Fix.** Keep the invariant documented beside `SyncObjectState`; if the state
machine grows again, introduce explicit binary, reserved, and timeline variants.

### `tools/riscv/nixos/m19/syncobjtest.c` line 192

> ```diff
> int main(void) {
>     /* independent ABI scenarios through the end of the file */
> }
> ```

`single-responsibility` (minor): The public client now combines binary,
timeline, sharing, eventfd, lifetime, execbuffer, stress, and bound tests in one
large function.

**Fix.** Split future additions into scenario helpers while retaining one
process and one final gate marker.

## Correctness

### `kernel/src/device/drm/virtio_gpu.rs` line 720

> ```diff
> let ticket = handle.gpu_manager.gpu.submit_3d_fenced_async(...)?;
> // ...
> for publication in output_publications {
>     publication.publish(fence.clone());
> }
> ```

Allocation failure after submission (major): Timeline and completion-queue
capacity is reserved before submission, but `Fence::chain`, callback boxing,
and some `Arc` creation still use infallible allocation after the transport has
accepted the command. Extreme allocator exhaustion can therefore abort the
kernel rather than return `ENOMEM` before submission.

**Fix.** Extend `SyncobjPublication` with fully prepared chain nodes and
callback storage, so post-submit publication performs only non-allocating state
updates.

## Security

No additional security finding remains after verification. System-wide RAII
quotas now bound retained eventfd watchers and pending chain-completion slots;
obsolete syncobj fence callbacks are cancellable and released on replacement,
reset, or object destruction.

## Documentation

No documentation defects remain after verification. The implementation notes
record the reservation contract, quotas, stress coverage, and final gate
evidence.

## Retracted by verification

- The completion-queue `Vec::push` IRQ-allocation finding was fixed by
  pre-reserving one queue slot per chain and transferring that RAII slot on
  enqueue.
- The eventfd stale-readiness race was fixed with a registration-generation
  cutoff; new watchers perform their own readiness check.
- The event-watcher snapshot allocation was removed; notification now retains
  entries in place under the IRQ-safe watcher lock.
- The BO-handle userspace read under resource locks was moved before both
  device-wide locks.
- Unbounded obsolete fence callbacks were fixed with cancellable callback
  registrations retained by the current syncobj/chain owner.
- Per-object eventfd accounting was supplemented by a 16,384-entry system-wide
  RAII quota.
- Point-zero reservation leakage was removed by a single binary/timeline
  `SyncobjPublication` token interface.
- The blocking eventfd test and timing-dependent handle-destroy test were
  replaced by bounded polling and deterministic cross-DRM-fd lifetime checks.
- Magic watcher-limit literals were replaced by `MAX_EVENT_WATCHERS`.
