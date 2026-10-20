# QEMU virgl fault matrix and recovery

Date: 2026-10-16 (Asia/Shanghai)

This is a QEMU-only guest-driver receipt. It does not exercise the physical
board or an EIC7700 GPU. The host is QEMU 10.2.1 with the GL backend selected
by the registered `drm-virgl` device set. Faults are opt-in guest cmdline
injections; the QEMU device model itself is not being represented as having a
fault property.

## Contract

Each leg uses one registered profile, `virtio-gpu-gl-device` with
`-display egl-headless`, and `asterinas.reboot_after=3`. The driver emits one
diagnosis and one stable `DRM_VIRGL_FAULT` marker, then the watchdog-style
software reboot returns to OpenSBI/U-Boot. A successful virgl marker is
forbidden in a fault leg.

| Leg | Profile | Required marker | Result |
| --- | --- | --- | --- |
| context creation | `generic-sv39-drm-virgl-fault-context-smp4` | `DRM_VIRGL_FAIL stage=context-init errno=5` and `DRM_VIRGL_FAULT stage=context-init` | **PASS; recovery complete** |
| ordinary submit | `generic-sv39-drm-virgl-fault-submit-smp4` | `DRM_VIRGL_FAIL stage=submit errno=5` and `DRM_VIRGL_FAULT stage=submit` | **PASS; recovery complete** |
| out-fence request | `generic-sv39-drm-virgl-fault-fence-smp4` | `DRM_VIRGL_FAIL stage=fence errno=5` and `DRM_VIRGL_FAULT stage=fence` | **PASS; recovery complete** |

The submit leg now uses a one-byte NOP after the 992/993-byte boundary probes,
so the injection is isolated to the ordinary submission path. The classifier
accepts both LF and QEMU's CRLF serial framing while still requiring exactly
one complete marker line.

## Reproducible receipts

The retained files are under
`target/qemu-uboot/drm-virgl-fault-matrix-20261016/`:

| File | SHA-256 |
| --- | --- |
| `context.result.json` | `fd707c855981e7d48463405a12bfa496b91af306f5659d2c5d643bde68882f35` |
| `context.serial.log` | `c0fa4539a89d029c4306d36f1a6d8d93fccb04840fae7bd11154e573ee7d0c18` |
| `submit.result.json` | `45ae8be67faeaeef701a9d084f90cf9d6a98bec0ba729352f11321e70a43faf5` |
| `submit.serial.log` | `393e47a007a65bd09849ea39a53187234f20eab4331d359c706d8ab7a6eeb7b1` |
| `fence.result.json` | `092c88fd529e6211849ac5e2c3f26c0ea5bc360c966a66193f08c19996484b6d` |
| `fence.serial.log` | `3b18606a5c911c59a76585e5d9e92adb3f83ae9df44aabc9a554b6320c70f49a` |

The kernel/initrd manifest used by all three legs is retained alongside the
receipts. The kernel SHA-256 is
`b4888f248b8e8df94d05e5f02cf284b9189792ecb9129526a99f1c27bcd9e68a`; the
fault-gate initrd SHA-256 is
`337c660eab77e69fc72a3e9eedd3af229a7903ef52da52162e848421ca04c1f8`.

## Scope boundary

This closes the virgl context/submit/fence failure and recovery gate for the
QEMU guest path. It does not close Firefox WebRender, physical-board GPU
acceleration, or the remaining 2D scanout fault cases (SETCRTC, transfer and
flush), which remain separate Task 5 work.
