# QEMU fbdev desktop recovery gate r1

Date: 2026-10-17 (Asia/Shanghai)

## Result

**PASS.** Using the same minimal GL-capable QEMU configuration as the fbdev
scanout r2 gate, one desktop boot armed the guest's bounded software reboot
timer and returned to a fresh OpenSBI/U-Boot epoch. The classifier reports
`passed=true, reason=pass`.

The first desktop epoch recorded an exact framebuffer write/restore pair:

```text
DEBIAN_DESKTOP_M4_FB_RW width=1280 height=1024 stride=5120 bpp=32 format=BgrReserved rect=16x16 status=pass saved_sha256=5f70bf18a086007016e948b04aed3b82103a36bea41755b6cddfaf10ace3c6ef restored_sha256=5f70bf18a086007016e948b04aed3b82103a36bea41755b6cddfaf10ace3c6ef
DEBIAN_DESKTOP_M4_READY user=asterinas display=:0
```

After the desktop marker, the serial receipt contains a new `OpenSBI v1.7`,
new U-Boot banner, and U-Boot prompt. The root image hash after the run is
`fe682e4fc144ec3f22e4ca9f0b4938c2e8e8837c3d5a85a4853af35376ef263e`.

## Boundary and receipt

The run was QEMU-only with `--network=none`, `-nic none`, `-display none`,
four harts, and no VNC or physical board. It verifies software reboot and
firmware recovery, not physical-board recovery or Firefox WebRender.

- Result: `/mnt/mydisk/asterinas-gates/qemu-fbdev-recovery-20261017-qemu-minimal/result.json`
- Serial: `/mnt/mydisk/asterinas-gates/qemu-fbdev-recovery-20261017-qemu-minimal/desktop-m4-recovery.serial.log`
- Result SHA-256: `1f4d94e26f96e686c4a0f485d209297a5e4ff0654e1d7c4329e0bff36c9c5fb5`
- Serial SHA-256: `2e3b5dc74b1a71000b1ef034cea90461084ccb8af52587a9fe7ca2eb956a54de`

