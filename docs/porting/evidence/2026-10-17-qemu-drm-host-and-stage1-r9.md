# QEMU DRM recovery replay: host GL backend and stage1 pairing

Date: 2026-10-17 (UTC)

This replay stayed in QEMU and did not touch the Megrez board. The source
worktree was left dirty; no kernel, rootfs, or manifest was rewritten.

## Inputs

- Kernel: `target/osdk/aster-kernel-osdk-bin.Image`
- U-Boot: `target/qemu-uboot/cache/u-boot-build/u-boot`
- Rootfs profile: `target/debian-riscv/desktop-m4-x11/rootfs`
- QEMU: `/root/qemu-gl-src/build/qemu-system-riscv64` from
  `asterinas/qemu-gl-env:20261014`, QEMU 10.2.1
- Generated DTB: `/mnt/mydisk/asterinas-gates/qemu-virt-4cpu-sv39-20261017.dtb`
  (four enabled CPU nodes, all `mmu-type = "riscv,sv39"`)

## Gate results

| replay | result | evidence | precise boundary |
| --- | --- | --- | --- |
| r6 | fail-closed | `/mnt/mydisk/asterinas-gates/qemu-drm-recovery-20261017-r6/result.json` | `reason=launch`; the default QEMU had no `egl-headless` or `virtio-gpu-gl-device` |
| r7 | fail-closed | `/mnt/mydisk/asterinas-gates/qemu-drm-recovery-20261017-r7/result.json` and `desktop-m4-drm-recovery.serial.log` | `reason=protocol`; the default DTB had `sv57`, so the kernel stopped after `Enter riscv_boot` |
| r8 | fail-closed | `/mnt/mydisk/asterinas-gates/qemu-drm-recovery-20261017-r8/result.json` and `desktop-m4-drm-recovery.serial.log` | `reason=protocol`; `DEBIAN_ROOTFS_FAIL reason=root-init-argument` from the older stage1 artifact |
| r9 | fail-closed | `/mnt/mydisk/asterinas-gates/qemu-drm-recovery-20261017-r9/result.json` and `desktop-m4-drm-recovery.serial.log` | `reason=protocol`; stage1 handoff reached `step=handoff-enter action=exec`, but the desktop marker did not arrive before the 180-second recovery boundary |

The r9 serial receipt proves the corrected stage1 and DTB pairing reaches:

```
DEBIAN_STAGE1_PROGRESS step=handoff-done action=chdir
DEBIAN_STAGE1_PROGRESS step=handoff-enter action=exec
```

It then reaches the Debian systemd banner, but not
`DEBIAN_DESKTOP_M4_READY` or the strict `renderer=virgl` pixel marker. This
does not count as a DRM or Firefox acceleration pass. The likely remaining
runtime issue is the selected `desktop-m4-x11` image's desktop service/target
activation; it must be isolated before any physical display run.

## Host backend finding

The wrapper QEMU in the ordinary cross image reports only `none` and `dbus`
display backends and rejects `virtio-gpu-gl-device`. The GL build reports
`egl-headless` and the GL device, and therefore is the only valid backend for
the DRM gate. The gate should preflight this capability before copying a
rootfs, so a missing host backend fails immediately and explicitly.

## Physical boundary

The board was not modified. The read-only RockOS receipt remains
`/mnt/mydisk/board-readonly-20261017/ssh-readonly.txt`; it shows UID 1000,
the existing boot-id, `/dev/fb0`, `/dev/dri/card0`, and `end1` up. No physical
DRM/fb0 or GPU experiment is authorized by this replay because the QEMU
`DEBIAN_DESKTOP_M4_READY` + strict virgl pixel gate is still open.

