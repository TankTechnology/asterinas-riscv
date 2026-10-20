# Desktop service TTY boundary r66

This bounded QEMU run used a disposable rootfs copy containing only phase markers in the desktop device-access and session scripts. Browser/Firefox was disabled.

The serial order is decisive:

```text
ASTERINAS_DESKTOP_DEVICE_ACCESS phase=start
ASTERINAS_DESKTOP_DEVICE_ACCESS phase=provider-fbdev
ASTERINAS_DESKTOP_DEVICE_ACCESS phase=input-ordered
ASTERINAS_DESKTOP_DEVICE_ACCESS phase=input-stable-before
ASTERINAS_DESKTOP_DEVICE_ACCESS phase=input-stable-done
ASTERINAS_DESKTOP_DEVICE_ACCESS phase=done
ASTERINAS_DESKTOP_DEVICE_ACCESS phase=fbdev-permissions-before
ASTERINAS_DESKTOP_DEVICE_ACCESS phase=fbdev-permissions-done
```

There is no `ASTERINAS_DESKTOP_M4_SESSION phase=start` after `ExecStartPre` completes. Therefore the hang is between the device-access `ExecStartPre` and execution of `desktop-m4-session`, in systemd service setup (the unit uses `PAMName=login`, `TTYPath=/dev/tty1`, and `StandardInput=tty`). It is not input symlink creation, framebuffer permission setup, Xorg, Netsurf, or Firefox.

The run ended at the explicit 60-second host bound and produced no desktop ready marker.

Artifacts:

- serial: `/mnt/mydisk/qemu-desktop-phase-r66/run/desktop-m4.serial.log` (SHA-256 `3890b90e12e1745123d65b2b685eed5ec8ab6cd48f0817162098e9f69db7918b`)
- result: `/mnt/mydisk/qemu-desktop-phase-r66/run/result.json` (SHA-256 `16e33db728263321a51358d24984e2067a37eda0f7f05e6f30e24649463545f5`)
- lifecycle: `/mnt/mydisk/qemu-desktop-phase-r66/run/lifecycle.json` (SHA-256 `7b8aa1aa7545ad62a96746e5f80f18f7155d4cd558da48fef5eb01a307dfff3d`)
- disposable rootfs: `/mnt/mydisk/qemu-desktop-phase-r66/debian-root.ext2`
