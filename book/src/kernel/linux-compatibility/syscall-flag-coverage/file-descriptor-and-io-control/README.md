# File Descriptor and I/O Control

<!--
Put system calls such as
dup, dup2, dup3, fcntl, ioctl, pipe, pipe2, splice, tee, vmsplice, sendfile,
eventfd, eventfd2, memfd_create and fadvise64
under this category.
-->

### `fcntl`

Supported functionality in SCML:

```c
{{#include fcntl.scml}}
```

Unsupported commands:
* `F_NOTIFY`
* `F_OFD_SETLK`, `F_OFD_SETLKW` and `F_OFD_GETLK`
* `F_GETOWN_EX` and `F_SETOWN_EX`
* `F_GETSIG` and `F_SETSIG`
* `F_SETLEASE` and `F_GETLEASE`
* `F_SETPIPE_SZ` and `F_GETPIPE_SZ`
* `F_GET_RW_HINT` and `F_SET_RW_HINT`
* `F_GET_FILE_RW_HINT` and `F_SET_FILE_RW_HINT`

For more information,
see [the man page](https://man7.org/linux/man-pages/man2/fcntl.2.html).

### `pipe` and `pipe2`

Supported functionality in SCML:

```c
{{#include pipe_and_pipe2.scml}}
```

Silently-ignored flags:
* `O_DIRECT`
* `O_NONBLOCK`

For more information,
see [the man page](https://man7.org/linux/man-pages/man2/pipe.2.html).

### `eventfd` and `eventfd2`

Supported functionality in SCML:

```c
{{#include eventfd_and_eventfd2.scml}}
```

For more information,
see [the man page](https://man7.org/linux/man-pages/man2/eventfd.2.html).

### `memfd_create`

Supported functionality in SCML:

```c
{{#include memfd_create.scml}}
```

Silently-ignored flags:
* `MFD_HUGETLB`

Unsupported flags:
* `MFD_HUGE_64KB`
* `MFD_HUGE_512KB`
* `MFD_HUGE_1MB`
* `MFD_HUGE_2MB`
* `MFD_HUGE_8MB`
* `MFD_HUGE_16MB`
* `MFD_HUGE_32MB`
* `MFD_HUGE_256MB`
* `MFD_HUGE_512MB`
* `MFD_HUGE_1GB`
* `MFD_HUGE_2GB`
* `MFD_HUGE_16GB`

For more information,
see [the man page](https://man7.org/linux/man-pages/man2/memfd_create.2.html).

### `fadvise64`

Supported functionality in SCML:

```c
{{#include fadvise64.scml}}
```

Silently-ignored flags:
* `POSIX_FADV_NORMAL`
* `POSIX_FADV_RANDOM`
* `POSIX_FADV_SEQUENTIAL`
* `POSIX_FADV_WILLNEED`
* `POSIX_FADV_DONTNEED`
* `POSIX_FADV_NOREUSE`

For more information,
see [the man page](https://man7.org/linux/man-pages/man2/posix_fadvise.2.html).

### `epoll_ctl`

Supported functionality in SCML:

```c
{{#include epoll_ctl.scml}}
```

Unsupported flags in events:
* `EPOLLEXCLUSIVE`
* `EPOLLWAKEUP`

For more information,
see [the man page](https://man7.org/linux/man-pages/man2/epoll_ctl.2.html).

### `poll` and `ppoll`

Supported functionality in SCML:

```c
{{#include poll_and_ppoll.scml}}
```

Unsupported events:
* `POLLRDBAND`
* `POLLWRNORM`
* `POLLWRBAND`

For more information,
see [the man page](https://man7.org/linux/man-pages/man2/poll.2.html).

### `ioctl`

Supported functionality in SCML:

```c
{{#include ioctl.scml}}
```

DRM handle-creating operations publish a new GEM handle only after the ioctl response has been copied to userspace successfully.
If copying the response fails, the handle remains inaccessible and a new dumb-buffer pool reservation is rolled back after successful device cleanup.
If the device cannot confirm resource destruction, the reservation is quarantined instead of being reused.

DRM primary-node clients can obtain an authentication token with `DRM_IOCTL_GET_MAGIC`.
The current DRM master can consume that token with `DRM_IOCTL_AUTH_MAGIC`, after which the client may use authentication-gated GEM operations such as `DRM_IOCTL_GEM_FLINK` and `DRM_IOCTL_GEM_OPEN`.
Render nodes do not use this legacy authentication flow.

When virtio-gpu is available, DRM exposes the `virtio_gpu` primary and render nodes.
Without virtio-gpu, a compatible firmware framebuffer exposes a `simpledrm` primary node only.
The firmware backend reports `DRM_CAP_DUMB_PREFER_SHADOW`, zero cursor dimensions, and a single fixed mode.
It implements legacy modesetting, dirty-framebuffer updates, and page flips by copying `XRGB8888` or `ARGB8888` dumb-buffer rows into the BGR-reserved scanout left active by firmware;
the high byte is reserved by the scanout and alpha is not blended.
`DRM_IOCTL_MODE_DIRTYFB` accepts `flags == 0` and at most 4096 nonempty, in-bounds clip rectangles.
Zero clips request a full redraw.
Damage whose aggregate area reaches one frame is collapsed to a single full redraw.
Firmware remains responsible for the display mode and link; native display programming and GPU acceleration are not provided by this backend.

`DRM_IOCTL_VIRTGPU_EXECBUFFER` supports both
`VIRTGPU_EXECBUF_FENCE_FD_IN` and `VIRTGPU_EXECBUF_FENCE_FD_OUT`.
The input fd gates submission until its sync fence signals;
the output is a close-on-exec, pollable asynchronous fence fd.
Input and output syncobj arrays support binary payloads and timeline points;
input descriptors may request reset after a successful submission.
Alternate rings are not supported.
One command stream is limited to 16 MiB,
and at most 64 MiB of command streams may be retained by concurrent submissions system-wide;
exceeding those limits returns `EINVAL` and `ENOSPC`, respectively.
At most 262,144 GEM-object/fence associations may be retained system-wide;
new submissions return `ENOSPC` after that boundary.
`DRM_IOCTL_VIRTGPU_WAIT` supports blocking waits and `VIRTGPU_WAIT_NOWAIT`; the latter returns `EBUSY` while tracked resource work is pending.
Blocking waits are interruptible and use the same 15-second device deadline as Linux virtio-gpu.
`DRM_IOCTL_VIRTGPU_RESOURCE_CREATE` allocates and returns a new GEM handle when `bo_handle` is zero.
As an Asterinas extension, a nonzero `bo_handle` may name an existing GEM object, allowing a dumb/KMS buffer to become the resource backing without a second allocation.
For formats whose tightly packed layout is known, the GEM backing must cover the complete mip chain.
Direct transfers are rejected when the guest layout cannot be proven.
The supported virtio-gpu query and resource operations are `GETPARAM`,
`RESOURCE_CREATE`, `RESOURCE_INFO`, `TRANSFER_TO_HOST`, `TRANSFER_FROM_HOST`,
`GET_CAPS`, and `MAP`.
Explicit `CONTEXT_INIT` is rejected with `EINVAL` because the corresponding
virtio-gpu feature is not negotiated.

DRM synchronization objects support binary and timeline signaling and waits,
reset, query, transfer, whole-object fd sharing, `sync_file` import and export,
and one-shot eventfd notification.
Wait-for-submit, wait-all, and wait-available behavior is implemented.
The deadline flag is accepted as a compatibility scheduling hint;
it does not currently alter GPU scheduling priority.
Syncobj arrays are limited to 4096 entries.
A timeline retains at most 4096 pending points, and one syncobj accepts at most
4096 eventfd watchers, with 16384 eventfd watchers and 16384 fence callbacks
system-wide.
Array-limit violations return `EINVAL`; capacity and system-limit violations
return `ENOSPC`.

The primary node exposes one CRTC, connector, encoder, and primary plane with
globally unique object IDs.
The primary plane is enumerated after the file enables
`DRM_CLIENT_CAP_UNIVERSAL_PLANES`; atomic ioctls require
`DRM_CLIENT_CAP_ATOMIC` on that file.
Both legacy KMS and the Linux `drm_mode_atomic` object/count/property layout
are supported.
Atomic `TEST_ONLY`, `ALLOW_MODESET`, and `DRM_MODE_PAGE_FLIP_EVENT` requests
are validated.
The primary plane exposes `IN_FENCE_FD`, where `-1` means no dependency,
and the CRTC exposes `OUT_FENCE_PTR`.
The output pointer is initialized to `-1` for test-only or failed commits;
a successful commit returns a sync fence that signals with scanout completion.
Atomic `NONBLOCK` commits exchange their logical KMS and property state before
returning, then apply hardware updates through a bounded per-file FIFO.
Requested flip-completion events reserve queue capacity before the state
exchange and are published after the corresponding hardware update succeeds.
An asynchronous hardware failure is logged and does not publish a false
completion event.
The virtio-gpu transport has no physical-vblank notification,
so the KMS layer uses a shared software refresh clock
derived from the advertised 60 Hz mode.
Legacy and atomic flip events,
their sequence/timestamp fields,
and atomic output fences converge on the next refresh boundary after presentation.
The clock is isolated behind the KMS completion interface,
so a hardware-backed display driver can replace it
with a real vblank interrupt source.
`DRM_IOCTL_WAIT_VBLANK` supports blocking and event requests,
relative and absolute targets, and `NEXTONMISS` semantics.
`DRM_IOCTL_CRTC_GET_SEQUENCE` reports the current 64-bit sequence,
monotonic nanosecond timestamp, and active state;
`DRM_IOCTL_CRTC_QUEUE_SEQUENCE` schedules 64-bit sequence events.
The corresponding monotonic-timestamp,
high-CRTC-index,
and CRTC-in-vblank-event capabilities are advertised.
Legacy `DRM_MODE_PAGE_FLIP_ASYNC` is likewise rejected with `EOPNOTSUPP`.
Property blobs are bounded to 64 KiB, owned by the creating DRM file, and kept
alive while committed KMS state references them.

DRM file descriptions append diagnostic fields to
`/proc/<pid>/fdinfo/<fd>`. `drm-driver` identifies the driver and
`drm-client-id` identifies the open file. Fields beginning with
`drm-device-` are device-wide totals, so every file for the same GPU reports
the same aggregate state rather than per-client usage. A snapshot may be
weakly consistent while other clients are changing resources; leak tests must
quiesce their workload before comparing it with a baseline.

The device fields report the DUMB-pool live-used, high-water, and capacity bytes; GEM objects,
references, and FLINK names; live and cleanup-only host resources; virgl
contexts and attachments; retained fences and per-object fence associations;
backend backing owners; scanout and cursor resources; and pending cleanup at
the DRM, context, and backend layers. A pending-cleanup value means that the
guest has not confirmed host destruction. `drm-device-fences-tracked` includes
completed fences retained as conservative lifetime barriers until the next
prune.
DUMB spans are reused only after GEM, VMA, and host-backing owners all release
them.
The used value is live allocated memory; the high-water value is the monotonic
peak.
Reused pages are cleared before a new handle can expose them.

For more information,
see [the man page](https://man7.org/linux/man-pages/man2/ioctl.2.html).

### `ioprio_set` and `ioprio_get`

Supported functionality in SCML:

```c
{{#include ioprio_get_and_set.scml}}
```

Unsupported selectors:
* `IOPRIO_WHO_PGRP`
* `IOPRIO_WHO_USER`

For more information,
see [the man page](https://man7.org/linux/man-pages/man2/ioprio_set.2.html).
