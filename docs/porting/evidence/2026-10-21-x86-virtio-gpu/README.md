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

The first attempt to paint the root window did not complete: Xlib's
`ChangeWindowAttributes` + `ClearArea` burst never got a reply. That was
first misread as a socket-level request/reply stall (the trace showed the
server writing 32-byte reply packets and then sleeping in `recvmsg`), which
sent the investigation after the unix-socket path. It was not that.

The actual cause was a kernel page fault that could never be resolved. The
modesetting driver `mmap`s the dumb buffer and writes it. `MapHandle::map_frame`
treats a present page as "the page fault was already handled" and returns.
That is true for RAM, but on the device path the handler is the only thing
that can resolve a fault: the server had a PTE with `R | ACCESSED | DIRTY`
(no `W`), while the mapping's permissions were `READ | WRITE | MAY_*`. The
first write fault mapped the page `R|W`; a later fault (the same range
touched through a second VMA or after a `mprotect`-style downgrade) found the
page present, returned without fixing the permissions, and the write fault
repeated. Kernel tracing gated on the pid that opens the DRM node showed the
fault storm at ~200k faults/s on the framebuffer page, so the server never
got back to its event loop to write the reply.

The fix is in `kernel/core/src/vm/vmar/vm_mapping.rs`: when `map_frame` finds
a present RAM page whose flags do not contain the mapping's required
`R/W/X`, it upgrades the PTE with `protect_next` and flushes the TLB, instead
of returning. RAM mappings are unaffected (their frames are already present
with the right permissions), and `map_iomem` keeps its original early
return.

After the fix, the same probe returns:

```
XREQ2_REPLY type=1 seq=3 ms=12      # ChangeWindowAttributes + ClearArea + GetInputFocus
XS_SETROOT=0                        # the real xsetroot -solid red
```

and the monitor `screendump` is 1024x768 with every sampled pixel red.

The investigation used a temporary syscall trace (gated to the pid that opens
the DRM node), fault tracing in the page-fault handler, signal-context
sampling, and an `LD_PRELOAD` wrapper around
`recvmsg`/`writev`/`poll`/`epoll_wait`; all of that instrumentation was
removed before this commit. Two observations from that pass remain useful:
`MSG_WAITALL` and `MSG_DONTWAIT` are accepted but ignored by the unix stream
socket (`RecvFlags::SUPPORTED` is only `MSG_PEEK | MSG_TRUNC`), and only
`O_NONBLOCK` makes a blocking unix-socket `recv` return early.

## Regression gate

`tools/x86/debian/xorg_check.sh` turns the acceptance above into a gate that
runs unattended, in the same shape as the other `*_check.sh` scripts:

```sh
make test_x86_debian_xorg \
  X86_DEBIAN_KERNEL=target/osdk/asterinas/asterinas-osdk-bin.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=target/x86-debian/m4/stage1/initramfs-initpath.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=target/final-matrix/xwrap/image18.ext2
```

It logs in on `ttyS0` as the image's account, installs `xserver-xorg-core`
and `x11-xserver-utils` over slirp, starts the modesetting driver on
`/dev/dri/card0`, runs `DISPLAY=:0 xsetroot -solid red`, and requires four
things before it prints `PASS`: `XORG_SETROOT=0` on the console, a monitor
`screendump` whose sampled pixels are at least 90% red, a power-button
power-off through the monitor, and a clean `e2fsck -fn`. QEMU is started with
`-vga none -device virtio-gpu-pci`; without `-vga none` the default VGA
device owns the display and the captured surface is not the virtio-gpu
scanout.

The script is picked up automatically by
`tools/x86/tests/test_x86_debian_gate_scripts.py`, so `--help` and syntax are
checked by `make test_x86_debian_tooling_unit`.

The first unattended run passed:

```
XORG_CLIENT user=True pass=True command=True done=True
screendump 1024x768 red=1.000
PASS (serial login, Xorg modeset on virtio-gpu, red root window, power-button power-off)
```

with `XORG_SETROOT=0` and `XORG_ok_END` on the console, and a clean
`e2fsck -fn` after the power-off.

## Reusing this on RISC-V

Both new crates are architecture-neutral. The RISC-V QEMU machine already
provides virtio-gpu, so running the same `drmprobe` against the RISC-V build
is the cross-architecture half of P1; the device probe and the DRM plugin do
not change.
