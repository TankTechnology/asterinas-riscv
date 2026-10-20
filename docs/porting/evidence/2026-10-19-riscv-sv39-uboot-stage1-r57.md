# RISC-V Sv39 U-Boot/Stage1 handoff r57

This is a non-DRM, non-GL, non-Firefox startup validation using the current
Sv39 kernel Image and the fixed generic CPU contract.

Inputs:

- kernel SHA-256: `245d54df230f0d8878411d1efd982d5e4189d469f6172379c6c246f56c9fba1e`
- DTB SHA-256: `d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c2`
- Stage1 SHA-256: `ce7fb996e9a6ac48160d81c29347a68d88e99204adcc147051df76f645031c84`
- U-Boot SHA-256: `47516422aea981a45eb3da6de786d356dbd9059c96bd9fc11d8e3ba28aec7210`
- QEMU CPU: `rv64,svpbmt=true,zkr=true`

## Handoff result

The serial receipt contains, in order:

- all U-Boot load and `booti` markers;
- `Starting kernel ...`;
- `ASTERINAS_SOFTWARE_REBOOT_ARMED`;
- all Asterinas first-kthread and first-process markers;
- Stage1 root selection and block probe;
- root mount, `/dev`, `/proc`, `/sys`, `/run`, `/tmp`, chroot, and exec handoff markers.

The gate later stopped at the desktop readiness protocol boundary. That does
not invalidate the kernel/Stage1 handoff assertion; Firefox was not started.

Artifacts:

- `/mnt/mydisk/qemu-startup-fbdev-r57/desktop-m4.serial.log`
- `/mnt/mydisk/qemu-startup-fbdev-r57/result.json`
- `/mnt/mydisk/qemu-startup-fbdev-r57/lifecycle.json`
