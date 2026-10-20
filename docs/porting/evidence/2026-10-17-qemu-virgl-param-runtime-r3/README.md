# QEMU virgl parameter runtime replay r3

Date: 2026-10-17

## Scope

One bounded replay used a newly generated, identity-bound boot bundle:

```text
target/qemu-uboot/virgl-replay-20261017/{u-boot,boot.ext4,artifacts.json}
```

The GL-capable QEMU container ran with networking disabled and only
`/dev/dri/renderD129` passed through. No physical board, serial port, or
hardware MMIO was used.

## Payload identity

The boot disk was rebuilt from the repository's current kernel, DTB and
`current-20261015-initramfs.cpio.gz`. The ext4 payloads were dumped back with
`debugfs` and compared byte-for-byte before the run. The manifest and boot
result carry these hashes:

```text
kernel_sha256=08c7acecb9374b86be24c9ae8f73d8b4d5de9d87f7a05b89c466d99c15bdb2f5
dtb_sha256=d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c2
initrd_sha256=159ccd2f0f43b429f453597a7d1cd0e2559e93965109b59d82835ce6410e5838
boot.ext4_sha256=4e4761cbdafc9f331ddb0c8493299e328af9b4bc9618fbbc03752ca8cbcf7b9f
manifest_sha256=3619ce38ce17f7cc06994104b2f869d66b6abc728340d20f65645cdb6712c598
```

## Result

The U-Boot/base runtime completed (`boot-result.json` has `passed=true`) and
serial evidence reached the virgl ready marker. The strict classifier rejected
the run:

```json
{"passed": false,
 "reason": "missing or unordered transfer markers",
 "reported_3d": 1,
 "reported_capsets": 2,
 "caps_bytes": 308,
 "resource_bo": 1,
 "resource_res": 2}
```

The serial order reached:

```text
DRM_VIRGL_PARAM 3d=1 capsets=0x2
DRM_VIRGL_CAPS PASS caps_bytes=308
DRM_VIRGL_CONTEXT PASS
DRM_VIRGL_RESOURCE PASS bo=1 res=2 size=4096
DRM_VIRGL_SUBMIT PASS refused=0
DRM_VIRGL_FENCE PASS
DRM_VIRGL_BACKING PASS
DRM_VIRGL_TIMING caps_context_us=14042 resource_us=424872 submit_us=14227 backing_us=9034
DRM_VIRGL_IMPLICIT PASS
ASTERINAS_DRM_VIRGL_R1_READY
```

There are no `DRM_VIRGL_TRANSFER_TO_HOST` or
`DRM_VIRGL_TRANSFER_FROM_HOST` markers. This is a guest probe/input-version
mismatch, not a QEMU launch or payload-integrity failure. The strict gate
therefore correctly remains fail-closed.

Raw files in this directory are the replay receipt. The next bounded attempt
must use `target/drm-virgl/current-20261016-transfer-boundary-initramfs.cpio.gz`
and regenerate a new manifest/boot disk pair; no existing bundle should be
mutated.
