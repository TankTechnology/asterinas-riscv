# QEMU VirtIO-GPU DRM scanout gate — 2026-10-13

This receipt records one fresh QEMU run of the independent Asterinas DRM
scanout gate. It uses the generic Sv39/SMP=4 profile and a single
`virtio-gpu-device`; networking, USB input, fbdev, and the cursor trace are not
part of this run.

The gate passed its host contract tests and its guest lifecycle. The prepared
artifacts were:

| artifact | SHA-256 |
| --- | --- |
| scanout initramfs | `01cb47af179b7ecb741fc1353ef61cb853a678bb95ececb8b2b1dbda433d465e` |
| kernel (boot result) | `0c2d5a0404770265e8aadbe8b19ff6d87c7d2d280efc9d90a09b98768ceaf092` |
| DTB (boot result) | `1527811c1ca2f77e072ad7565966c7e3fc48c8a784897e504c467766f63e2fec` |

The guest emitted, in order, `DRM_SCANOUT_DEVICE`, the `virtio-gpu` driver
version, dumb-buffer capability, one CRTC/connector/encoder, a connected mode,
mode-sized dumb-buffer creation and guest readback, ADDFB, SETCRTC, GETCRTC,
cleanup, and `ASTERINAS_DRM_SCANOUT_R1_READY`. The mode reported by this QEMU
instance was 1280×800. The QEMU trace window after the guest marker contained
exactly one ordered sequence for resource creation, backing attachment,
scanout selection, transfer, and flush (resource id `0x2`). The classifier
reported:

```json
{
  "passed": true,
  "reason": "passed",
  "trace_count": 5,
  "screenshot_sha256": "50adbf5d9bd46e1afd982876b5bd7efda93a77583be29adbfb2d36b5ad252e2d",
  "screenshot_audit": {
    "width": 1280,
    "height": 800,
    "distinct_colors_lower_bound": 3,
    "non_black_pixels": 1023935,
    "passed": true
  }
}
```

Evidence is under
`target/qemu-uboot/drm-scanout-20261013/evidence/` in the prepared build
environment. This is a QEMU DRM/KMS control-path receipt. It does not claim
PowerVR acceleration or physical-board scanout; those require separate
hardware evidence.

## Xorg modesetting screenshot validation

An existing QEMU Debian DRM desktop run was revalidated with the new
`xorg_modesetting_gate.py` contract. Its serial evidence contains the ordered
`modesetting` + `virtio_gpu` Xorg marker, the desktop client markers, and an
`llvmpipe` renderer observation. The QMP P6 screenshot is 1280×800 and passed
the painted-content threshold:

```json
{
  "passed": true,
  "marker_count": 7,
  "width": 1280,
  "height": 800,
  "distinct_colors": 1407,
  "non_background_pixels": 986514,
  "pixel_sha256": "e7a3a45a9c290f18024692d8360ed866d5a99fbe4f70bd896e141e43e8bef7ab",
  "ppm_sha256": "766a96d8292c04b48686a20f767cc42e7d6d12dc5a32caa56310c21b91ba383a",
  "renderer": "llvmpipe"
}
```

The copied receipt and image are under
`target/drm-xorg-modesetting-20261013/`. This proves the QEMU
modesetting/Xorg scanout and screenshot path. `llvmpipe` is recorded rather
than treated as hardware acceleration; GPU acceleration remains a separate
milestone.

The scanout classifier also has bounded fault-injection tests for the three
control boundaries that matter here: missing `SETCRTC`, transfer, and flush
trace records. Each mutation fails closed with a stage-specific reason. A
second clean run reuses the same evidence directory and replaces the failed
result, proving that recovery cannot inherit stale success. These are
host-side transcript injections; the QEMU device itself has no deterministic
failure switch for those three commands yet.
