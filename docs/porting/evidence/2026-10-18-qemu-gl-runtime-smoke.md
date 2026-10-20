# QEMU GL runtime smoke (2026-10-18)

The existing capability preflight only inspected `-display help` and
`-device help`. This bounded smoke adds the missing runtime assertion: launch
an empty RISC-V machine with `egl-headless,gl=on` and
`virtio-gpu-gl-device`; staying alive until the deadline is success, while an
early exit is failure. No guest kernel, rootfs, Firefox, or physical board is
used.

## Matched launcher

`tools/riscv/drm/qemu_virgl_env.sh` sets `LD_LIBRARY_PATH` and
`QEMU_MODULE_DIR` for the existing QEMU package. It does not copy or rebuild
the package for each run.

## Verification

Static tests:

```text
python3 -m unittest tools/riscv/tests/test_drm_virgl_host_preflight.py
......
----------------------------------------------------------------------
Ran 6 tests in 0.121s

OK
```

Runtime command:

```text
python3 tools/riscv/drm/virgl_host_preflight.py \
  --qemu tools/riscv/drm/qemu_virgl_env.sh \
  --runtime-smoke --runtime-timeout 5
```

The capability portion passed, but the runtime portion failed closed:

```json
{
  "capability": {
    "has_egl_headless": true,
    "has_virtio_gpu_gl": true,
    "passed": true,
    "version": "QEMU emulator version 6.2.0 (Debian 1:6.2+dfsg-2ubuntu6.31)"
  },
  "runtime_smoke": {
    "passed": false,
    "reason": "QEMU GL runtime exited before the smoke deadline: qemu-system-riscv64: OpenGL is not supported by the display",
    "returncode": 1,
    "timed_out": false
  }
}
```

This closes the false-positive path in the host preflight. The DRM desktop
gate now runs this smoke after the capability check and fails before guest
startup when it does not pass. The current QEMU 6.2 package advertises the GL
device but cannot initialise its EGL display on this host, so it must not be
used for the Firefox or guest virgl gates. A matched QEMU/virgl/EGL runtime is
the next host prerequisite before another E3 guest run.

## Artifacts

```text
tools/riscv/drm/virgl_host_preflight.py
  cf3e7239b7e8847f1ef5751234eaa7d2e7ffa778d31d760ba4546eebbf2fd31d
tools/riscv/drm/qemu_virgl_env.sh
  e837ae5ca6b30fa5fcf45b23e24d9cf4bdb9ce215584334b63d69a74d0c02977
tools/riscv/tests/test_drm_virgl_host_preflight.py
  f513a07a588bdda64cbc5394d19757af7d83bf587585006b5f374792cf81d562
tools/riscv/debian/rootfs/desktop_m4_gate.py
  f1cf3c9ad8003246341eb6bcf4f560db97d53e9a0f57f841242a9c46462884ba
tools/riscv/tests/test_debian_rootfs.py
  dc4c9485bd0e9291c70c8b7825c9d87a0c28c1016137550db121ca54bdf8848e
```

