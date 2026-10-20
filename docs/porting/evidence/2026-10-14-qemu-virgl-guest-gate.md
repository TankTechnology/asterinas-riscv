# QEMU guest VirGL parameter gate — 2026-10-14

This is a guest-side QEMU result. It does not touch the EIC7700 board and it
does not prove native PowerVR acceleration.

## Environment

- QEMU: 10.2.1, built with OpenGL, GBM and virglrenderer in
  `asterinas/qemu-gl-env:20261014`.
- Device: `virtio-gpu-gl-device` with QEMU's `egl-headless` display backend.
- Kernel: current workspace RISC-V build at the time of the run.
- Profile: `generic-sv39-drm-virgl-smp4`.
- Initramfs: `target/drm-virgl/current-initramfs.cpio.gz`.
- Output: `target/qemu-uboot/drm-virgl-current/evidence/`.

The host preflight was run before starting the guest. The command used for the
guest gate was:

```text
PYTHONPATH=tools/riscv python3 tools/riscv/drm/virgl_param_gate.py \
  --uboot target/qemu-uboot/cache/u-boot-build/u-boot \
  --boot-disk target/qemu-uboot/drm-virgl-current/boot.ext4 \
  --manifest target/qemu-uboot/drm-virgl-current/artifacts.json \
  --output-directory target/qemu-uboot/drm-virgl-current/evidence
```

## Result

The classifier returned `passed=true`:

```json
{
  "backing_us": 8944,
  "caps_bytes": 308,
  "caps_context_us": 13760,
  "expected_3d": 1,
  "passed": true,
  "reported_3d": 1,
  "reported_capsets": 2,
  "resource_bo": 1,
  "resource_res": 2,
  "resource_us": 444492,
  "submit_us": 14244
}
```

The serial receipt contains, in order:

```text
DRM_VIRGL_PARAM 3d=1 capsets=0x2
DRM_VIRGL_CAPS PASS caps_bytes=308
DRM_VIRGL_CONTEXT PASS
DRM_VIRGL_RESOURCE PASS bo=1 res=2 size=4096
DRM_VIRGL_SUBMIT PASS refused=0
DRM_VIRGL_FENCE PASS
DRM_VIRGL_BACKING PASS
DRM_VIRGL_TIMING caps_context_us=13760 resource_us=444492 submit_us=14244 backing_us=8944
DRM_VIRGL_IMPLICIT PASS
ASTERINAS_DRM_VIRGL_R1_READY
```

This proves the current guest can negotiate the virgl feature, read the virgl
capset, create a context and resource, attach backing, submit a command, and
observe a completion fence. It does not yet prove Xorg glamor, a real EGL/GLES
pixel hash, or Firefox WebRender.

## No-3D control run

The same kernel and boot disk were run once with the registered `drm-gem`
device set. The control result also passed, but reported no 3D capability and
refused the submission:

```json
{
  "backing_us": 0,
  "caps_bytes": 0,
  "expected_3d": 0,
  "passed": true,
  "reported_3d": 0,
  "reported_capsets": 0,
  "resource_bo": 0,
  "resource_res": 0,
  "resource_us": 1969,
  "submit_us": 1016
}
```

The control serial receipt includes `virgl 3D unavailable` and
`DRM_VIRGL_SUBMIT PASS refused=1`. This pair of runs demonstrates that the
device selection changes the guest capability result instead of the probe
unconditionally claiming 3D.

