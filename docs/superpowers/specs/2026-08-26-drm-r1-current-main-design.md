# Current-Main DRM R1 Design

Date: 2026-08-26

## Goal

Establish a current-main, reviewable DRM R1 baseline for RISC-V: retain the
existing virtio-gpu 2D/KMS path, add a Linux-compatible hardware cursor, and
prove the combined path in one bounded generic-Sv39, SMP=4 QEMU gate.

This milestone is deliberately smaller than the historical `track/drm-m20`
branch. It does not add GEM render nodes, virgl, atomic modesetting, page-flip
events, PRIME, Mesa acceleration, or Megrez display hardware. Those remain
separate follow-on slices once R1 is stable.

## Current baseline

Current main already provides:

- `/dev/dri/card0` with the modesetting discovery ioctls;
- per-open dumb-buffer handles, framebuffer ids, `mmap`, `SETCRTC`, `DIRTYFB`,
  and synchronous page-flip presentation;
- a virtio-gpu 2D control-queue implementation that creates, attaches,
  transfers, flushes, and unreferences scanout resources;
- a generic-Sv39/SMP=4 RISC-V build and bounded QEMU lifecycle helpers.

The virtio-gpu cursor queue is created but unused. The DRM device does not
accept `DRM_IOCTL_MODE_CURSOR` or `DRM_IOCTL_MODE_CURSOR2`.

## Source-of-truth policy

Current main remains authoritative. Historical DRM work is used only for
behavioral evidence and milestone decomposition. R1 will not cherry-pick the
old driver wholesale because that branch predates hundreds of current-main
changes and mixes later GEM, virgl, packaging, and test-harness work.

The implementation follows the Linux DRM UAPI in `drm_mode.h` and the
VirtIO 1.3 GPU cursor commands. Exact external behavior is frozen by new tests
before implementation.

## DRM cursor contract

Both legacy cursor ioctls use Linux's `drm_mode_cursor` layout. The first form
sets `hot_x = hot_y = 0`; `CURSOR2` supplies explicit hotspots.

Supported flags are:

- `DRM_MODE_CURSOR_BO`: select or replace the cursor buffer;
- `DRM_MODE_CURSOR_MOVE`: move the cursor;
- both flags together: update and move in one request.

The contract requires the known CRTC id, rejects unknown flags, and bounds the
cursor to 64x64 pixels. A non-zero handle must name a 32-bpp dumb buffer whose
declared dimensions and backing span cover the requested cursor. Hotspots must
lie inside a non-empty cursor. Handle zero hides the cursor. Move-only requests
do not require a buffer handle.

Validation is separated from I/O: a small cursor module parses the UAPI request
into a validated operation; `dri.rs` resolves the per-open handle and physical
backing, drops its state lock, then calls the GPU. No virtqueue command runs
while the DRM state spinlock is held.

## VirtIO cursor path

The GPU driver adds the standard cursor-position and update request layouts.
The cursor backing is exposed as a B8G8R8A8 resource, attached through the
existing control queue, transferred to the host, and then selected through the
cursor queue. Move-only requests use `MOVE_CURSOR`; buffer replacement and
hide use `UPDATE_CURSOR`.

Cursor-queue commands are request-only. Completion is a used descriptor with
zero response bytes, so the driver waits with `pop_used()` rather than
requiring a control response body. The cursor queue size is chosen from the
device's advertised maximum instead of assuming 64 descriptors, preserving
compatibility with QEMU virtio-pci devices that expose a smaller cursor queue.

The GPU owns one active cursor resource. A successful replacement publishes
the new resource before best-effort unref of the old one; a failed setup
unrefs the new resource and leaves the previous resource active. Hiding sends
resource id zero and then unreferences the old resource. Resource ids share the
existing monotonic allocator with scanout resources.

## State and lifetime

Each DRM open retains its current cursor dimensions, hotspot, and handle for
UAPI validation, while the GPU device owns the host-visible active cursor
resource. Destroying a dumb buffer that is currently used by that open's cursor
is rejected with `EBUSY`; this avoids a dangling guest-memory backing. Closing
the DRM file hides its cursor best-effort before its pool is released.

Concurrent cursor ioctls are serialized at the GPU's cursor state boundary.
The cursor-state lock is never held across a control-queue or cursor-queue
submission; the state is committed only after successful device commands.

## Verification

R1 has three layers:

1. pure kernel tests for UAPI parsing, flag combinations, dimensions, hotspots,
   handles, and checked backing-size calculations;
2. virtio-gpu tests for exact wire layout, queue-size selection, and the
   zero-length used-buffer completion rule;
3. one local QEMU generic-Sv39/SMP=4 gate that creates a dumb cursor, issues
   set/move/hide ioctls, and requires guest markers plus QEMU
   `virtio_gpu_update_cursor` trace evidence. QEMU uses this single event for
   both commands and records `update` or `move` in the event payload.

The QEMU gate uses total deadlines, process-group cleanup, complete serial and
trace capture, and invalidates stale success before launch. A screenshot is
not used to prove the cursor because QEMU cursor overlays are not guaranteed to
appear in framebuffer screendumps.

## Acceptance and non-claims

R1 passes when current main compiles for RISC-V, existing DRM behavior remains
green, cursor validation tests pass, and a single SMP=4 QEMU run proves
set/move/hide through both the guest ioctl markers and host VirtIO trace
events. This is a reliable software-rendered desktop/input foundation; it is
not a claim of 3D acceleration or Megrez HDMI support.
