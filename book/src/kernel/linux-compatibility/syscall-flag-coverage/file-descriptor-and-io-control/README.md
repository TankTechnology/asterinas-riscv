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

Silently-ignored flags:
* `EFD_NONBLOCK`
* `EFD_SEMAPHORE`

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

`DRM_IOCTL_VIRTGPU_EXECBUFFER` supports `VIRTGPU_EXECBUF_FENCE_FD_OUT` and returns a pollable asynchronous fence fd.
Syncobj arrays, alternate rings, and input fence fds are not supported.
`DRM_IOCTL_VIRTGPU_WAIT` supports blocking waits and `VIRTGPU_WAIT_NOWAIT`; the latter returns `EBUSY` while tracked resource work is pending.

The primary node exposes one CRTC, connector, encoder, and primary plane with
globally unique object IDs.
The primary plane is enumerated after the file enables
`DRM_CLIENT_CAP_UNIVERSAL_PLANES`; atomic ioctls require
`DRM_CLIENT_CAP_ATOMIC` on that file.
Both legacy KMS and the Linux `drm_mode_atomic` object/count/property layout
are supported.
Atomic `TEST_ONLY`, `ALLOW_MODESET`, and `DRM_MODE_PAGE_FLIP_EVENT` requests
are validated; atomic `NONBLOCK` is rejected with `EOPNOTSUPP` because commits
are currently synchronous.
Flip-completion events are queued after synchronous presentation, not from a
hardware-vblank interrupt.
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

The device fields report the DUMB-pool used and capacity in bytes; GEM objects,
references, and FLINK names; live and cleanup-only host resources; virgl
contexts and attachments; retained fences and per-object fence associations;
backend backing owners; scanout and cursor resources; and pending cleanup at
the DRM, context, and backend layers. A pending-cleanup value means that the
guest has not confirmed host destruction. `drm-device-fences-tracked` includes
completed fences retained as conservative lifetime barriers until the next
prune. The DUMB-pool used value is a monotonic allocation watermark, not live
GEM memory: the current allocator does not reuse a span because an established
mapping can outlive its GEM handle.

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
