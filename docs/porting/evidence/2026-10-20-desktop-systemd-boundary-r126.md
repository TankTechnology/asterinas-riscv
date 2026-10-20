# desktop-systemd-boundary r126

## Scope

One bounded QEMU probe (120 seconds) targeted the systemd PID 1 boundary. The
boot used the current Firefox-profile rootfs with qcow2 backing, but masked the
M4 desktop and Firefox services and selected `systemd.unit=basic.target`.
No Xorg, Firefox, DRM userspace probe, physical board, serial input, or hardware
access was used.

A first adapter attempt stopped at the host virgl preflight because the
container QEMU lacks `egl-headless`; it created no guest process. The measured
probe below used the ordinary QEMU framebuffer adapter so the systemd boundary
could be isolated from the GL host capability.

## Marker table

| Boundary | Observed | Evidence |
|---|---:|---|
| U-Boot prompt and boot command | yes | QEMU serial log |
| `Starting kernel ...` | yes | QEMU serial log |
| `ASTERINAS_BOOT_PHASE phase=first-kthread-enter` | yes | serial line 198 |
| `ASTERINAS_BOOT_PHASE phase=kthread-init-done` | yes | serial line 202 |
| `ASTERINAS_BOOT_PHASE phase=banner-begin` | yes | serial line 203 |
| `ASTERINAS_BOOT_PHASE phase=banner-done` | yes | serial line 230 |
| `ASTERINAS_BOOT_PHASE phase=init-spawned` | yes | serial line 231 |
| Stage1 `step=start mode=systemd` | no | absent from serial log |
| Stage1 root handoff complete | no | absent from serial log |
| systemd banner / `Reached target basic.target` | no | absent from serial log |
| Xorg marker | no | service was masked and no marker appeared |
| Firefox/NewSession marker | no | service was masked and no marker appeared |

The gate ended with `reason=protocol` at the 120-second boundary. The last
observed kernel marker was `phase=init-spawned`; this run does not identify a
systemd unit because systemd had not started yet.

## Inputs and artifacts

- kernel: `54dd701c273b296c783269973bb1328abcecc6c0a0aea2a3c14bd13e8a6badad`
- U-Boot: `47516422aea981a45eb3da6de786d356dbd9059c96bd9fc11d8e3ba28aec7210`
- DTB: `d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c2`
- Stage1: `ce7fb996e9a6ac48160d81c29347a68d88e99204adcc147051df76f645031c84`
- root image: `5d7e10e83e14b617505c091cac7ca320d08154d60ba0e6299deed6406a84bffc`
- lifecycle: `/mnt/mydisk/qemu-desktop-systemd-boundary-r126/lifecycle.json`
  SHA-256 `d1cf76b7f9b213520524a35fd2d02d5a217130480bd7ebb8e1300e7296eb9f6e`
- result: `/mnt/mydisk/qemu-desktop-systemd-boundary-r126/result.json`
  SHA-256 `d59b463488e6182a4bd9661a2581d2085b42aa41039a311e391ed9d7808ce2c2`
- serial: `/mnt/mydisk/qemu-desktop-systemd-boundary-r126/desktop-systemd-boundary.serial.log`
  SHA-256 `2f5fa2e1a4c5be360da79109cd459ca46ac4d6713dafe227b8bfc83c78d218d4`

## Conclusion

The next boundary is the Stage1 rootfs handoff and the first systemd exec,
not Firefox capability negotiation. Repeating a systemd unit probe without
shortening or instrumenting that handoff would reproduce the same pre-PID1
boundary.
