# Firefox Marionette boundary probe r95

Date: 2026-10-20

This is one bounded probe, not a full Firefox scene gate.  It used the
official Debian riscv64 Firefox package with `ASTERINAS_FIREFOX_PROBE_ONLY=1`
and did not start real hardware or a second long scene run.

## Result

`firefox-marionette-tcp-futex-boundary`: **fail**.

The DRM/Xorg precondition passed:

```text
DEBIAN_DESKTOP_M4_XORG_START provider=drm status=ready source=drm-log pid=143
FIREFOX_DRM_GLXTEST rc=0 bytes=211 text=DRI_DRIVER virtio_gpu VENDOR Mesa RENDERER virgl VERSION 3.1 Mesa 25.0.7-2+deb13u1 TFP TRUE DRM_RENDERDEVICE /dev/dri/renderD128 MESA_VENDOR_ID 0x1af4 MESA_DEVICE_ID 0x1050 DDX_DRIVER modesetting; TEST_TYPE EGL
```

The request boundary was reached:

```text
A_WEB_MARIONETTE_TRANSPORT ... command="WebDriver:NewSession" ... stage="send"
A_WEB_MARIONETTE_TRANSPORT ... command="WebDriver:NewSession" ... stage="send_complete"
```

No `response_header` or `response_body` marker for request 1 appeared before
the bounded probe ended.  The TCP diagnostics in the same serial log show the
2828 request was received and notified (`seq=18` server receive, `seq=19`
socket-events, `seq=20` pollee-notify).  Therefore this run rules out listener
creation and guest TCP delivery as the immediate boundary; it does not prove a
specific Firefox or kernel cause.

The gate process recorded slow futex/read calls after the request, including
`pid=118 tid=312 syscall=98 futex elapsed_jiffies=29365`,
`pid=118 tid=615 syscall=98 futex elapsed_jiffies=9591`, and
`pid=118 tid=615 syscall=63 read elapsed_jiffies=1292`.  The existing bounded
RISC-V futex probe and r19 wake-pairing receipt pass, so no futex semantic patch
is justified from this run alone.

`/proc/net/tcp*` is not implemented by this guest.  The probe therefore emits
an explicit `tcp=error:FileNotFoundError` marker and does not use that absence
as evidence that the listener was missing.

The guest did not publish its own atomic `FIREFOX_DRM_PROBE_RECEIPT`; the host
run ended with `lifecycle.status=failed` and `reason=guest reported desktop
failure`.  This host receipt records that missing guest terminal marker
separately from the NewSession boundary.

## Inputs and artifacts

| Item | SHA-256 / path |
| --- | --- |
| rootfs | `/mnt/mydisk/asterinas-firefox-rootfs-20261020-probe-r95/debian-root.ext2`, `1018d02c1db8b48ecdab5368a0ed8a80e95a476c602f3409279a4fa8a671a2d4` |
| serial log | `/mnt/mydisk/qemu-firefox-probe-r95/desktop-m4-drm.serial.log`, `2dee9823e53d454f7c01f5fae0c6454bff82befa0cbdac3734b3a8d19603827d` |
| lifecycle | `/mnt/mydisk/qemu-firefox-probe-r95/lifecycle.json`, `dad23d6412799af8a747b20dd63fa90415542a4e323aa6f74ff105d99efe45cf` |
| result | `/mnt/mydisk/qemu-firefox-probe-r95/result.json`, `968db258977924474709abcc1cb50caed1d64e860606ad36aed16c451113ea93` |

The input manifest is `68645b154bb876db055a7e49b32a55b0f26e3a4bab44e89be4b2223f7abd39f8`.

