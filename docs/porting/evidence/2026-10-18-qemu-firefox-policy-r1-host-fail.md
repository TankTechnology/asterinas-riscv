# E3 official Firefox policy-r1 host preflight (2026-10-18)

## Input set

This attempt used a new content-addressed rootfs overlay, so it is distinct
from the blocked r44/r47 guest log boundary:

- overlay specification: `target/e3-render-policy-r1/overlay.json`
- derived rootfs: `target/e3-render-policy-r1/rootfs/debian-root.ext2`
- derived rootfs SHA-256: `34cd31b3f990ebca7c649dc2bfc1aa3752b4b8be0c10ec5fd4fe05b6c4b8ed13`
- derived rootfs manifest SHA-256: `b2fb157199ecb529942b0e8be5d7543183a07743963a9f4360d561710eef080f`
- DTB: `/mnt/mydisk/firefox-official-debian-20261018/qemu-virt-r5.dtb`
  (SHA-256 `1bcec907d28a9ede2c0020d9024de93640d2d0ba9bdfc5f09f20e81f4777a9eb`)
- QEMU GL binary: `/mnt/mydisk/qemu-build-gl2/qemu-system-riscv64`

The overlay was materialized without unpacking or modifying the retained base
image. `/mnt/shared` remained above the 60 GiB safety floor after materializing
it.

## Result

The gate reached its QEMU launch path but the host EGL backend failed before a
guest process could start. A direct replay of the exact QEMU argument contract
reported:

```
amdgpu: os_same_file_description couldn't determine if two DRM fds reference the same file description.
qemu-system-riscv64: egl: eglGetDisplay failed: EGL_SUCCESS
qemu-system-riscv64: egl: render node init failed
```

The container initially lacked `/dev/dri`; after mapping it, the same EGL
failure remained. The gate result was therefore `passed=false`, `reason=launch`,
with an empty serial log and no Firefox markers. This is a host-display
preflight failure, not a Firefox or guest assertion, and must not count as a
third E3 guest rerun.

## Required external condition

Run the same input set in a host/container with a working EGL GBM device
provider (or explicitly provide compatible NVIDIA/AMD EGL vendor libraries and
DRM render-node access). Only then can the single bounded E3 guest run resume.
