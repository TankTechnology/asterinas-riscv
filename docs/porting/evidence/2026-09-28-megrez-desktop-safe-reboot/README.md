# Megrez desktop safe-reboot handoff

The 2026-09-28 selected physical boot verified that a ready desktop can retain
Firefox beyond the former 180-second userspace recovery deadline.
This is a boot and internal display-path result; physical HDMI pixels were not
captured.

## Failure and change

The previous quiet Asterinas boot reached `ASTERINAS_DESKTOP_BOOT_READY`,
disarmed the kernel watchdog, and subsequently returned to U-Boot near the
userspace recovery deadline.
The rootfs safe-reboot service had an independent timer and did not observe the
kernel watchdog handoff.
The desktop readiness service now atomically publishes the current boot ID in
`/run/asterinas-desktop-ready` after confirming that the kernel watchdog reads
back as zero.
The safe-reboot service exits successfully only when the marker is root-owned,
matches the current boot ID, and the kernel watchdog is zero.
If any check fails, its bounded quiesce, sync, and reboot path remains armed.

For this selected boot, Stage1 carried the updated helper and installed a
runtime drop-in that overrides the existing rootfs service's `ExecStart`.
The ext2 rootfs image and RockOS default boot entry were not replaced.

## Selected boot and observations

- Serial device: `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`.
  The host used a single serial owner; commands used fresh nonce-framed
  responses, including after closing and reopening the connection.
- RockOS before the test: `rockos-eswin`, Linux `6.6.87`, boot ID
  `6ffd3f97-9d3c-416d-a26d-1f983b8a40fe`, root `/dev/mmcblk1p3`.
  `/dev/mmcblk1p2` was unmounted.
- Selected one-shot U-Boot artifacts on RockOS partition 3: kernel SHA-256
  `8ba4b6cb9819a70671268065209f8b2400f67ffc0dbd59d62ad9d7fc584920ba`,
  Stage1 SHA-256
  `666d58af0673e2af52c9f1f36b5438095c759d95b3efcd800180054606bb3d71`,
  and DTB SHA-256
  `465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba`.
  U-Boot verified each loaded artifact's CRC and size before `booti`.
- Selected arguments included `console=tty0 loglevel=off`,
  `asterinas.reboot_after=300`, and
  `systemd.setenv=ASTERINAS_SAFE_REBOOT_AFTER=180`.
  The opt-in isolated root debug console remained enabled.
- Asterinas boot ID was `289593cb-e34a-49dc-aad2-8e83c3ed3ee0`.
  A fresh serial response proved UID 0 and that boot ID.
  The boot runner reached the root console in 69.059 host seconds.
- At guest uptime 54.605 seconds, `dmesg` reported
  `ASTERINAS_DESKTOP_BOOT_READY firefox_pid=250` and
  `ASTERINAS_DESKTOP_WATCHDOG_DISARMED`.
  The safe-reboot unit used
  `/run/asterinas-tools/megrez-safe-reboot` and ended with
  `SubState=exited`, `MainPID=0`, and `ExecMainStatus=0`.
  Its marker contained the current boot ID and the kernel watchdog read zero.
- At guest uptime 214.72, 316.55, and 409.79 seconds, newly reopened root
  serial connections still reported the same boot ID.
  Firefox and the desktop readiness service remained active after the 180- and
  300-second deadlines.
  A short display preflight found `/dev/fb0`, Xorg with the fbdev device open,
  Openbox, one visible Firefox window, and USB keyboard/mouse input nodes.
  Firefox PID 250 had no service restarts.

The host retained the boot transcript, result JSON, and separate nonce-framed
serial command logs under `.local-test/desktop-goal-20260927/` in the
`asterinas-main-publish` checkout.
Neither `/dev/dri` nodes nor Xorg's fbdev path prove GPU rendering or HDMI
scanout.
