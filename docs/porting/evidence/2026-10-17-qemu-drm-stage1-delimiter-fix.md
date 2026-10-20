# QEMU DRM desktop gate after Stage1 argument-delimiter fix

Date: 2026-10-17 (Asia/Shanghai)

## Gate

This is one bounded, no-recovery QEMU run. It is a QEMU runtime receipt only;
it is not physical-board or Firefox WebRender evidence. The GL-capable
`asterinas/qemu-gl-env:20261014` image was run with `--network=none` and
`/dev/dri/renderD129`. The guest used the retained desktop-m4 kernel, U-Boot,
DTB and Debian root image, with a freshly rebuilt Stage1 initramfs.

The U-Boot command line contains the literal argument delimiter before the
root-init options:

```text
init=/init ... asterinas.display_provider=drm -- --root-init=systemd --root-device=/dev/vdb
```

Before this fix, Stage1 rejected that delimiter as an unknown argument and
stopped at `DEBIAN_ROOTFS_FAIL reason=root-init-argument`. Stage1 now accepts
one delimiter, rejects a duplicate delimiter, and continues to parse the
explicit root device. The native self-test covers both cases.

## Inputs

| input | SHA-256 |
| --- | --- |
| kernel | `08c7acecb9374b86be24c9ae8f73d8b4d5de9d87f7a05b89c466d99c15bdb2f5` |
| U-Boot | `18c34ed87dd053ad012a0d3f8acef7445d40b0199c4da54ec04ba8cbba841710` |
| DTB | `d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c2` |
| Stage1 initramfs | `297d52d2d5eaf059bb559fc991ca15d134748066ba84f9de150930a3731ce3db` |
| root image | `ce2bfc3b450dc5cee79b60e7edfaa43edbd155c138a1d8bc980fcd646f134a3c` |

The Stage1 build output is
`target/debian-riscv/stage1-delimiter-20261017/initramfs.cpio`.

## Results

The focused native Stage1 test passed:

```text
test_native_self_test_covers_discovery_and_handoff_failures ... OK
```

The QEMU result is `passed=true`, `reason=pass`. The serial receipt proves the
ordered handoff and the DRM desktop markers:

```text
DEBIAN_STAGE1_PROGRESS step=root-select source=explicit device=/dev/vdb
DEBIAN_STAGE1_PROGRESS step=handoff-done action=exec
DEBIAN_DESKTOP_M4_DRM_FBDEV dependency=none action=skipped
DEBIAN_DESKTOP_M4_DRM_CONFIG driver=modesetting kmsdev=/dev/dri/card0 fbdev=none
DEBIAN_DESKTOP_M4_DRM_SYSFS card=226:0 render=226:128 driver=virtio_gpu
DEBIAN_DESKTOP_M4_DRM_NODES card=/dev/dri/card0 render=/dev/dri/renderD128
DEBIAN_DESKTOP_M4_XORG provider=drm display=:0 renderer=virgl
PIXEL_PROBE ok=yes left=ffffff right=000000 expect=ffffff/000000 display=gbm renderer='virgl'
DEBIAN_DESKTOP_M4_DRM_X11_SCREENSHOT width=1280 height=800 bpp=32 stride=5120 bytes=4096000 sha256=b00063d6eadf9747639a6251864c6cbe9cafa38c4939c1eac0c074a715e265de
DEBIAN_DESKTOP_M4_READY user=asterinas display=:0
```

The raw result and serial log are archived in
`docs/porting/evidence/2026-10-17-qemu-drm-stage1-delimiter-fix/`.

## Boundary

This closes the Stage1 root-init delimiter/handoff gate and provides a fresh
QEMU Task 5 DRM desktop pass. A separate recovery run using a 60-second
reboot timer did recover through OpenSBI/U-Boot but stopped before its first
desktop marker; that result remains a recovery observation, not a desktop
recovery pass. No physical board, Firefox WebRender, or native GPU claim is
made here.
