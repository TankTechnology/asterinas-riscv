# QEMU virgl submit-fault replay

Date: 2026-10-17

## Result

The single `submit` fault replay was run in the same isolated GL container as
the happy-path gate (network disabled, input artifacts read-only, no physical
board). It **did not pass**. The gate result was:

```json
{
  "passed": false,
  "reason": "base run did not recover: ('unexpected milestone occurrence: FirmwareReady expected 1, got 2', 'unexpected milestone occurrence: BootloaderReady expected 2, got 3', 'missing milestone: UserspaceReady', 'expected exactly one terminal marker, got 0', 'marker event fields do not match the fixed scenario contract', 'marker event marker_seen did not equal yes')",
  "recovery_complete": false,
  "reported_stage": null,
  "requested_fault": "submit",
  "serial_sha256": "70fbb35266cf2691041f48563daec1a9c53529c1247aa3da76e1f2e737cc30aa"
}
```

The serial log shows the guest booted the ordinary virgl initramfs and reached
`DRM_VIRGL_SUBMIT PASS refused=0`, `DRM_VIRGL_FENCE PASS`, and
`ASTERINAS_DRM_VIRGL_R1_READY`; it never emitted
`DRM_VIRGL_FAIL stage=submit` or `DRM_VIRGL_FAULT stage=submit`.

## Why this is not a driver failure

The supplied boot disk's manifest identifies the ordinary kernel and initramfs:

```text
kernel_sha256=35edfd0fa024396eef9d038c029bce6b33612dea64f6216e34ed6abffbcd1db5
initrd_sha256=159ccd2f0f43b429f453597a7d1cd0e2559e93965109b59d82835ce6410e5838
```

The only preserved fault manifest requires a different pair:

```text
kernel_sha256=b4888f248b8e8df94d05e5f02cf284b9189792ecb9129526a99f1c27bcd9e68a
initrd_sha256=337c660eab77e69fc72a3e9eedd3af229a7903ef52da52162e848421ca04c1f8
```

The fault raw directory contains receipts and serial logs but no matching
boot disk or kernel. Therefore the current replay is an input-provenance
failure, not evidence that the submit fault path is correct or incorrect.
The historical fault receipts remain review artifacts only until their
matching boot inputs are re-archived.

