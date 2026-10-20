# QEMU DRM/Xorg TTY handoff r92

- Gate: `desktop-drm-tty-control` (E2 supplemental, one assertion)
- Run: `2026-10-19T20:54:34+00:00`
- Inputs: custom GL QEMU `/root/qemu-gl-src/build/qemu-system-riscv64`, kernel `245d54df230f0d8878411d1efd982d5e4189d469f6172379c6c246f56c9fba1e`, stage1 `e03ab1b48f1715d727517ff7c63c203211d05de09dc063793335523e38efd019`, DTB `d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c2`, rootfs `d3e9d76bb76cff3b99f849cc5be1c480aaa7e6eb7e830e6bd117abd5ac9be1a3`.

## TTY/DRM result

The M4 unit with `TTYPath=/dev/tty1`, `StandardInput=tty`, and TTY lifecycle settings passed the previous VT boundary. The guest reached Xorg readiness and virgl initialization:

```text
[    85.182] (II) modeset(0): glamor X acceleration enabled on virgl
DEBIAN_DESKTOP_M4_XORG_START provider=drm status=ready source=drm-log pid=135
[    93.136] (II) AIGLX: Loaded and initialized virtio_gpu
```

This closes the specific VT-control failure observed in r91. Input setup is a separate issue: both stable evdev nodes were rejected with `Permission denied`.

## Firefox boundary

The selected `desktop-m4-firefox` profile also starts the official Firefox scene service. Its EGL probe passed, but Marionette never accepted TCP connections; no fixed scene screenshot or WebRender application receipt was produced:

```text
FIREFOX_DRM_ACCESS device=renderD128 groups=992,1000 owner=0 group=992 mode=0660 read=1 write=1
FIREFOX_DRM_GLXTEST rc=0 ... RENDERER virgl ... DRM_RENDERDEVICE /dev/dri/renderD128 ...
A_WEB_MARIONETTE_TRANSPORT ... stage=tcp_connect ... ConnectionRefusedError ...
```

Gate result: `passed=false`, lifecycle reason `protocol`. This run is not an E3 pass.

Artifacts:

- serial: `/mnt/mydisk/qemu-desktop-m4-r92-tty/desktop-m4-drm.serial.log` SHA-256 `5b6956a2fe74683f42f9a05ddfe61e0bfb45171f35b08bb3dc66a36d3d3e1860`
- result: `/mnt/mydisk/qemu-desktop-m4-r92-tty/result.json` SHA-256 `49c75f63f5e726e90af70ef4956029e1afdd2d26df07cf3df059974c666e738d`
- lifecycle: `/mnt/mydisk/qemu-desktop-m4-r92-tty/lifecycle.json` SHA-256 `2f03aa510cd3fe4e5151fea4dc0e18b059c30b4f8d3979543397e0b236e7e6f2`
- rootfs manifest: `/mnt/mydisk/asterinas-firefox-rootfs-20261020-topology-r92/rootfs-manifest.json` SHA-256 `3e079503e191ac548e2a2e060e45aea21002bc20a86471e315fb5ed29175f18b`
- manifest.lock SHA-256 `68645b154bb876db055a7e49b32a55b0f26e3a4bab44e89be4b2223f7abd39f8`
