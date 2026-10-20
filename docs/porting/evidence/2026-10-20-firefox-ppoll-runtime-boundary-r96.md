# Firefox ppoll runtime boundary r96

Date: 2026-10-20
Gate: `firefox-ppoll-runtime-boundary`
Claim: E3 (failed runtime boundary; does not satisfy `official_firefox_scene`)

## Inputs and result

The run used the official Debian Firefox rootfs
`/mnt/mydisk/asterinas-firefox-rootfs-20261020-probe-r95/debian-root.ext2`
(SHA-256 `1018d02c1db8b48ecdab5368a0ed8a80e95a476c602f3409279a4fa8a671a2d4`)
and the newly built RISC-V kernel with `asterinas.ppoll_profile=1`.
QEMU was bounded by 420 seconds and cleaned up after the guest timeout.

The DRM precondition remained valid: Xorg logged glamor acceleration on virgl
and render node `/dev/dri/renderD128`. Marionette greeting completed, and the
`WebDriver:NewSession` request was fully sent. The TCP trace then recorded:

* `seq=18 stage=peer-process value0=121 value2=121`;
* `seq=19 stage=socket-events value0=3`;
* `seq=20 stage=pollee-notify value0=3 value1=121`.

The Firefox parent (PID 306) continued running. Its sampled main event loop
executed `ppoll(nfds=3, timeout=0)` and returned `0` at profile call 8192.
No Marionette response header/body arrived before the bounded desktop timeout;
the run ended with `DEBIAN_DESKTOP_M4_FAIL reason=desktop-timeout`.

This rules out a thread stuck in a positive-timeout `ppoll` as the sole cause.
It does not yet prove that a zero-timeout poll missed the notified socket, so no
poll or futex semantic change is made from this run.

Artifacts:

* serial log: `/mnt/mydisk/qemu-firefox-probe-r96/desktop-m4-drm.serial.log`
  SHA-256 `e9852277ff790245172ecce61487ca8fd121b67964a5a16e7e506da2529e8a1d`;
* lifecycle: `/mnt/mydisk/qemu-firefox-probe-r96/lifecycle.json`
  SHA-256 `8fd57b7c43aee5fd9a41b8c6bec2ee4950e72948ce20acae63dc624423dca772`;
* result: `/mnt/mydisk/qemu-firefox-probe-r96/result.json`
  SHA-256 `6475e95266bfb1bda565363a1f811b840af057ac31b5f0fa51383e38151d1ddc`.
