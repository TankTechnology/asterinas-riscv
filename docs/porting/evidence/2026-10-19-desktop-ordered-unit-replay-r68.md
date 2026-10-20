# Ordered desktop unit replay r68

A disposable rootfs contained a normal M4 unit guarded by `!asterinas.desktop_ordered` and an ordered sibling without PAM/TTY properties. The QEMU boot command contained `asterinas.desktop_ordered=1`; browser/Firefox remained disabled.

The device-access phase reached `fbdev-permissions-done`, but no `ASTERINAS_DESKTOP_M4_SESSION phase=start` appeared before the 60-second bound. The serial stream showed only `Starting asterinas-desktop-m4.service`; no ordered sibling start was reported. This means the current Asterinas systemd path did not select the sibling unit as expected (or does not implement this condition form), so the TTY/PAM fix is not yet runtime-validated.

Artifacts:

- serial: `/mnt/mydisk/qemu-desktop-phase-r68/run/desktop-m4.serial.log` (SHA-256 `07b9a15efcff39effab6cc7ae49588003210bc93db714d1b81c437cdc09cca4a`)
- result: `/mnt/mydisk/qemu-desktop-phase-r68/run/result.json` (SHA-256 `8f36f453c28c8f2838e9291a7aa55b8c29e5f892d56106266dbdcec8a45d29f8`)
- lifecycle: `/mnt/mydisk/qemu-desktop-phase-r68/run/lifecycle.json` (SHA-256 `6ac346bb007832863506af9d30b3c7a7d73cb341b996b05cfff51b167f5dfc31`)
