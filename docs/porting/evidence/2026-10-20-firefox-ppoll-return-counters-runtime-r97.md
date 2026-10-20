# Firefox ppoll return-counter runtime probe r97

Date: 2026-10-19T22:01:57Z
Gate: `firefox-ppoll-return-counters-runtime`
Claim: E3 (failed diagnostic boundary; does not satisfy `official_firefox_scene`)

## Scope

This was one bounded probe, not the 900-second Firefox scene gate. It used the
official Debian riscv64 Firefox package and the kernel containing opt-in ppoll
return counters. No hardware, serial console, or physical-board operation was
performed.

## Observed boundary

The guest reached the DRM/virgl precondition:

```
FIREFOX_DRM_GLXTEST rc=0 ... DRI_DRIVER virtio_gpu ... RENDERER virgl ... DRM_RENDERDEVICE /dev/dri/renderD128 ...
```

The gate's Marionette client then repeatedly failed at `stage=tcp_connect`
with `ConnectionRefusedError errno=111`; no greeting, NewSession request, or
response header was observed. The lifecycle ended as `status=failed`,
`phase=finished`, `reason=protocol`.

The new counter lines were emitted for early helper processes, including ready
and zero-result returns and one `errno=EINTR`. They do not identify a lost wake
in Firefox's parent process, so this run does not justify changing ppoll/futex
semantics. The next diagnostic boundary is Marionette listener readiness and
process startup, not another long scene run.

## Inputs and artifacts

* kernel Image SHA-256: `ee5959bddb72609881a97edb3768f1743d35e7735f5dc527f4cacd366ddb54db`
* root image SHA-256: `1018d02c1db8b48ecdab5368a0ed8a80e95a476c602f3409279a4fa8a671a2d4`
* root manifest SHA-256: `af59b9262e96a7b838e0432788a795b061c162f505e8d9c2da5390a634793515`
* serial log: `/mnt/mydisk/qemu-firefox-probe-r97/desktop-m4-drm.serial.log`
  (SHA-256 `299641b88def6a68b9473fc64e634e343e68d51bfb60c9713429e20050c27643`)
* lifecycle: `/mnt/mydisk/qemu-firefox-probe-r97/lifecycle.json`
  (SHA-256 `fa2c09acda4be059593b7bc90e0963e313b4ce906415be0894edf67f55dfb887`)
* result: `/mnt/mydisk/qemu-firefox-probe-r97/result.json`
  (SHA-256 `b27d7059120b1cb0e57baeaaf5111b7ee1edac2473480d1d11be60b48c3a73ca`)

Recovery was bounded QEMU teardown; no guest or board was left running.
