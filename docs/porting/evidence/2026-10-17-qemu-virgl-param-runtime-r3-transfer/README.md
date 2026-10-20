# QEMU virgl parameter runtime replay r3 — transfer boundary

Date: 2026-10-17

## Scope

One bounded GL-capable QEMU run used a newly generated, identity-bound bundle:

```text
target/qemu-uboot/virgl-replay-20261017-transfer/{u-boot,boot.ext4,artifacts.json}
```

The run used the repository's retained
`target/drm-virgl/current-20261016-transfer-boundary-initramfs.cpio.gz`.
Networking was disabled and only `/dev/dri/renderD129` was passed through. No
physical board, serial port, or hardware MMIO was used.

## Payload identity

The ext4 payloads were dumped back with `debugfs` and compared byte-for-byte
before launch. The bundle records these hashes:

```text
kernel_sha256=08c7acecb9374b86be24c9ae8f73d8b4d5de9d87f7a05b89c466d99c15bdb2f5
dtb_sha256=d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c2
initrd_sha256=edbc91f56d55cca6ff8a8fe8ae34165c786adfe4469d2ead5594d093f8a10d52
boot.ext4_sha256=9cbf36dd10f799b2e13a167a774ffa099814c86c948931a716e53ba41ae8fac3
manifest_sha256=7b465308c9142eff8d3d605aa9e61d7bd5864941a700dc3b966c8e8d04029663
```

## Result

The strict virgl parameter gate passed:

```json
{"backing_us": 8062,
 "caps_bytes": 308,
 "caps_context_us": 13794,
 "expected_3d": 1,
 "passed": true,
 "reason": "passed",
 "reported_3d": 1,
 "reported_capsets": 2,
 "resource_bo": 1,
 "resource_res": 2,
 "resource_us": 401314,
 "submit_us": 11715}
```

The base U-Boot handoff also passed (`boot-result.json`). The serial receipt
contains the ordered 3D capability, context, resource, transfer-to-host,
transfer-from-host, 992/993 boundary, submit, fence, backing, timing,
implicit-submission and ready markers. The raw serial and boot receipts are
stored beside this file.

This is a QEMU virgl runtime result. It does not count as physical-board or
Firefox WebRender evidence.
