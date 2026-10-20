# Current QEMU DRM scanout gate — 2026-10-15

This receipt records the first current-source KMS/scanout run after correcting
the DRM driver-name assertion. The initial attempt failed before the version
marker because the probe expected `virtio-gpu`; the kernel's Mesa-compatible
DRM ABI is `virtio_gpu`. That failure was retained in the run directory and
was not treated as a pass.

## Fix and regression test

The probe and host classifier now require:

```text
DRM_SCANOUT_VERSION name=virtio_gpu
```

The test `test_driver_name_uses_mesa_abi_spelling` locks this spelling down.
The focused suite reports 9 tests passed, and the static C layout self-test
prints `DRM_SCANOUT_SELF_TEST PASS layout`.

## Passing runtime

Inputs are the current kernel, the QEMU `virt` SMP=4 profile, and the static
scanout probe initramfs:

```text
kernel sha256=35edfd0fa024396eef9d038c029bce6b33612dea64f6216e34ed6abffbcd1db5
initrd sha256=dc698032f48f24eab9ad8908e60a651af656e2e9dbc644f6fbcc9bc312d7389d
dtb sha256=f2bf8e524fe1e4ecb1f2f56665c9827bfe5dd990ffbacbb735ab466e025037d5
qemu=QEMU 10.2.1, machine virt, SMP=4, TCG
```

The gate returned:

```json
{"passed": true, "reason": "passed", "trace_count": 5,
 "screenshot_sha256": "50adbf5d9bd46e1afd982876b5bd7efda93a77583be29adbfb2d36b5ad252e2d"}
```

The guest serial log contains the ordered protocol:

```text
DRM_SCANOUT_DEVICE virtio-gpu
DRM_SCANOUT_VERSION name=virtio_gpu
DRM_SCANOUT_CAP dumb=1
DRM_SCANOUT_RESOURCES crtcs=1 connectors=1 encoders=1
DRM_SCANOUT_CONNECTOR connected=1 modes=1
DRM_SCANOUT_DUMB create=mode bpp=32
DRM_SCANOUT_READBACK status=pass
DRM_SCANOUT_ADDFB status=pass
DRM_SCANOUT_SETCRTC status=pass
DRM_SCANOUT_GETCRTC fb=1
DRM_SCANOUT_CLEANUP status=pass
ASTERINAS_DRM_SCANOUT_R1_READY
```

QEMU's five matching traces were observed in order: resource creation,
backing attachment, SET_SCANOUT, transfer-to-host, and flush. The captured
1280×800 image covered the full frame, had 1,023,935 non-black pixels, and at
least three distinct colors. Evidence hashes:

```text
d4324ed63e39ac36dd097b2f6c8183ca3b2c8c91529c484ec5b39a671868e785  target/qemu-uboot/drm-current-scanout-20261015/scanout-evidence/serial.log
343473c2a84e518f494ed07d076de60447a2ef6cb91687a8d385c87648c2e957  target/qemu-uboot/drm-current-scanout-20261015/scanout-evidence/result.json
50adbf5d9bd46e1afd982876b5bd7efda93a77583be29adbfb2d36b5ad252e2d  target/qemu-uboot/drm-current-scanout-20261015/scanout-evidence/scanout.ppm
```

This proves the current kernel's basic DRM KMS/scanout path and pixel
readback under QEMU. It does not yet prove virgl 3D, Mesa acceleration, or
Firefox WebRender.
