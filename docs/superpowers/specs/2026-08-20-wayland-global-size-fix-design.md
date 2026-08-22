# Wayland Registry Message Size Fix

## Context

The RISC-V Wayland framebuffer demo reaches userspace, maps `/dev/fb0`, and
connects its client to the compositor. The first registry exchange then fails
with `bad global size`.

Wayland encodes a message header's size in the high 16 bits and its opcode in
the low 16 bits. The writer and compositor stream parser follow that format,
but the client's registry-event loop reads the low 16 bits as the size. For a
`wl_registry.global` event with opcode zero, the client therefore sees a zero
length and exits.

## Scope

Make the smallest behavior change needed to read the registry-event size from
the high 16 bits. Do not redesign the wire codec, compositor, socket handling,
kernel framebuffer support, or kernel UNIX-socket implementation.

## Implementation

In `tools/riscv/wayland/client.c`, decode the size from `sz_op >> 16`, matching
`wl_put_header` in `wire.c` and `stream_recv` in `compositor.c`.

Add a host-side regression test that compiles and runs the real `wire.c`,
`client.c`, and `compositor.c` sources. The test waits for the production
client/compositor handshake to reach the shared-memory render and callback
acknowledgement markers. It must fail against the current low-16-bit decoding
with `bad global size` and pass only after the fix.

## Verification

Verification has three layers:

1. Run the focused regression test and confirm the pre-fix failure is caused by
   reading the opcode as the size.
2. Run the full RISC-V tooling test suite and rebuild the static RISC-V Wayland
   initramfs.
3. Boot the rebuilt initramfs through the existing QEMU/U-Boot/Sv39 pipeline.
   The serial log must contain the registry, shared-memory-pool, render, and
   acknowledgement milestones without `bad global size` or a kernel panic.
   The QEMU screendump must contain the expected red, green, and blue horizontal
   bands rather than a black or panic screen.

## Deliverables

- One focused client-side header-decoding fix.
- One automated regression test that detects the original bit-selection bug.
- Fresh QEMU serial and screenshot evidence under the ignored `target/`
  directory.

## Non-goals

- Supporting arbitrary fragmented Wayland messages in the hand-written client.
- Replacing the demo with libwayland or Weston.
- Changing Asterinas socket, framebuffer, VT, or input-device behavior.
- Changing the full Xorg/systemd desktop assembly pipeline.
