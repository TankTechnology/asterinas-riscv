# QEMU GL runtime smoke r2 (2026-10-18)

This is the corrected host smoke after r1 exposed two launch mistakes:

1. `-nographic` overrides the selected EGL display with `none`.
2. The host NVIDIA EGL loader cannot initialise the AMD render node; the
   existing fixed QEMU GL container provides Mesa EGL and the AMD node
   `/dev/dri/renderD129`.

The smoke now uses `-bios none`,
`-display egl-headless,rendernode=/dev/dri/renderD129`,
`-device virtio-gpu-gl-device`, `-S`, `-serial null`, and `-monitor none`.
The process staying alive through the five-second deadline is the pass
condition. No guest kernel, rootfs, Firefox, or board is started.

## Verification

The existing `asterinas/qemu-gl-env:20261014` image was reused in the
persistent container `asterinas-qemu-gl-env-r1`; no image rebuild or rootfs
copy was performed.

```text
ASTERINAS_QEMU_GL_CONTAINER=asterinas-qemu-gl-env-r1 \
python3 tools/riscv/drm/virgl_host_preflight.py \
  --qemu tools/riscv/drm/qemu_virgl_env.sh \
  --runtime-smoke --runtime-timeout 5
```

Result:

```json
{
  "capability": {
    "has_egl_headless": true,
    "has_virtio_gpu_gl": true,
    "passed": true,
    "version": "QEMU emulator version 6.2.0 (Debian 1:6.2+dfsg-2ubuntu6.31)"
  },
  "runtime_smoke": {
    "passed": true,
    "reason": "QEMU GL runtime stayed alive for 5.0s",
    "timed_out": true
  }
}
```

The same environment passed `DesktopM4DrmOperations._validate_qemu_argv`, so
the real DRM desktop gate will now reject an unusable GL host before guest
startup and accept this matched runtime for the next bounded guest run.

Focused host and desktop gate tests: `50 passed`.

## Artifacts

```text
tools/riscv/drm/virgl_host_preflight.py
  8b1930ec8f74cedaecd6f56d89971a734486f3dadcc96697f073f7ed39adf68a
tools/riscv/drm/qemu_virgl_env.sh
  61a69f9bc748dff9856263a26b3d68817777a4e48423c0489beebc44420bc84d
tools/riscv/debian/rootfs/desktop_m4_gate.py
  f1cf3c9ad8003246341eb6bcf4f560db97d53e9a0f57f841242a9c46462884ba
```

