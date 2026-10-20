# QEMU DRM desktop VT handoff fix and recovery gate r24

Date: 2026-10-17 (Asia/Shanghai)

This is a QEMU-only runtime receipt. The physical board was not accessed,
written, rebooted, or probed.

## Change under test

The previous DRM run reached systemd and Xorg, but Xorg terminated at:

```text
(EE) xf86OpenConsole: Switching VT failed
(WW) xf86OpenConsole: VT_ACTIVATE failed: Operation not permitted
DEBIAN_DESKTOP_M4_XORG_FAIL provider=drm reason=xorg-process-exited
```

The M4 desktop entry point now passes `-novtswitch -keeptty` to both Xorg
launch paths. This is the same no-VT-switch policy already used by the M5
desktop entry and is restricted to the Xorg invocation; it does not disable
DRM modesetting or change framebuffer ownership.

The source contract was checked before the runtime run:

```text
m4-vt-contract-pass
```

`bash -n tools/riscv/debian/rootfs/desktop_m4_session.sh` also passed.

## Runtime inputs

The GL-capable QEMU was run from `asterinas/qemu-gl-env:20261014` with
`--device=/dev/dri/renderD128`, `--network=none`, `egl-headless`, and
`virtio-gpu-gl-device`.

| Input | SHA-256 |
| --- | --- |
| kernel | `08c7acecb9374b86be24c9ae8f73d8b4d5de9d87f7a05b89c466d99c15bdb2f5` |
| U-Boot | `18c34ed87dd053ad012a0d3f8acef7445d40b0199c4da54ec04ba8cbba841710` |
| DTB | `d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c2` |
| stage1 | `ce7fb996e9a6ac48160d81c29347a68d88e99204adcc147051df76f645031c84` |
| patched root image | `3b39eb4af533e93d4505a6eac6344c426473aa47d7539a69eb76443fda132fb6` |
| patched manifest | `1a04ca7e0f22103fdcb01f18aad5e868a0e00918953787d278f7f93fd79d4d0a` |

Raw receipt directory:
`/mnt/mydisk/asterinas-gates/qemu-drm-recovery-20261017-r24/`

| Raw file | SHA-256 |
| --- | --- |
| `desktop-m4-drm-recovery.serial.log` | `7bf4fb92825480bfeeba264dd4cda1dd804658366f10571af0c30257a4692b3a` |
| `result.json` | `ed354739716d40bdd14a5127dcc66881f1224695fca26fb86b2ec02b34c77340` |

## Gate result

`result.json` reports `passed=true`, `reason=pass`. The serial receipt contains
the following ordered records:

```text
DEBIAN_DESKTOP_M4_UDEV state=active
DEBIAN_DESKTOP_M4_LOGIND state=active
DEBIAN_DESKTOP_M4_SESSION user=asterinas tty=tty1
DEBIAN_DESKTOP_M4_INPUT keyboard=evdev pointer=evdev
DEBIAN_DESKTOP_M4_DRM_FBDEV dependency=none action=skipped
DEBIAN_DESKTOP_M4_DRM_CONFIG driver=modesetting kmsdev=/dev/dri/card0 fbdev=none
DEBIAN_DESKTOP_M4_DRM_SYSFS card=226:0 render=226:128 driver=virtio_gpu
DEBIAN_DESKTOP_M4_DRM_NODES card=/dev/dri/card0 render=/dev/dri/renderD128
DEBIAN_DESKTOP_M4_XORG provider=drm display=:0 renderer=virgl
PIXEL_PROBE ok=yes left=ffffff right=000000 expect=ffffff/000000 display=gbm renderer='virgl'
DEBIAN_DESKTOP_M4_DRM_X11_SCREENSHOT width=1280 height=800 bpp=32 stride=5120 bytes=4096000 sha256=3b533cd8006fd3e0db2dfb390137c26072d85df0a2223ec7c59261dd663190bc
DEBIAN_DESKTOP_M4_READY user=asterinas display=:0
OpenSBI v1.7
U-Boot 2026.07
```

The gate therefore closes the current QEMU DRM/Xorg/virgl pixel and recovery
leg. It does not yet close Firefox WebRender or native EIC7700 GPU support.
