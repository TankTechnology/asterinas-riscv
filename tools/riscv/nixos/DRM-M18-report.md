# DRM-M18: Page-Flip Events (vsync)

**Status:** PASS (21/21) on top of M17 (regression-verified 24/24).

## Motivation

Every real compositor (weston, sway/wlroots, kmscube) drives its frame
loop off `DRM_EVENT_FLIP_COMPLETE` events: it submits a flip with
`DRM_MODE_PAGE_FLIP_EVENT`, blocks in `poll()`, then `read()`s the event
to learn when the frame landed. Without events, userspace can only
busy-poll or guess. M18 adds the event path.

## What was added

### Event delivery (`kernel/src/device/drm/mod.rs`)

- Per-open-file event queue (`VecDeque<DrmEventVblank>`) in `DriInner` —
  events are visible only to the fd that requested the flip, matching
  Linux's per-`drm_file` semantics.
- `read()` on the DRM fd dequeues `drm_event_vblank` records (32 bytes,
  type/length/user_data/tv_sec/tv_usec/sequence/crtc_id). Empty queue:
  `EAGAIN` with `O_NONBLOCK`, blocking wait otherwise. Short buffer:
  `EINVAL`.
- `poll()` reports `POLLIN` when events are pending (via `Pollee`,
  same pattern as evdev).
- Global monotonic flip sequence counter on `GpuManager`; timestamps from
  `read_monotonic_time()`.

### Producers

- `MODE_PAGE_FLIP`: `DRM_MODE_PAGE_FLIP_EVENT` queues the event after the
  present completes (our virtio-gpu present path is synchronous, so the
  event lands immediately and correctly ordered). Unknown flags → EINVAL.
  `DRM_MODE_PAGE_FLIP_ASYNC` accepted (we are synchronous anyway).
- `MODE_ATOMIC`: accepts `DRM_MODE_PAGE_FLIP_EVENT` (0x01) as wlroots uses
  it; queues the event with the commit's `user_data` when an FB_ID was
  presented. Also validates the atomic flag mask
  (TEST_ONLY | NONBLOCK | ALLOW_MODESET | PAGE_FLIP_EVENT), rejecting
  unknown bits.

## Verification

`tools/riscv/nixos/m18/flipevent.c` runs as `/init` on a minimal initramfs
(`build_m18.sh`, `boot_m18.py`, artifacts in `target/drm-m18/`), QEMU
`virtio-gpu-device`, smp=4:

- flip without EVENT → read EAGAIN, poll timeout
- flip with EVENT → poll POLLIN, read returns type=FLIP_COMPLETE,
  length=32, user_data round-trips, crtc=1, monotonic timestamp
- sequence numbers increment across flips (0 → 1 → 3 with atomic)
- short buffer → EINVAL; unknown flip/atomic flags → EINVAL
- atomic commit with PAGE_FLIP_EVENT → event with commit user_data
- render node read → EAGAIN

```
Summary: PASS=21 FAIL=0
M18_ATOMIC_PASS: all page-flip event checks passed
```

## Known limitations

- Events fire when the synchronous present completes — there is no real
  vblank interrupt, so timing is "as fast as the guest submits" rather
  than host-refresh-aligned. Compositors that measure frame pacing from
  event timestamps will see bursty timestamps; a timer-based vblank
  emulator (~60 Hz) can smooth that if needed.
- No `DRM_EVENT_VBLANK` (only `FLIP_COMPLETE`); no CRTC sequence
  calibration ioctls.

## Files

| File | Purpose |
|------|---------|
| `tools/riscv/nixos/m18/flipevent.c` | Page-flip event verification (runs as /init) |
| `tools/riscv/nixos/m18/build_m18.sh` | Build flipevent + initramfs + boot disk |
| `tools/riscv/nixos/m18/boot_m18.py` | QEMU boot + evidence check |
| `kernel/src/device/drm/mod.rs` | Event queue, read/poll, PAGE_FLIP flags |
| `kernel/src/device/drm/atomic.rs` | Atomic flag validation + commit events |
