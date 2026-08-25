# Current-Main DRM R1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Linux-compatible virtio-gpu hardware cursor to current main and prove the existing 2D/KMS plus cursor path on generic-Sv39 SMP=4 QEMU.

**Architecture:** Keep current main authoritative. Add a small pure cursor-contract module below the existing DRM device, extend the existing virtio-gpu driver with request-only cursor-queue commands and explicit resource lifetime, then add a bounded host gate using current RISC-V lifecycle conventions. Do not import historical GEM/virgl/atomic code.

**Tech Stack:** Safe Rust kernel code, VirtIO GPU 1.3, Linux DRM legacy cursor UAPI, OSTD ktests, Python 3/Bash gate tooling, QEMU RISC-V generic-Sv39 SMP=4.

---

### Task 1: Freeze the cursor UAPI contract

**Files:**
- Create: `kernel/src/device/dri/cursor.rs`
- Modify: `kernel/src/device/dri.rs`

- [ ] Add failing ktests for `MODE_CURSOR`/`MODE_CURSOR2` layout, supported flag
  combinations, exact CRTC, 64x64 limit, 32-bpp buffer requirements, checked
  backing span, hotspots, hide, and move-only behavior.
- [ ] Run the focused compile/test gate and record RED from the missing cursor
  parser.
- [ ] Implement a pure parser that returns a validated operation without
  touching DRM state or hardware.
- [ ] Run focused GREEN and commit as
  `test(drm): define legacy cursor contract`.

### Task 2: Implement the VirtIO cursor queue

**Files:**
- Modify: `kernel/comps/virtio/src/device/gpu/mod.rs`
- Modify: `kernel/comps/virtio/src/device/gpu/device.rs`

- [ ] Add failing tests for exact cursor wire sizes/fields, advertised
  queue-size selection, and zero-byte used completion.
- [ ] Add cursor-position/update wire structures and create the cursor queue at
  a supported power-of-two size no larger than the device maximum.
- [ ] Implement request-only `MOVE_CURSOR` and `UPDATE_CURSOR` submission using
  `pop_used()`, not a response buffer.
- [ ] Add cursor resource create/attach/transfer/update/hide and rollback-safe
  replacement without holding cursor state across queue I/O.
- [ ] Run focused GREEN and RISC-V ktest compile, then commit as
  `feat(virtio-gpu): drive hardware cursor queue`.

### Task 3: Connect DRM ioctls and lifetime

**Files:**
- Modify: `kernel/src/device/dri.rs`
- Modify: `kernel/src/device/dri/cursor.rs`

- [ ] Add failing integration ktests for handle lookup, physical backing,
  destroy-while-active rejection, set/move/hide state transitions, and cleanup
  on close.
- [ ] Register typed `MODE_CURSOR` and `MODE_CURSOR2` ioctl definitions and
  dispatch through the pure parser.
- [ ] Resolve the dumb buffer under the DRM state lock, copy the hardware
  parameters, release the lock, call the GPU, and commit per-open state only on
  success.
- [ ] Run focused GREEN, `cargo osdk check --ktests` for RISC-V, and the
  relevant Clippy gate. Commit as `feat(drm): expose virtio hardware cursor`.

### Task 4: Add one reusable SMP=4 cursor gate

**Files:**
- Create: `tools/riscv/drm/cursor_gate_init.c`
- Create: `tools/riscv/drm/build_cursor_gate.sh`
- Create: `tools/riscv/drm/cursor_gate.py`
- Create: `tools/riscv/tests/test_drm_cursor_gate.py`
- Modify: `Makefile`
- Modify: `tools/riscv/README.md`

- [ ] Add host RED tests for deterministic initramfs construction, exact
  generic-Sv39/SMP=4/no-network QEMU argv, total deadlines, stale-evidence
  invalidation, process-group teardown, and marker/trace classification.
- [ ] Add a static RISC-V guest probe that opens `/dev/dri/card0`, creates and
  maps a 64x64 ARGB dumb buffer, then performs set, move, and hide ioctls with
  one stable marker after each successful operation.
- [ ] Launch QEMU with only current-main artifacts and
  `virtio_gpu_update_cursor`/`virtio_gpu_move_cursor` tracing; reject panic,
  timeout, missing markers, or missing trace events.
- [ ] Run host GREEN and commit as
  `test(riscv): automate DRM cursor gate`.

### Task 5: Run one real decision gate and record evidence

**Files:**
- Create after success: `docs/porting/evidence/2026-08-26-drm-r1-current-main.md`
- Modify after success: `docs/porting/README.md`

- [ ] Reuse verified Sv39/SMP=4 kernel and boot artifacts when hashes match;
  build only stale inputs locally in the pinned container.
- [ ] Run exactly one bounded QEMU cursor gate. Inspect live local output rather
  than waiting silently, and do not monitor remote CI.
- [ ] If red, classify the first failed layer and add one focused RED before a
  code change. Do not repeat an unchanged QEMU run.
- [ ] If green, record commands, artifact hashes, duration, all guest markers,
  trace-event counts, and non-claims. Commit as
  `docs(riscv): record current-main DRM R1 evidence`.

### Task 6: Choose the next admitted DRM slice

- [ ] Compare the R1 evidence with issues #71-#74 and the historical M16-M20
  reports without merging their implementation wholesale.
- [ ] Prefer Megrez U-Boot framebuffer to `/dev/fb0` as the next experience
  milestone unless R1 exposes a current-main regression that must be fixed
  first.
- [ ] Keep GEM/renderD128, atomic/page-flip events, PRIME, and virgl as separate
  acceptance gates so regressions remain attributable.
