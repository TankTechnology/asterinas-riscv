# PRIME / dma-buf on the shared DRM core

This is milestone P2 of
[`docs/porting/drm-shared-core-design.md`](../../drm-shared-core-design.md), and
the continuation of the two P1 halves
([x86](../2026-10-21-x86-virtio-gpu/README.md),
[RISC-V](../2026-10-21-riscv-virtio-gpu/README.md)).

PRIME is how one process hands a buffer to another: a GEM handle is exported as
a dma-buf file descriptor, the descriptor travels over a unix socket with
`SCM_RIGHTS`, and the receiver imports it into its own DRM file. Every
compositor, DRI3 client and Wayland buffer path depends on this pair of ioctls.

## What was added

### Kernel

- `kernel/comps/drm/src/prime.rs` — `DmaBufFile`, the per-open object behind a
  dma-buf descriptor. It holds the `DrmGemObject` (so the buffer outlives the
  exporting handle) and the exporting `DrmMinor` (so import can reject a
  descriptor from a different device). It is mappable: a shared mapping walks
  the GEM backend's frames, with the file offset taken as a byte offset into the
  buffer.
- `DRM_IOCTL_PRIME_HANDLE_TO_FD` (0x2d) and `DRM_IOCTL_PRIME_FD_TO_HANDLE`
  (0x2e) in `kernel/comps/drm/src/ioctl/`, allowed on render nodes and gated on
  GEM support. Export honors `DRM_CLOEXEC`; import verifies that the descriptor
  belongs to the same DRM device.
- `DRM_CAP_PRIME` now reports `DRM_PRIME_CAP_IMPORT | DRM_PRIME_CAP_EXPORT`
  (0x3) instead of the previous placeholder 0.
- `kernel/core/src/fs/file/anon.rs` — `create_anon_file` and
  `with_anon_file_as`, the `anon_inode_getfile` analogue for components outside
  `aster-core`. The DRM core is the first user; the helper is deliberately
  generic because io_uring/userfaultfd-style files will need it too.
- `kernel/core/src/fs/vfs/path/dentry.rs` — pseudo dentries may now carry a
  fixed name (`PseudoName::Static`) in addition to a name function, so the
  anonymous file shows up as `anon_inode:[drm-buf]` in `/proc/<pid>/fd`.

The live tree's `SCM_RIGHTS` path already accepts any `FileLike`, so no
"proven leaf" audit was needed on this branch. That audit existed in the
upstream-layout tree because its `DmaBufFile` was a bare `FileLike` that could
not retain a file description; here the descriptor is an ordinary inode handle
whose per-open object is `DmaBufFile`.

## Acceptance

`tools/drm/prime_probe.c` is an architecture-neutral static client that runs as
a one-file initramfs `/init`. It:

1. reads `DRM_CAP_PRIME` and requires `0x3`;
2. creates a 64x64 dumb buffer, maps it through `MODE_MAP_DUMB`, and writes a
   page pattern;
3. exports the handle with `PRIME_HANDLE_TO_FD`, maps the descriptor, and
   compares the bytes;
4. imports the descriptor into a second open of the same device
   (`PRIME_FD_TO_HANDLE`), maps the imported handle, and compares again;
5. closes the exporting file and passes the descriptor to a forked child over
   `SCM_RIGHTS`; the child imports, maps and compares;
6. requires `EINVAL` for a pipe descriptor and `EBADF` for a closed one.

Both architectures pass, with the same probe source and the same kernel code
(`tools/drm/prime_check.sh`):

```
$ tools/drm/prime_check.sh --arch amd64 --kernel target/prime/kernels/x86.elf
PRIMEPROBE_CAP prime=0x3
PRIMEPROBE_DUMB handle=1 pitch=256 size=16384
PRIMEPROBE_EXPORT fd=4
PRIMEPROBE_MMAP_SHARED ok
PRIMEPROBE_IMPORT handle=1
PRIMEPROBE_IMPORT_MMAP ok
PRIMEPROBE_CHILD_PASS
PRIMEPROBE_SCM_RIGHTS ok
PRIMEPROBE_REJECT_NON_DMABUF ok
PRIMEPROBE_REJECT_CLOSED_FD ok
PRIMEPROBE_PASS
PASS (amd64 PRIME: export, shared mmap, import, SCM_RIGHTS, negative cases)

$ tools/drm/prime_check.sh --arch riscv64 --kernel target/prime/kernels/riscv.Image
... identical transcript ...
PASS (riscv64 PRIME: export, shared mmap, import, SCM_RIGHTS, negative cases)
```

`make test_x86_prime` and `make test_riscv_prime` run the gates; the kernel
artifact paths are explicit because the x86 ELF and the RISC-V `Image` share
`target/osdk` and clobber each other.

Kernel builds used for the run:

```sh
# x86_64
cargo osdk build --target-arch x86_64 --boot-method vmm-direct
# RISC-V (Sv39)
make kernel TARGET_ARCH=riscv64 FEATURES=riscv_sv39_mode
```

## Notes and limits

- Import is same-device only. Cross-device import needs a real dma-buf
  attachment layer (exporters, `dma_buf_attach`, move notifications); the probe
  and the import check make that explicit.
- The descriptor reports a zero size through `st_size`; the anon inode is
  shared, so the per-object size is not reflected in inode metadata. Consumers
  that import the descriptor rather than `fstat` it are unaffected.
- The next consumer is X DRI3: the server receives the DRM fd and a dma-buf fd
  from its client and imports both. The parent-process and `SCM_RIGHTS` paths
  the probe exercises are exactly what that handshake needs. On the RISC-V
  desktop image the server currently advertises `DRI2` and `GLX` but not
  `DRI3`/`PRESENT` (`xdpyinfo -queryExtensions`), and `glxinfo -B` reports
  llvmpipe with direct rendering. Enabling DRI3 is the remaining work for this
  milestone, alongside P3's atomic/syncobj surface — the fd-passing primitives
  it needs are in place.
