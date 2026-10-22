# A second CRTC, and page flips that really are asynchronous

T1 of the desktop push. Two things were still synchronous or single-pipeline:

- `DRM_IOCTL_MODE_PAGE_FLIP` performed the whole commit (transfer + flush) inside
  the ioctl and queued the completion event immediately, so `PAGE_FLIP_EVENT`
  did not line up with a scanout.
- The driver created one CRTC/encoder/connector no matter how many scanouts the
  host exposed.

## Asynchronous page flips

The request is now validated in the ioctl and queued on the CRTC; the ioctl
returns. Applying it needs the KMS object store lock and a virtio transfer, so
it cannot run in the timer softirq:

```
ioctl            PAGE_FLIP -> crtc.queue_flip(PendingFlip)          (returns)
timer softirq    tick_vblank -> flip_due = true, worker.notify()
worker thread    take_due_flip -> commit_modeset -> FLIP_COMPLETE event
```

- `PendingFlip` carries the framebuffer, the connector routing, the mode, the
  user data, and an `Arc<DrmEventQueue>` — not the `DrmFile` — so the worker
  can deliver the event without holding a file open or creating a cycle. The
  event queue moved into `DrmEventQueue` (`kernel/comps/drm/src/file.rs`), owned
  by the file and shared with pending flips.
- One `FlipWorker` per DRM device (`kernel/comps/drm/src/flip.rs`) holds its
  CRTCs directly, so the softirq wake-up path never touches the object store.
- `aster_core::spawn_kernel_thread` exposes the kernel-thread spawner to
  components, next to the existing `EventSource`/`IntervalTimer` helpers.
- A second flip while one is pending returns `EBUSY`, as on Linux.

The probe proves the deferral: right after the ioctl it polls with a zero
timeout and requires *no* event yet, then waits for the event. A synchronous
implementation fails that check on every attempt; a deferred one can lose a
single attempt if the 60 Hz tick lands between the ioctl and the poll (seen on
TCG), so the probe retries up to five times.

```
FLIPPROBE_VBLANK_RELATIVE ms=16.91 sequence=2      (x86)
FLIPPROBE_FLIP_DEFERRED ioctl_ms=0.04
FLIPPROBE_FLIP_EVENT sequence=5
FLIPPROBE_FLIP_ASYNC ok
FLIPPROBE_PASS
```

```
FLIPPROBE_VBLANK_RELATIVE ms=27.81 sequence=2      (RISC-V)
FLIPPROBE_FLIP_DEFERRED ioctl_ms=3.88
FLIPPROBE_FLIP_EVENT sequence=6
FLIPPROBE_PASS
```

The ASYNC flip is applied by the worker on a later scanout, so the probe now
polls `GETCRTC` for up to 200 ms instead of reading the state immediately.

## A second CRTC

`config.num_scanouts` from the virtio-gpu device now decides how many pipelines
exist:

- The device layer keeps one rect and one current resource per scanout and
  takes the scanout id in `set_scanout`/`disable_scanout`; `present` only unrefs
  a resource when no other scanout still shows it.
- `drm-virtio-gpu` creates one primary plane, CRTC, encoder and connector per
  scanout; the CRTC index is the scanout id handed to `SET_SCANOUT`.
- QEMU reports one scanout per display backend, so `-display none` gives one
  even with `max_outputs=2`; the probe runner gained `--gpu-option` and
  `--vnc auto` for this case.

`tools/drm/multi_crtc_probe.c` modesets both CRTCs with their own connector and
framebuffer, flips CRTC 1 asynchronously, checks that the completion event
names CRTC 1 and that CRTC 0 keeps its framebuffer, then flips CRTC 0 back:

```
MULTICRTC_RESOURCES crtcs=2 connectors=2 encoders=2
MULTICRTC_MODESET crtc0=18 crtc1=22 fb0=27 fb1=28
MULTICRTC_FLIP crtc1 fb=29 sequence=3
MULTICRTC_FLIP crtc0 fb=27 sequence=4
MULTICRTC_PASS
```

The same transcript appears on x86_64 and RISC-V.

## Gates

```sh
make test_x86_flip          # async flip probe, PASS
make test_x86_multi_crtc    # second CRTC, PASS
make test_riscv_multi_crtc  # second CRTC, PASS
make test_x86_graphical     # DRI3 gate with async flips: PASS
make test_riscv_graphical   # DRI3 gate with async flips: PASS
```

The Xorg gate matters here: the server flips through `PAGE_FLIP` for Present,
so it exercises the deferred path on every frame of the session.

## Boundaries

- `WAIT_VBLANK` still selects the first CRTC (the request's CRTC field is not
  parsed yet); each CRTC does keep its own sequence for events.
- Each CRTC has one primary plane; no cursor/overlay planes yet.
- Only scanout 0 is visible in the VNC output; the second CRTC is validated
  through KMS state and its completion events, not by pixels.
- The worker applies flips strictly on the software 60 Hz tick; there is no
  hardware completion signal to synchronise with.
