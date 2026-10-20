# QEMU fbdev VNC scanout gate r1

Date: 2026-10-17 (Asia/Shanghai)

## Result

**FAIL CLOSED.** This was one bounded QEMU-only scanout attempt using the
newly built QEMU 10.2.1 binary with VNC support. The guest reached the fixed
Stage1 root handoff, but the desktop scanout marker did not arrive before the
gate deadline. The run therefore produced no screenshot and is not counted as
a scanout pass.

The last guest progress was:

```text
DEBIAN_STAGE1_PROGRESS step=handoff-enter action=exec
```

The serial log then remained in systemd's `ldconfig.service` start job until
the bounded process cleanup. The exact repeated line was:

```text
Job ldconfig.service/start running (1min 6s / no limit)
```

No `DEBIAN_DESKTOP_M4_FB_SCANOUT_READY`, desktop-ready marker, or PPM
artifact was accepted. This is a guest boot/protocol timeout with the VNC
variant; it is not evidence of a framebuffer pixel mismatch and does not
claim a physical-board result.

## Inputs and command boundary

The gate used the retained kernel, U-Boot, DTB, Stage1 delimiter-fix
initramfs, and current desktop root image. QEMU ran in
`asterinas/qemu-gl-env:20261014`, with `--network=none`, `-nic none`, and a
private Unix VNC socket. No real hardware, serial device, or board reboot was
used.

The built QEMU binary SHA-256 is:

```text
d4a95ece1ea69687257cb584182bce231d8f379fda0231ec62d4da07611e1952
```

The frozen argv included:

```text
-display none -vnc unix:<private-socket> -nic none
-device bochs-display,xres=1280,yres=1024
```

## Receipt

The machine-readable result is
`/mnt/mydisk/asterinas-gates/qemu-fbdev-scanout-20261017-qemu102-vnc/result.json`.
It records `passed=false` and `reason=protocol`.

Raw serial log:
`/mnt/mydisk/asterinas-gates/qemu-fbdev-scanout-20261017-qemu102-vnc/desktop-m4-scanout.serial.log`

Serial SHA-256:

```text
c6f47ad3c0f93a7ff4720bd0e27987e357e2fce8676d6cd2a7d6399661151c60
```

