# Page-flip events and vblank on the x86 line (P3, second block)

Continues P3 of
[`docs/porting/drm-shared-core-design.md`](../../drm-shared-core-design.md)
after [`2026-10-22-x86-atomic-kms.md`](2026-10-22-x86-atomic-kms.md). The
previous block rejected page-flip events and returned `EOPNOTSUPP` for
`WAIT_VBLANK`; this block gives both a real, if bounded, implementation.

## Kernel

### Event delivery

- `aster_core::events::EventSource` is the public `poll_wait` analogue for
  component crates: it wraps the kernel `Pollee` for `poll` registration and
  keeps a raw `Waker` list for blocking file operations.
- `DrmFile` now owns an event queue of `drm_event_vblank` records
  (`kernel/comps/drm/src/event.rs`) and an `EventSource`:
  - `poll` reports `IN` only when the queue is non-empty (the previous
    "always ready" bug stays fixed);
  - `read` returns one 32-byte event, returns `EAGAIN` with `O_NONBLOCK`, and
    blocks otherwise;
  - a buffer smaller than one event is `EINVAL`.

### Page flips

- `DRM_IOCTL_MODE_PAGE_FLIP` (0xb0) swaps the framebuffer of an active CRTC
  while keeping its mode and connector routing. `ASYNC` is accepted; there is
  no hardware flip queue, so the transfer/flush still runs inside the ioctl.
- With `DRM_MODE_PAGE_FLIP_EVENT`, a `DRM_EVENT_FLIP_COMPLETE` record carrying
  the request's `user_data`, the post-flip vblank sequence and the CRTC id is
  queued on the calling file, waking `poll`.
- Atomic commits now accept `DRM_MODE_ATOMIC_PAGE_FLIP_EVENT` and
  `DRM_MODE_ATOMIC_NONBLOCK` and emit the same completion event with the
  atomic request's `user_data`. `PAGE_FLIP_ASAP` remains `EINVAL`.

### Vblank

- Each CRTC carries a software vblank state
  (`kernel/comps/drm/src/kms/objects/crtc.rs`): a sequence counter, the
  timestamp of the last scanout, and an IRQ-safe waiter list. The counter
  advances on every completed scanout (a modeset or flip that transferred and
  flushed the framebuffer) **and** from a 60 Hz software tick that is started
  when the CRTC becomes active and stopped when it is disabled.
  `aster_core::IntervalTimer` is the public periodic-timer wrapper; its
  callback runs in the timer softirq with interrupts disabled, so the vblank
  state uses atomics plus an IRQ-safe waiter list only.
  This makes a relative wait for the next vblank complete (~17 ms) without any
  flip.
- `DRM_IOCTL_WAIT_VBLANK` (0x3a) supports relative and absolute requests and
  `DRM_VBLANK_EVENT` (queues a `DRM_EVENT_VBLANK` record and returns
  immediately). The reply carries the current sequence and timestamp.
  Only CRTC 0 is addressable; `SECONDARY`, `FLIP`, `NEXTONMISS` and the
  high-CRTC index bits are `EINVAL`.

## Acceptance

### `tools/drm/flip_probe.c`

```
FLIPPROBE_VBLANK sequence=1
FLIPPROBE_VBLANK_RELATIVE ms=17.49 sequence=2
FLIPPROBE_FLIP_EVENT sequence=3
FLIPPROBE_FLIP_STATE ok
FLIPPROBE_FLIP_ASYNC ok
FLIPPROBE_VBLANK_WAIT sequence=4
FLIPPROBE_VBLANK_EVENT ok
FLIPPROBE_ERRORS ok
FLIPPROBE_PASS
```

After establishing the mode, the probe waits for one vblank *without*
flipping (relative wait) and requires that the software tick advanced the
sequence and took at least 5 ms. It then flips to a second
framebuffer with `EVENT`, polls and reads the completion (checking the event
type, length, `user_data` and sequence), verifies `GETCRTC` reports the flipped
framebuffer, flips back with `ASYNC`, waits for the current sequence with an
absolute `WAIT_VBLANK`, requests a vblank event and reads it through the same
queue, and checks the negative cases (unknown flip flags -> `EINVAL`, missing
framebuffer -> `ENOENT`).

### Atomic path (`tools/drm/atomic_probe.c`)

The probe now also commits with `PAGE_FLIP_EVENT` and reads the completion:

```
ATOMICPROBE_REJECT_PAGE_FLIP_ASAP ok
ATOMICPROBE_FLIP_EVENT sequence=3
ATOMICPROBE_VBLANK sequence=3
ATOMICPROBE_PASS
```

### RISC-V

The same three probes pass on the RISC-V Sv39 `Image` (QEMU virt,
`-device virtio-gpu-device`), with identical transcripts:

```
PASS (riscv64 flip_probe)
PASS (riscv64 atomic_probe)
PASS (riscv64 sync_probe)
```

### Regressions

- `tools/drm/virtio_gpu_probe.c` (legacy KMS) still passes.
- `tools/drm/sync_probe.c` still passes.
- The x86 Xorg gate still passes: `screendump 1024x768 red=1.000`, power-button
  power-off, clean `e2fsck -fn`.

### Reproduction

```sh
cargo osdk build --target-arch x86_64 --boot-method vmm-direct
cp target/osdk/asterinas/asterinas-osdk-bin.elf target/prime/kernels/x86.elf

make test_x86_flip
make test_x86_atomic
make test_x86_syncobj
```

## Boundaries of this block

- Vblank is software: a 60 Hz tick per active CRTC advances the sequence, and
  completed scanouts advance it as well. There is no hardware refresh
  interrupt, so timestamps are softirq-time, not scanout-time.
- `ASYNC` flips are applied synchronously; the flag is accepted because the
  result is indistinguishable for this driver.
- `WAIT_VBLANK` addresses CRTC 0 only, and its wait is not interruptible by
  signals.
- No `CRTC_SEQUENCE` events, no vblank-based page-flip timestamping, and
  `atomic` page-flip events carry no fence — sync-object fence import remains
  future work.
