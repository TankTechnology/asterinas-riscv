# QEMU rootfs handoff and local Firefox canary, 2026-10-06

This is a QEMU-only verification of the graphical boot path. No physical
board was started. The kernel change is deliberately opt-in: the normal VT
framebuffer renderer remains enabled, while a serial-managed graphical boot
may pass `asterinas.vt_framebuffer=off` to keep VT painting off a slow firmware
scanout BAR. The framebuffer device and VT ABI remain registered for Xorg and
userspace.

## Why the handoff stalled

The earlier experiments showed that the expensive operation was VT's
synchronous full-screen painting and log mirroring after framebuffer
registration. Merely mapping the scanout BAR, or changing its mapping size,
did not explain the delay. With VT painting deferred, rootfs handoff completes
without removing the framebuffer device.

## Reproducible canary

The pinned development image (`4f054ba7e4d3`) ran QEMU with the `virt`
machine, four RISC-V CPUs, 2 GiB RAM, a simple-framebuffer FDT node, the
offline Debian rootfs, and the release kernel built with:

```text
TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
```

The boot disk was patched only in the temporary QEMU overlay. Kernel image
SHA-256:

```text
d1ce9520d14e8444114277a72ac3a9e546c7a53798a588d16fbc25d15a97f183
```

The boot arguments included `asterinas.vt_framebuffer=off`. The isolated root
console then ran one bounded command:

```sh
id -u
cat /proc/sys/kernel/random/boot_id
stat /dev/fb0
timeout --kill-after=1s 5s dd if=/dev/fb0 of=/dev/null bs=4096 count=1 status=none
```

Observed markers were:

```text
uid=0
QEMU_HANDOFF_PROBE_READ_DONE rc=0
QEMU_HANDOFF_PROBE_END
```

The boot produced a fresh boot-id and `/dev/fb0`; the single read completed
within the five-second bound. The expected QEMU-only
`ASTERINAS_DESKTOP_BOOT_FAIL reason=watchdog-not-armed` message is not a rootfs
handoff failure; this canary does not emulate the physical watchdog.

## Local Firefox page

After the handoff, `asterinas-desktop-m5.service` returned `0` and published
the X socket. Firefox was started as the unprivileged `asterinas` user with
the fixed local URL. A Marionette session was created on loopback port 2828,
then the page was navigated and inspected through the DOM:

```text
DESKTOP_START_RC=0
DISPLAY_SOCKET=ready
FIREFOX_BG_RC=0
MARIONETTE_FILE=2828
MARIONETTE_SESSION=True
LOCAL_PAGE_URL=file:///usr/share/asterinas/physical-graphics/index.html
LOCAL_PAGE_TITLE=ASTERINAS_PHYSICAL cycle=0 stage=waiting nonce=
LOCAL_PAGE_TEXT_BYTES=309
LOCAL_PAGE_READY=complete
LOCAL_PAGE_PASS=True
QEMU_LOCAL_FIREFOX_END
```

The guest reports `No GPUs detected via PCI` and the launch still sets
`MOZ_AVOID_OPENGL_ALTOGETHER=1`; this proves the local page and display path,
not GPU acceleration. Hardware DRM/GPU support remains a separate task.

The full serial transcript is retained outside the repository at
`/tmp/asterinas-browser-web-offline-overlay-20261005/qemu-offline-desktop-firefox-local-gate.serial.log`.
