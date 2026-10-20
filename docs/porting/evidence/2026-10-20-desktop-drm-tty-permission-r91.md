# QEMU DRM desktop tty-control boundary r91

- Gate: `desktop-drm-tty-control` (supplemental E3 diagnostic; one assertion)
- Run: `2026-10-19T20:45:44+00:00`
- Runtime: custom GL QEMU (`/root/qemu-gl-src/build/qemu-system-riscv64`), `virtio-gpu-gl-device`, render node `/dev/dri/renderD129`; no hardware access.
- Rootfs: `/mnt/mydisk/asterinas-firefox-rootfs-20261020-topology-r91/debian-root.ext2`
- Input hashes: kernel `245d54df230f0d8878411d1efd982d5e4189d469f6172379c6c246f56c9fba1e`, stage1 `e03ab1b48f1715d727517ff7c63c203211d05de09dc063793335523e38efd019`, rootfs `f6386c26d7dbee70001da0734e6cc001e58636cbdc6fb39de02ef127d4af4132`, DTB `d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c2`.

## Result

The bounded M4 run terminated with `passed=false` / `guest reported desktop failure`. DRM device setup and libdrm discovery completed, but both DRM and fbdev Xorg attempts failed at the VT-control boundary. The tty1 ownership fix removed the earlier `Permission denied` open failure; it did not provide the VT ioctl capability required by this Asterinas service path.

Decisive serial lines:

```text
DRM_LIBDRM_DEVICE PASS bustype=pci-compat transport=virtio
[    68.744] (WW) xf86OpenConsole: VT_ACTIVATE failed: Operation not permitted
[    68.753] (EE) xf86OpenConsole: Switching VT failed
DEBIAN_DESKTOP_M4_XORG_FAIL provider=drm reason=xorg-process-exited
ASTERINAS_DISPLAY_PROVIDER_RECEIPT requested=drm selected=fbdev reason=xorg-process-exited bounded=1
DEBIAN_DESKTOP_M4_FAIL reason=display-provider-fallback from=drm to=fbdev detail=xorg-process-exited
```

Artifacts (retained as a bound group):

- serial: `/mnt/mydisk/qemu-desktop-m4-r91-topology/desktop-m4-drm.serial.log` SHA-256 `74dac2f531187e7f083d1a9d15688d04aefd1a488bcb1c106028d86fc27c6378`
- result: `/mnt/mydisk/qemu-desktop-m4-r91-topology/result.json` SHA-256 `bc51a1a42eb73a8fee158709e186f089103ff761a6fe9d7bc83dc312a2344533`
- lifecycle: `/mnt/mydisk/qemu-desktop-m4-r91-topology/lifecycle.json` SHA-256 `733728c950c723b3324d86574c93786c6fb34714dabbdc754d28caae238eba67`
- manifest.lock SHA-256 `68645b154bb876db055a7e49b32a55b0f26e3a4bab44e89be4b2223f7abd39f8`

This does not invalidate the earlier independent E2 virgl receipt; it identifies the current M4 service boundary as the next fix. No Firefox scene was run.
