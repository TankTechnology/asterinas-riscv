# QEMU Firefox DRM scene r18

This is a fail-closed Task 6 diagnostic. It ran only in QEMU with the GL
capable QEMU binary, no NIC, and no physical-board access. The full serial
receipt is [serial.log](2026-10-16-qemu-firefox-drm-scene-r18/serial.log).
The compact runner result is in
[result.json](2026-10-16-qemu-firefox-drm-scene-r18/result.json).

## Inputs and bounds

- QEMU: `/root/qemu-gl-src/build/qemu-system-riscv64`
- QEMU binary SHA-256: `d867a49eda903ff33cdc9138b6f1b0596e94c8445b912a9a83c7337ddca1f31a`
- display/device: `-display egl-headless -device virtio-gpu-gl-device`
- network: `-nic none`
- U-Boot SHA-256: `3e5ae37dcd7071a274780414bc37c74ac1df12b916d77532b290e4eb61a5fc80`
- boot disk SHA-256: `1b7fd88f41c100ab6f5dc1e88a6deafd8a1ef9415758b85032646a06f5f8b315`
- kernel in boot disk SHA-256: `f2cb57a9248162739e7c546dc95557d459ce9672fd645e66c159e96b9ec225b1`
- DTB in boot disk SHA-256: `22a33eaefb4c064dc04482d2b336144e2f4352615c0f9052ba55100689a835a3`
- stage1 initramfs SHA-256: `16c6558760b92e16e605edfd1b52eef5eee9d9a188dc7502a0343f7815771cc4`
- disposable root image SHA-256: `1dfc06dd8a20efa4ced5451247a3aa4b2f5fb90028201a2e159446a0c1751eef`
- injected gate SHA-256: `0f110df0732ca27346770ef159330e7e798dc9f34c1462ad8e0907478634bd89`
- injected scene SHA-256: `31ed5f5836586c4503fce39547866366d50e70f029ec16b28fddb3b59d080915`
- injected service SHA-256: `43a033bd66506b846ec50b0dd394b30db34834ce56f21c1fb62cf1b79abce901`
- gate budget: 600 s per run, NewSession 540 s, service 1200 s, outer runner 1300 s
- serial SHA-256: `e3789e027cb1e68ce446946b4473eef452f98bb18115eb8cbbd4d8a249dc478e`

## Observed boundary

The desktop reached the DRM/Xorg stage before Firefox. Firefox eventually
opened Marionette after a long cold start. The ordered application markers were:

```text
FIREFOX_DRM_PHASE name=new-session
FIREFOX_DRM_PHASE name=window-rect
FIREFOX_DRM_PHASE name=window-context
FIREFOX_DRM_PHASE name=transport-probe
FIREFOX_DRM_TRANSPORT probe=pass
FIREFOX_DRM_SUPPORT_FALLBACK reason=privileged_page_deferred
FIREFOX_DRM_PHASE name=scene
```

The transport log contains complete request/response frames after the probe,
so the boundary is no longer socket or NewSession setup. The gate stopped at
the bounded scene deadline and emitted:

```text
FIREFOX_DRM_FAIL_TYPE type=GateError
FIREFOX_DRM_FAIL_REASON reason=Firefox_fixed_scene_did_not_finish
FIREFOX_DRM_FAIL reason=GateError:Firefox fixed scene did not finish
```

No `FIREFOX_DRM_SUPPORT`, `FIREFOX_DRM_GL`, screenshot, frame, or recovery
marker was emitted. Therefore this run does **not** prove Firefox WebRender,
virgl in Firefox, pixels, or restart recovery. It narrows the next Task 6
diagnostic to the local scene's readiness/DOM execution after a healthy
Marionette transport. The QEMU Task 5 virgl receipt remains valid and is not
changed by this fail-closed result.

