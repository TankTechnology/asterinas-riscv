# Firefox Marionette short probe r7 launch boundary

r7 used the child-process instrumentation from the host-side short probe. The launcher terminated during the QEMU `launch` phase before a serial log or guest result was published.

## Evidence

- `lifecycle.json`: `phase=launch`, `status=running`, updated `2026-10-18T19:09:52+00:00`.
- No `desktop-m4-drm.serial.log` was present.
- No `result.json` was present.
- No Firefox greeting or NewSession marker was observed.
- The container had no live `qemu-system-riscv64` process when inspected after termination.

This is a launcher/incomplete-run record, not an E3 Firefox assertion and not evidence about the guest renderer.

## Input

- rootfs: `/mnt/mydisk/asterinas-firefox-marionette-probe-rootfs-20261019-r7/debian-root.ext2`
- rootfs sha256: `4b88a7a0518dda63d6927cd6dce245c7044cad1c7e8e1a6929be100388579dab`

The valid Firefox runtime boundary remains r6; no retry is justified until the host launcher lifecycle is repaired or replaced.
