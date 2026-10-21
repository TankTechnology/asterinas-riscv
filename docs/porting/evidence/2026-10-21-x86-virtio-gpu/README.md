# virtio-gpu on the x86_64 kernel

This is milestone P1 of the shared-DRM plan in
[`docs/porting/drm-shared-core-design.md`](../../drm-shared-core-design.md): the
first graphics transport that the shared `aster-drm` core can drive, and the
first driver written as a plugin on top of it. The device layer lives in
`kernel/core/comps/virtio` and the driver in `kernel/comps/drm-virtio-gpu`, so
the same two crates are what a RISC-V QEMU target will build.

## What was added

- `kernel/core/comps/virtio/src/device/gpu/` — the 2D command set:
  `GET_DISPLAY_INFO`, `RESOURCE_CREATE_2D`, `RESOURCE_ATTACH_BACKING`,
  `SET_SCANOUT`, `TRANSFER_TO_HOST_2D`, `RESOURCE_FLUSH`, `RESOURCE_UNREF`.
  The control queue is synchronous; requests are written into a DMA control
  buffer with the response at a fixed offset. Backing is attached with one
  memory entry per page, so non-contiguous GEM objects work.
- `kernel/comps/drm-virtio-gpu/` — a `DrmDevice` that hands dumb buffers to
  the shared shmem GEM backend, and a `DrmKmsOps` that turns the framebuffer
  into a 2D resource on first scanout and then transfers and flushes dirty
  rectangles. It registers `/dev/dri/card0` through the shared DRM core.

## A poll bug in the shared DRM core

`DrmFile::poll` used to report `IN | OUT` unconditionally. Xorg's
modesetting driver registers the DRM fd in its event loop, so the fd was
ready on every iteration and the server re-entered `drmHandleEvent` forever:
one CPU saturated, clients starved. The file now reports no events, because
the DRM core has no vblank or page-flip event queue yet. This is a
correctness fix for every DRM client that polls the node, not a virtio-gpu
special case.

Evidence that the fix took effect: `/proc/<Xorg>/fdinfo/3` lists `tfd: 13
events: 1` (the DRM fd, registered with `EPOLLIN`), while a `poll()` of fd 13
reports no events and no longer appears in the epoll wakeups (`tfd` numbers
are per-run).

## Acceptance: raw KMS, without Xorg

Kernel build (inside the build container, the same path used by the existing
x86 Debian gates):

```sh
cargo osdk build --target-arch x86_64 --boot-method vmm-direct
```

Guest image: the Debian trixie ext4 root used by the x86 gates, with
`/root/drmprobe` (a static C program that speaks the legacy KMS ioctls and
`mmap`s the dumb buffer) as the only addition.

QEMU adds `-vga none -device virtio-gpu-pci -vnc :12`; the probe runs from a
oneshot systemd unit. The transcript shows:

```
DRMPROBE_VERSION virtio-gpu
DRMPROBE_RESOURCES crtcs=1 connectors=1
DRMPROBE_MODE 1024x768 1024x768@60
DRMPROBE_CRTC 15
DRMPROBE_DUMB handle=1 pitch=4096 size=3145728
DRMPROBE_FILLED
DRMPROBE_ADDFB fb=20
DRMPROBE_SETCRTC ok
DRMPROBE_DIRTYFB ok
DRMPROBE_PASS
```

A monitor-socket `screendump` taken while the probe sleeps is a 1024x768 PPM
with the four bars the probe draws: sampled pixels are `(255,0,0)`,
`(0,255,0)`, `(0,0,255)` and `(255,255,255)`, and all 8108 sampled pixels are
non-black. Scanout therefore works end to end (GEM pages -> virtio-gpu 2D
resource -> host display), independent of any X server.

## Xorg modesetting: what works and what does not

The modesetting driver does bind to the device:

```
modeset(0): Output Virtual-1 connected
modeset(0): Using exact sizes for initial modes
modeset(0): Output Virtual-1 using initial mode 1024x768 +0+0
modeset(0): Damage tracking initialized
```

and a raw X11 client that performs only the connection handshake gets a
valid setup reply (status 1, 1024x768 screen). With `AutoAddDevices off` the
input-driver loading warnings are the only errors in the log.

`xsetroot -solid red` still does not complete. The server accepts the client
connection, processes the request burst, and writes 32-byte replies
(`RECVMSG fd=9 -> len=36`, `WRITEV fd=9 -> len=32`, ...), then blocks in the
next `recvmsg`; the client keeps alternating `poll`/`recvmsg` on its socket
and is killed by its timeout. At that point the server's state is `S` with
`utime`/`stime` frozen and `minflt`/`majflt` unchanged, so it is not
spinning and not taking faults.

The investigation used a temporary syscall trace (gated to the pid that opens
the DRM node), signal-context sampling, and an `LD_PRELOAD` wrapper around
`recvmsg`/`writev`/`poll`/`epoll_wait`; all of that instrumentation was
removed before this commit. The remaining wedge is a client/server
request-reply stall, not a KMS problem, and is left as the next step. A
promising signal is the client-side trace: Xlib polls a unix socket whose
`poll` result includes `POLLOUT`, and the server ends up asleep on a socket
that the client believes still has a pending reply.

## Reusing this on RISC-V

Both new crates are architecture-neutral. The RISC-V QEMU machine already
provides virtio-gpu, so running the same `drmprobe` against the RISC-V build
is the cross-architecture half of P1; the device probe and the DRM plugin do
not change.
