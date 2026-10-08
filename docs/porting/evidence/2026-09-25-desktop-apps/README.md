# Megrez Debian desktop application smoke test

Date: 2026-09-25.

The `browser-web` Debian root now includes Mousepad, Ristretto, Atril,
Xarchiver, ZIP tools, and `dbus-x11`.
The test used the one-time Megrez U-Boot boot path with the existing
`asterinas-6694c4c7ff5a.booti` kernel (CRC32 `ab15b580`),
`stage1-ea446515661f.cpio` (CRC32 `64e4ff7c`), and
`megrez-465cb129333c.dtb` (CRC32 `40ed4c65`).
The normal RockOS menu entry remained available.
The sole host serial owner used
`/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`.
Both Asterinas entries used `--root-init=systemd
--debug-console=isolated-root` with persistent home and framebuffer output;
the first used userspace/kernel reboot deadlines of 150/330 seconds and the
second used 180/360 seconds.
The final signed root image SHA-256 is
`c9716e8a6f40ca9492df05a745057203bc869e18c4e7b61899c58e2f4b41a8e8`.

Before changing partition 2, RockOS copied the complete 4-GiB partition to
`/home/debian/asterinas/backups/p2-before-daily-apps-20260925.img`.
The backup and source block device had the same SHA-256,
`313775d473eaa7924cc65bbe2baa1ca406b0c5f263d7e5a291bf1703fe031342`.
The Firefox profile, Downloads, and `.local` data were separately archived;
that archive's SHA-256 was
`05e8130946fadfb047901ebf0e18dc8deeba06c591d3292c7495650c3ad8763d`.
Each installed 2-GiB root image was compared byte for byte with partition 2
before restoring this user data.
`e2fsck -fn` passed after restoration.

| Application | Physical operation | Result |
| --- | --- | --- |
| Mousepad | Type through X11 and save a text file | Saved bytes matched `Mousepad physical save proof 91d7f2` |
| Ristretto | Open the anime wallpaper PNG | Image rendered in a visible window |
| Atril | Open a one-page PDF | Page rendered with text; direct launch worked after adding `dbus-x11` |
| Xarchiver | Open a ZIP containing the text fixture | Archive window listed the correct member; `unzip -t` passed |

The [uncovered physical desktop](megrez-uncovered-desktop.png) confirms that
the anime wallpaper fills the current 1920×1080 scanout behind the launchers
and bottom panel.
The [final PDF screenshot](megrez-final-pdf.png) shows Atril rendering the
fixture in the final image with Firefox open behind it.
The [four-application screenshot](megrez-four-apps.png) shows the saved
Mousepad text, rendered PDF, archive listing, and Ristretto image on the
1920×1080 physical framebuffer.
An [earlier image/archive screenshot](megrez-image-and-archive.png) shows the
same root before the PDF viewer was raised.
The [QEMU desktop screenshot](qemu-wallpaper-and-shell.png) shows the wallpaper,
three original launchers, Firefox task button, and bottom panel at 1280×1024.
It is a pixel-identical PNG conversion of the
[final run's minimized PPM](qemu-final-minimized.ppm), whose SHA-256 is
`72cd17c7568532893f03c9973b18f8514ef33ccbf589edaf1d51a755d7742ee2`
and matches the `minimized` entry in the adjacent result JSON.
The [final-image QEMU result](qemu-desktop-shell-result.json) records all six
captured desktop states and a 67.304-second elapsed time.
The rootfs verifier accepted both this image's manifest and an earlier signed
schema-7 `browser-web` image without the seven new application gate packages.

The first physical image lacked `dbus-launch`:
Ristretto warned about its missing D-Bus session, and Atril exited without a
window when launched directly.
Launching Atril under `dbus-run-session` rendered the PDF, which isolated the
missing session-bus path.
The second image installed `dbus-x11`; direct UID-1000 Atril launch then
remained running and produced a visible PDF window.
The final image repeated that direct-launch result and captured its rendered
page at physical resolution.

The isolated root debug console boot uses
`asterinas-debug-console.target` as the default target.
Neither `basic.target` nor `multi-user.target` becomes active in that mode.
The recovery service was therefore inactive when it was enabled only from
those standard targets.
Manually starting its existing unit with a deadline 20 seconds ahead made the
service active and produced a new OpenSBI cycle at the deadline.
RockOS then returned with partition 2 unmounted and a fresh root-console boot
ID of `30d1b590-2116-4a22-ab4d-aad2f0230a87`.
The source now also enables the recovery unit from the isolated target;
the final image verified the change in two separate physical boots.
In the first, the default target was `asterinas-debug-console.target`, the
recovery service was `active` with MainPID 17 at uptime 48.81 seconds, and
the desktop service was also `active`.
Fresh nonce-framed serial commands checked `id -u`,
`cat /proc/sys/kernel/random/boot_id`, and
`systemctl is-active asterinas-safe-reboot.service` in Asterinas.
The Asterinas boot ID was `803c9853-57ca-482b-8e06-498de12b6a0f`.
An OpenSBI reboot epoch arrived at the 150-second userspace deadline,
well before the 330-second kernel fallback.
After RockOS boot, a closed-and-reopened serial connection returned UID 0 and
boot ID `fc673806-fa61-45c3-a4a5-ab673feaf8ca` with partition 2 unmounted.
The second boot used 180- and 360-second deadlines, captured the uncovered
wallpaper and final PDF window, then rebooted into RockOS.
Its Asterinas and recovered RockOS boot IDs were
`e228bac0-c4ab-4995-8d11-ab45721637ed` and
`f08df4b6-5853-46de-9562-5b81d9d9f595`.
The final closed-and-reopened RockOS serial check repeated the nonce-framed
`id -u` and boot-ID commands, and `findmnt /dev/mmcblk1p2` confirmed that
partition 2 was unmounted.
The final RockOS `e2fsck -fn` returned 0.
The RockOS clock lagged the image timestamp by about seven days, so its
read-only filesystem check reported future timestamps but no structural
errors.

The physical display remains at the firmware-provided 1920×1080 mode,
although the connected monitor supports 2560×1600.
The minimal image also lacks the `xdg-open` and `gio` command-line helpers;
file-manager double-click associations were not exercised in this run.
This test did not establish a performance gain, native-resolution output,
GPU acceleration, video playback, or support for larger applications such as
LibreOffice.
