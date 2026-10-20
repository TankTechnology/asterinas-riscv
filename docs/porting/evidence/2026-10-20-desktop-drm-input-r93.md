# QEMU DRM/Xorg/input handoff r93

- Gate: `desktop-drm-input-control` (E2 supplemental, one assertion)
- Run: `2026-10-19T20:54:00Z`–`2026-10-19T20:58:21Z`
- Runtime: custom GL QEMU with `egl-headless,rendernode=/dev/dri/renderD129`, `-nic none`
- Inputs: kernel `245d54df230f0d8878411d1efd982d5e4189d469f6172379c6c246f56c9fba1e`, stage1 `e03ab1b48f1715d727517ff7c63c203211d05de09dc063793335523e38efd019`, DTB `d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c2`, root image `d86e115401d7bf0bc4f298f12917b83f8755d0ff8f942c1854cb6c25af990339`.

## E2 boundary

The DRM desktop reached Xorg modesetting and virgl initialization. The ordered
input-permission phase also completed: both keyboard and pointer evdev nodes
were opened and configured, with no `Permission denied` record.

```text
glamor X acceleration enabled on virgl
DEBIAN_DESKTOP_M4_XORG_START provider=drm status=ready source=drm-log pid=141
AIGLX: Loaded and initialized virtio_gpu
evdev: Asterinas keyboard: Configuring as keyboard
evdev: Asterinas pointer: Configuring as touchscreen
```

The Firefox EGL probe independently reported the virgl renderer and the guest
render node:

```text
FIREFOX_DRM_ACCESS device=renderD128 ... mode=0660 read=1 write=1
FIREFOX_DRM_GLXTEST rc=0 ... RENDERER virgl ... DRM_RENDERDEVICE /dev/dri/renderD128 ...
```

This closes the E2 input-access boundary. It does not close E3: the official
Firefox process never exposed a Marionette listener, so no application scene
or pixel hash was produced.

## E3 failure boundary observed in the same run

The scene gate reached its probe-connect phase and received repeated
`ConnectionRefusedError` responses while trying to connect to Marionette:

```text
A_WEB_MARIONETTE_TRANSPORT ... command="greeting" stage="tcp_connect" ... error_type="ConnectionRefusedError" errno=111
```

The run ended with lifecycle `status=failed`, `reason=protocol`; this is an
E3 userspace listener boundary, not a DRM/virgl renderer failure. No screenshot
hash is claimed.

## Artifacts

- Serial: `/mnt/mydisk/qemu-desktop-m4-r93-input/desktop-m4-drm.serial.log`, SHA-256 `18e889e6c54693cab8aab3560846349e012370c2bd53ad24ad0de550e2199d91`
- Lifecycle: `/mnt/mydisk/qemu-desktop-m4-r93-input/lifecycle.json`, SHA-256 `3cf7a93cf3849ae54e44256357893fbcafeb5320340871ff2b25109871cce354`
- Result: `/mnt/mydisk/qemu-desktop-m4-r93-input/result.json`, SHA-256 `98a7c761faae9bba5a84f8a4a18838893333b4445c95c8ae0faa089320844b1f`
- Rootfs manifest: `/mnt/mydisk/asterinas-firefox-rootfs-20261020-topology-r93/rootfs-manifest.json`, SHA-256 `a73696e71b4f2b571c8083dacd511b61198a48508668cbb3779813b421a4330d`
- Development overlay manifest: `/mnt/mydisk/asterinas-firefox-rootfs-20261020-topology-r93/dev-overlay-manifest.json`, SHA-256 `e3983151e2712ae205b5a95bb4c5203f1f2372d35ea9343b17a84b38d777e9ff`
- Root image: `/mnt/mydisk/asterinas-firefox-rootfs-20261020-topology-r93/debian-root.ext2`, SHA-256 `d86e115401d7bf0bc4f298f12917b83f8755d0ff8f942c1854cb6c25af990339`
- `manifest.lock` SHA-256: `68645b154bb876db055a7e49b32a55b0f26e3a4bab44e89be4b2223f7abd39f8`
