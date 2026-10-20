# QEMU DRM/virgl runtime replay: r10–r13

Date: 2026-10-17 (Asia/Shanghai)

These are bounded QEMU-only runs. The physical board was not accessed or
modified. Inputs were kept immutable; each run used a fresh output directory.

## Host and launch boundary

The GL build inside `asterinas/qemu-gl-env:20261014` is
`/root/qemu-gl-src/build/qemu-system-riscv64` (QEMU 10.2.1). It reports both
`egl-headless` and `virtio-gpu-gl-device`. The desktop DRM launch hook from
r10 ran before `launch_process()` and selected this executable through PATH.

| run | host setup | result | boundary |
| --- | --- | --- | --- |
| r10 | GL QEMU, no `/dev/dri` device mapping | `passed=false`, `reason=launch` | QEMU printed `egl: no drm render node available`; serial was empty |
| r11 | GL QEMU + `/dev/dri/renderD128` mapping | `passed=false`, `reason=protocol` | EGL initialized; guest reached Debian systemd, but first-boot services prevented desktop marker |
| r12 | diagnostic masks appended after stage1 `--` delimiter | `passed=false`, `reason=protocol` | stage1 correctly rejected `DEBIAN_ROOTFS_FAIL reason=root-init-argument`; diagnostic was invalid by construction |
| r13 | diagnostic masks placed before stage1 `--` delimiter | `passed=false`, `reason=protocol` | guest entered systemd, but `systemd-sysctl.service` still ran and remained pending; no Xorg/virgl marker |

## r11 runtime evidence

The serial receipt is
`/mnt/mydisk/asterinas-gates/qemu-drm-recovery-20261017-r11/desktop-m4-drm-recovery.serial.log`.
It contains the complete root handoff through:

```text
DEBIAN_STAGE1_PROGRESS step=handoff-enter action=exec
```

and then:

```text
[FAILED] Failed to start systemd-sysctl.service - Apply Kernel Variables.
[FAILED] Failed to start systemd-sysusers.service - Create System Users.
Job ldconfig.service/start running ...
```

The process was terminated only at the configured 180-second recovery
boundary. No `DEBIAN_DESKTOP_M4_XORG`, `renderer=virgl`, pixel probe, or
desktop-ready marker appeared.

## r12/r13 diagnostic result

r12 proves the stage1 delimiter is enforced; it is not a desktop result. In
r13 the masks were syntactically in the kernel command line, but the guest
still emitted `Starting systemd-sysctl.service`, then remained in its start
job. This shows that adding userspace masks is not a reliable workaround for
the current Asterinas systemd boot path and must not be used as the normal
desktop gate.

## Current conclusion

The GL host path is now verified through EGL initialization and the guest
reaches the Debian root handoff. Task 5 is still **not passed**: there is no
runtime evidence that Xorg selected virgl, no pixel hash, and no recovery
receipt after a desktop marker. The next bounded investigation is the
kernel/userspace ABI behind `systemd-sysctl` (and the subsequent sysusers/
ldconfig jobs), using a diagnostic QEMU root shell or service-level probe;
physical fb0/DRM remains outside this gate.

