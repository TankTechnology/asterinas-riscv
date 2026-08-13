# Minimal Wayland protocol verification

This directory proves that the Asterinas RISC-V kernel can carry the Wayland
protocol end to end, without pulling in `libwayland`/`libffi`/Weston (whose
riscv64 cross-libraries are not available in the local environment).

The demo is a single static riscv64 `/init`:

- The main process is a **tiny Wayland compositor** that mmaps `/dev/fb0`,
  listens on an AF_UNIX socket, and forks the client.
- The **client** allocates a `memfd`-backed shared buffer, fills it with a
  red/green/blue color-bar pattern, and submits it as a `wl_shm` surface buffer.
- On `wl_surface.commit`, the compositor blits the client's buffer to the
  framebuffer.

The protocol path exercised — AF_UNIX socket, `SCM_RIGHTS` fd passing,
`memfd` shared memory, `wl_display`/`wl_registry`/`wl_shm` handshake — is the
same machinery a real Wayland compositor (Weston) relies on, so this verifies
the kernel-side plumbing without any kernel changes.

## Build and run

```bash
python3 tools/riscv/wayland/build_wayland.sh   # -> target/qemu-uboot/initramfs-wayland.cpio.gz
# ... prepare_qemu_uboot_booti.sh prepare (with that initramfs) ...
python3 tools/riscv/qemu_desktop_boot.py --display-gtk   # or headless + screendump
```

## Verification

- Serial log shows the full handshake: `received shm pool`,
  `rendered buffer to /dev/fb0`, `buffer committed and acknowledged`.
- The framebuffer shows three horizontal color bars (red, green, blue), i.e.
  the client's XRGB8888 buffer was correctly channel-swapped into the
  framebuffer's x8r8g8b8 layout.

## Scope / non-goals

- The wire codec (`wire.c`) supports only the tiny `wayland core + wl_shm`
  subset needed for the demo; strings/ints/fds, no arrays or fixed types.
- `SOCK_STREAM` coalescing is worked around by pacing the client between
  messages (`usleep`). A production stack parses the byte stream; that is out
  of scope here.
- `EVIOCSABS` (write absinfo) is not implemented because `InputDevice` only
  exposes a read-only capability view.

## Next step

Replace the hand-written client/compositor with the real `libwayland`/`Weston`
once riscv64 cross-libraries (or a riscv64 rootfs) are available; the kernel
path verified here needs no change.

## libwayland client variant

`client_libwayland.c` + `build_wayland_libwayland.sh` replace the hand-written
wire client with a real `libwayland-client` client. This required:

- Cross-compiling the dependency chain into `target/riscv-cross/usr`:
  - libffi 3.5.2 (autotools: `./autogen.sh && ./configure --host=riscv64-linux-gnu`)
  - expat 2.7.1 (autotools: `./buildconf.sh && ./configure --host=riscv64-linux-gnu`)
  - libwayland 1.26.0 (meson: `--cross-file` + `-Ddtd_validation=false`, matching
    the host wayland-scanner version)
- Fixing the wire byte order to match the real Wayland format: the message
  header is `size << 16 | opcode`, not `opcode << 16 | size` (the hand-written
  demo had it reversed and only worked because both sides agreed).
- Making the compositor handle the dynamic object ids libwayland allocates
  (registry/compositor/shm/surface/pool ids from the wire), and parse the
  coalesced SOCK_STREAM byte stream instead of one-message-per-recv.

The libwayland client completes the full handshake against the demo compositor
on the host (bound globals, SCM_RIGHTS fd, surface commit, `rendered buffer`).
On the Asterinas kernel it stalls inside `wl_display_roundtrip` because the
kernel does not yet support `MSG_DONTWAIT`/`MSG_NOSIGNAL` on UNIX stream
sockets — libwayland's non-blocking `prepare_read`/poll loop depends on
`MSG_DONTWAIT`, which Asterinas currently warns about and ignores (see
`kernel/src/net/socket/util/message_flags.rs`, `RecvFlags::SUPPORTED`). That is
a kernel socket-flag gap, not a demo-code issue.
