# QEMU DRM desktop gate r2

This is the readable index for the completed r2 QEMU runtime receipt.  The
original `result.json` and `desktop-m4-drm.serial.log` remain in the
root-owned runtime directory
`target/desktop-m4-current-overlay-20261016-status/gate-gl-r2/`; they were
read and hashed from the development container without changing them.

## Runtime identity

- profile: `desktop-m4`
- QEMU display: `egl-headless`
- GPU device: `virtio-gpu-gl-device`
- network: disabled (`-nic none`)
- classifier result: `passed=true`, `reason=pass`
- `result.json` SHA-256:
  `03a405874ca3ae66494da4fa9dfff1e21020d646a14179217d7098f5e0d88f10`
- serial log SHA-256:
  `3f74921006284bb35d809be50ae3802c675eaca5eac22649eed2eaa09524be08`

Input hashes recorded by `result.json`:

| Input | SHA-256 |
| --- | --- |
| kernel | `f3e8cd97e95c487adf13ceb36afd120b777c6d02461a097698ed1d52ec643913` |
| U-Boot | `1bd37d97b159cae4ffa3c8b6b96612178453257014c5716c425d441df2330c4f` |
| DTB | `586d1136aed48f8935a35febe0cf7d68fe44892215ab81209def62ffdb000142` |
| stage1 initramfs | `96f03ab573ae37e567eef6327e343f0295c34808bf881d3150282819fb73dfcb` |
| root image | `ce2bfc3b450dc5cee79b60e7edfaa43edbd155c138a1d8bc980fcd646f134a3c` |
| rootfs manifest | `139eb1c87ac10d426c4b76810deeafe02f99226736053ab67609b999d4323752` |

## Ordered gate markers

The serial receipt contains these ordered markers:

```text
DEBIAN_DESKTOP_M4_DRM_FBDEV dependency=none action=skipped
DEBIAN_DESKTOP_M4_DRM_CONFIG driver=modesetting kmsdev=/dev/dri/card0 fbdev=none
DEBIAN_DESKTOP_M4_DRM_SYSFS card=226:0 render=226:128 driver=virtio_gpu
DEBIAN_DESKTOP_M4_DRM_NODES card=/dev/dri/card0 render=/dev/dri/renderD128
DEBIAN_DESKTOP_M4_XORG provider=drm display=:0 renderer=virgl
PIXEL_PROBE ok=yes left=ffffff right=000000 expect=ffffff/000000 display=gbm renderer='virgl' gl='3.1 Mesa 25.0.7-2+deb13u1'
DEBIAN_DESKTOP_M4_DRM_X11_SCREENSHOT width=1280 height=800 bpp=32 stride=5120 bytes=4096000 sha256=b00063d6eadf9747639a6251864c6cbe9cafa38c4939c1eac0c074a715e265de
DEBIAN_DESKTOP_M4_READY user=asterinas display=:0
```

The host classifier was re-run against this exact serial log and returned
`GateResult(passed=True, reason='pass')`.  This closes the previously missing
DRM-only rootfs and `kmsdev` ordered-marker checks for Task 5.  It is a QEMU
virgl receipt only; it does not claim Firefox WebRender or native EIC7700
acceleration.

