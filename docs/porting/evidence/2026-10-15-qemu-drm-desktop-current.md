# Current QEMU DRM/Xorg virgl desktop gate — 2026-10-15

This is a QEMU-only result. No physical board, serial console, or hardware
GPU was accessed.

## Host-side fix

The first full gate attempt stopped before guest launch because
`desktop_m4_drm_qemu_argv()` inserted the GL devices between an existing
`-device` flag and its value, producing `-device -device ...`. A regression
test was added first and failed with:

```text
AssertionError: True is not false : the contract must not emit adjacent -device flags
```

The insertion now occurs before the boot-disk `-device` flag and checks the
contract shape. The focused tests pass after the fix:

```text
Ran 2 tests in 0.001s
OK
```

The resulting device sequence is:

```text
-device virtio-gpu-gl-device
-device virtio-keyboard-device
-device virtio-tablet-device
-device virtio-blk-device,drive=bootdisk
```

## Current one-shot desktop run

The current DRM kernel/DTB and the prepared desktop initramfs were run with
QEMU 10.2.1 from `asterinas/qemu-gl-env:20261014`, using
`virtio-gpu-gl-device`, `egl-headless`, no network, and the Debian DRM root
image. The serial evidence is:

```text
(II) modeset(0): glamor X acceleration enabled on virgl
(II) modeset(0): glamor initialized
DEBIAN_DESKTOP_M4_DRM_SYSFS card=226:0 render=226:128 driver=virtio_gpu
DEBIAN_DESKTOP_M4_DRM_NODES card=/dev/dri/card0 render=/dev/dri/renderD128
DEBIAN_DESKTOP_M4_XORG provider=drm display=:0 renderer=virgl
PIXEL_PROBE ok=yes left=ffffff right=000000 expect=ffffff/000000 display=gbm renderer='virgl' gl='3.1 Mesa 25.0.7-2+deb13u1'
DEBIAN_DESKTOP_M4_DRM_X11_SCREENSHOT width=1280 height=800 bpp=32 stride=5120 bytes=4096000 sha256=b00063d6eadf9747639a6251864c6cbe9cafa38c4939c1eac0c074a715e265de
DEBIAN_DESKTOP_M4_CORE_READY user=asterinas display=:0
```

The host classifier returned `passed=true` for this transcript. Evidence
hashes:

```text
caf2a251aeddaea56eac6debb0bbc55b0ce5bfc860cc747060ec04f5e6855636  target/qemu-uboot/drm-current-desktop-20261015/drm-evidence/serial.log
7e866334e4653e87fc654cb24e817a641bff4840371e38a69fa56f42f8815956  target/qemu-uboot/drm-current-desktop-20261015/drm-evidence/result.json
```

This is direct current evidence for Xorg glamor, Mesa virgl and deterministic
EGL/X11 pixels. It is one bounded boot, so it does not by itself satisfy the
two-boot software-restart/recovery requirement.

## Full lifecycle boundary

After the argument fix, the full two-boot gate was run with the current kernel,
GL QEMU and the `desktop-m4-x11` immutable rootfs. It reached Xorg and virgl,
then failed closed at the screenshot step:

```text
DEBIAN_DESKTOP_M4_DRM_X11_SCREENSHOT_FAIL reason=XWD pixel payload is truncated
DEBIAN_DESKTOP_M4_FAIL reason=drm-x11-screenshot
```

The lifecycle result is intentionally `passed=false`; the recovery receipt is
not claimed. The earlier one-shot root image has a working tiled screenshot
helper, but it is not paired with a current schema-4 rootfs manifest, so it
cannot be substituted into the immutable two-boot gate without rebuilding and
publishing a matching rootfs manifest. This is the remaining host/QEMU
desktop-lifecycle prerequisite before any Firefox run or physical display
experiment.
