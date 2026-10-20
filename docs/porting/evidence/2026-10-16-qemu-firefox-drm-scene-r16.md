# QEMU Firefox DRM scene r16 — 2026-10-16

This is a QEMU-only, disposable-rootfs receipt. It did not access the
development board, write a serial console, or perform a hardware reset.

The run used the GL-capable QEMU 10.2.1 backend with
`egl-headless`/`virtio-gpu-gl-device`, the existing DRM desktop artifacts, and
a temporary browser rootfs. The temporary rootfs contained the current gate
source and a pure DOM/CSS scene; its image hash is recorded in `result.json`
and is not a release artifact.

## Runtime input

```text
kernel             35edfd0fa024396eef9d038c029bce6b33612dea64f6216e34ed6abffbcd1db5
u_boot             3e5ae37dcd7071a274780414bc37c74ac1df12b916d77532b290e4eb61a5fc80
dtb                f2bf8e524fe1e4ecb1f2f56665c9827bfe5dd990ffbacbb735ab466e025037d5
stage1_initramfs   16c6558760b92e16e605edfd1b52eef5eee9d9a188dc7502a0343f7815771cc4
root_image         63b27472fd1c3be747d6aa616dad82902b90766c8a773b3112fc52b9c8fa917d
serial_sha256      2566ae89b9330ed83ba678fb1693d006b0ad72b10514e752765aef8d833e49ed
result_sha256      458121c2eaadde2ff2073303b536c41225bf5a7a34839cde58dbc3a0a45a2f48
```

## Ordered result

The guest reached the existing rootfs handoff and DRM/Xorg startup, then
Firefox reached `NewSession` and `SetWindowRect`. The gate deliberately
deferred the privileged `about:support` page, opened the local scene, and
waited for the fixed scene marker. It failed closed at the bounded Marionette
script deadline:

```text
FIREFOX_DRM_SUPPORT_FALLBACK reason=privileged_page_deferred
FIREFOX_DRM_PHASE name=scene
FIREFOX_DRM_FAIL_REASON reason=Marionette_gate_deadline_expired
```

No `FIREFOX_DRM_SUPPORT`, `FIREFOX_DRM_GL`, screenshot, frame, or recovery
receipt was emitted. The temporary Firefox MOZ log and screenshot inode both
remained zero-sized. Therefore Task 6 is **not passed**. This isolates the
remaining boundary to Firefox's Marionette/scene execution on the DRM guest;
it does not change the already-passed QEMU Task 5 virgl/Xorg receipt.

Full raw serial and result files are in this directory.
