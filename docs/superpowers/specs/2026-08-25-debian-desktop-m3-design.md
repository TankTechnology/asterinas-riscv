# Debian RISC-V Desktop M3 Design

Date: 2026-08-25

## Goal

Boot a signed Debian Trixie RISC-V root on Asterinas into a real, non-root Xorg
session and preserve screenshot evidence. The milestone establishes the
desktop foundation: udev device discovery, PAM/logind session ownership,
VirtIO keyboard and pointer input, an Xorg framebuffer, a window manager, and
a terminal.

This milestone does not claim a full daily-use desktop, accelerated DRM, audio,
or a modern browser. Those build on this gate after it is stable.

## Current foundation

The current branch already provides:

- a signed Debian 13/Trixie ext2 image and immutable package provenance;
- Stage1 handoff to Debian's packaged systemd;
- reliable `/run`, `/tmp`, mount monitoring, D-Bus, and first-cold-boot
  `systemd-logind` startup;
- generic-Sv39, SMP=4 QEMU launch with matching four-hart DTB;
- Asterinas VirtIO input, simple framebuffer/DRM support, and older Xorg
  screenshot and interactive-input demonstrations;
- current `AF_NETLINK` route and `KOBJECT_UEVENT` sockets, replacing the old
  desktop report's unsupported-netlink premise.

## Alternatives

### Extend `systemd-m2`

Rejected. M2 is frozen evidence for the systemd and persistence milestone.
Changing its packages or filesystem identity would make old evidence
ambiguous.

### Reuse the old hand-built desktop initramfs

Rejected as the primary path. It is useful as a source of known-good Xorg,
QEMU, and screenshot conventions, but it bypasses the signed Debian root and
normal distribution package management.

### Add a separate signed `desktop-m3` profile

Chosen. It keeps M2 reproducible, uses Debian's native riscv64 packages, and
lets the gate attribute failures to udev, logind, PAM, Xorg, input, or rendering
without conflating them.

## Profile and session

`desktop-m3` has its own schema version, ext2 label, UUID, output directory,
and exact package identity. Its explicit packages are the M2 base plus:

- `udev` and `libpam-systemd`;
- `xserver-xorg-core`, `xserver-xorg-video-fbdev`, and
  `xserver-xorg-input-evdev`;
- `xinit`, `xauth`, `xfonts-base`, and `xterm`;
- `matchbox-window-manager`.

The builder creates an unprivileged `asterinas` user with a private home and
installs a dedicated systemd service. The service uses `User=asterinas`,
`PAMName=login`, and `TTYPath=/dev/tty1`, then runs `xinit`. The X session
starts matchbox and xterm. Xorg runs against the existing simple framebuffer,
with evdev devices supplied by the Asterinas input stack. No desktop process
runs as root.

## Boot and evidence flow

The QEMU gate reuses the existing descriptor-pinned input/output lifecycle and
process-group cleanup. It adds `bochs-display`, VirtIO keyboard, and VirtIO
tablet devices. U-Boot injects a `simple-framebuffer` node using the assigned
bochs BAR before handing control to Asterinas.

The guest evidence service waits with a bounded deadline and emits one marker
only after all of these are true:

1. `systemd-udevd` and `systemd-logind` are active;
2. `loginctl` reports the `asterinas` PAM session;
3. `/dev/input/event*` contains keyboard and pointer devices;
4. Xorg is running and its log records fbdev plus both evdev devices;
5. the matchbox and xterm processes are owned by `asterinas`.

The host then requests an HMP `screendump`, converts the PPM only for display
when needed, and rejects a blank or single-color framebuffer. The complete
serial transcript, result JSON, and screenshot are atomically written to the
gate output directory.

## Error and safety boundaries

- Inputs stay O_NOFOLLOW and descriptor-pinned as in the existing rootfs gate.
- The canonical signed M2 image is never modified; M3 receives a new image and
  every QEMU run uses a private writable copy.
- Timeouts are total deadlines, not per-read waits.
- A missing udev/logind/session/input/Xorg/render marker is a stable failure,
  and no failed run leaves `passed: true`.
- The known slow second boot in one QEMU process is tracked separately. Desktop
  M3 is a cold-boot gate because warm-reboot performance is not part of its
  correctness claim.

## Acceptance

Desktop M3 passes only when one generic-Sv39, SMP=4, 2 GiB Asterinas cold boot
reaches the non-root X session, keyboard and pointer are attached by Xorg, and
the saved framebuffer contains meaningful rendered pixels. A screenshot alone
or a `graphical.target` transition alone is insufficient.

After this gate is green, the next user-experience slice adds PCManFM and
NetSurf, then networking and interactive focus/input checks. Accelerated DRM
and the physical PCI xHCI keyboard remain separate hardware tracks.
