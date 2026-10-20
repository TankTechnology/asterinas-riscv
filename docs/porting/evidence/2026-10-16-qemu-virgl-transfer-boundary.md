# QEMU virgl transfer and submit-boundary receipt — 2026-10-16

This is a host/QEMU-only receipt from the current RISC-V kernel. The guest ran
with QEMU's `virtio-gpu-gl-device` and `egl-headless` provider; it does not
exercise the physical board or claim PowerVR acceleration.

The prepared payload manifest records:

```text
kernel sha256=08c7acecb9374b86be24c9ae8f73d8b4d5de9d87f7a05b89c466d99c15bdb2f5
initrd sha256=edbc91f56d55cca6ff8a8fe8ae34165c786adfe4469d2ead5594d093f8a10d52
dtb sha256=064a790363ad866bb773835b437d1fb8da73e193bdb4487d5b69e3d5535b563e
```

The gate returned `passed=true` with the current GL QEMU preflight. Its guest
receipt is ordered:

```text
DRM_VIRGL_PARAM 3d=1 capsets=0x2
DRM_VIRGL_CAPS PASS caps_bytes=308
DRM_VIRGL_CONTEXT PASS
DRM_VIRGL_RESOURCE PASS bo=1 res=2 size=4096
DRM_VIRGL_TRANSFER_TO_HOST PASS
DRM_VIRGL_TRANSFER_FROM_HOST PASS
DRM_VIRGL_BOUNDARY PASS size=992
DRM_VIRGL_BOUNDARY PASS size=993
DRM_VIRGL_SUBMIT PASS refused=0
DRM_VIRGL_FENCE PASS
DRM_VIRGL_BACKING PASS
DRM_VIRGL_TIMING caps_context_us=16587 resource_us=479697 submit_us=14797 backing_us=10524
DRM_VIRGL_IMPLICIT PASS
ASTERINAS_DRM_VIRGL_R1_READY
```

The transfer pair exercises both directions of the 44-byte
`DRM_IOCTL_VIRTGPU_TRANSFER_{TO,FROM}_HOST` ABI against the created resource.
The 992- and 993-byte NOP streams cross the old response-overlap boundary and
both complete without a fence descriptor being overwritten. The control run
still expects no 3D, refuses these operations, and remains covered by the
host classifier tests.

Evidence hashes:

```text
a99d7bac1e8187ea7178dd553eb8960c6f081d53742fe27dbaf3cd1521e4f132  target/qemu-uboot/drm-current-virgl-20261016/virgl-evidence/serial.log
fca37bbc5ac7386e96d874055b0dfe6155f2e18eec3f2f06dc7432b52139cc6e  target/qemu-uboot/drm-current-virgl-20261016/virgl-evidence/result.json
5714bb56a012e793a02eaa1d39fc2afc39ef9744f5d3e8f5939430e5ce3a84e4  target/qemu-uboot/drm-current-virgl-20261016/artifacts.json
```

`RESOURCE_FLUSH` is the virtio-gpu 2D control-queue command used by the scanout
provider; it is already covered by the ordered Task 1 scanout receipt,
including the GTK host-backend run in
`2026-10-16-qemu-drm-scanout-gtk.md`. It is not a virgl 3D context-stream
command. The context/submit/fence failure matrix is recorded separately in
`2026-10-16-qemu-virgl-fault-matrix.md`. The remaining open QEMU item is the
independent 2D SETCRTC/transfer/flush fault matrix.
