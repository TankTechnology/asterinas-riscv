# Desktop systemd activation boundary — r85–r89

## Scope

This is a bounded QEMU userspace diagnostic. It did not start Firefox, access
the physical board, write serial input, or touch DRM/GPU hardware. The
original r84 rootfs was never modified.

## Observations

| Run | Change | Result | Useful boundary |
|---|---|---|---|
| r85 | Added `systemd.log_target=console systemd.log_level=debug` before `init=/init` | `passed=false`, 45 s | Stage1 reached `handoff-enter action=exec`; no systemd banner. These parameters are not a valid diagnostic path on this guest and are not used as proof. |
| r86 | Added `systemd.unit=basic.target` plus the same logging parameters | `passed=false`, 45 s | Same pre-banner boundary; not used as proof. |
| r87 | Derived rootfs unit emits `DEBIAN_DESKTOP_M4_UNIT_ACTIVATED` in `ExecStartPre` | `passed=false`, 60 s | Stage1 reached `exec`, but the short bound ended before systemd banner. Marker count: 0. |
| r89 | Same r87 derived rootfs, standard boot arguments, 120 s | `passed=false`, `reason=protocol` | Systemd banner reached at serial lines 277–287, but `DEBIAN_DESKTOP_M4_UNIT_ACTIVATED` and `DEBIAN_DESKTOP_M4_SESSION` are absent. No target/service status follows before QEMU termination. |

The r89 serial tail is:

```text
DEBIAN_STAGE1_PROGRESS step=handoff-enter action=exec
Failed to find module 'autofs4'
Failed to find module 'unix'
systemd 257.13-1~deb13u1 running in system mode ...
Detected virtualization container-other.
Detected architecture riscv64.
Welcome to Debian GNU/Linux 13 (trixie)!
Hostname set to <ubuntu-MS-7D76>.
Initializing machine ID from random generator.
Failed to bump fs.nr_open, ignoring: Operation not supported
Failed to enable kbrequest handling, ignoring: Inappropriate ioctl for device
Failed to open libbpf, cgroup BPF features disabled: Operation not supported
qemu-system-riscv64: terminating on signal 15 from pid 38553 (python3)
```

This closes the diagnostic at systemd target activation. It does not prove a
Firefox, Xorg, DRM, or kernel fault.

## Artifact drift found

The r84 rootfs was built before the current builder topology was applied:

- `/etc/systemd/system/asterinas-desktop-m4-ordered.service` remains as a
  regular file in r84, while the current `build_rootfs.sh` explicitly removes
  this retired M4 entry.
- r84 has `firefox-drm-scene.service` as a regular unit but no corresponding
  `basic.target.wants/firefox-drm-scene.service` symlink, although the current
  `configure_desktop_m4_firefox` creates that link.

These are provenance mismatches, so r84 and its derivatives must not be used
as evidence for the current builder behavior. A clean rootfs rebuild is the
next required input before another desktop runtime attempt.

## Receipts

| Artifact | SHA-256 |
|---|---|
| r85 result | `303671eb0f3b0cce639a0d71d353fbc87c84f6bc80f1da6e9cb80d3ff659ef98` |
| r85 serial | `c15ddce65c2d24e515973f89a7d8f61298e078ed5fdac9656b60a4cf6d1e13bb` |
| r86 result | `90baf70e1d951c0e2cee68bdf545686a8a485541c9cb04596da0a454ef87522d` |
| r86 serial | `3d0c28fd815eefc078c25a338dfc1809c02c7e32c9603635d35ce39f24e0eb8f` |
| r87 result | `6299e9d1da5ef5fe0fa431e10a528a6837dadb971518f4c168ea8213e74fe8df` |
| r87 serial | `43e7cb24f98ebee7732a09b24e0449e379abe3972074e78f1ff28a728727d3de` |
| r89 result | `82512acd2366ab7dc9633a201c95f9ede02601ff99085cd298ed964c77124195` |
| r89 serial | `d74aa3d79108501f6e8929c0f9a3c6f88536b989719c4c244d248fb3893659d3` |

Runtime directories are under `/mnt/mydisk/qemu-desktop-m4-r85-systemd-debug`,
`/mnt/mydisk/qemu-desktop-m4-r86-unit-basic`,
`/mnt/mydisk/qemu-desktop-m4-r87-activation-gate`, and
`/mnt/mydisk/qemu-desktop-m4-r89-activation-120`.

## Next gate

Build one clean `desktop-m4-firefox` rootfs with the current builder, verify
the service/symlink topology against its manifest, and run one bounded
systemd-to-session check. Do not start Firefox until that clean artifact emits
the activation and session markers.
