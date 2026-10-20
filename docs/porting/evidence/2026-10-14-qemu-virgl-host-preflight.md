# QEMU virgl host preflight — 2026-10-14

This is a host capability check only. It does not launch Asterinas and does
not touch the development board.

## Contract

The 3D profile is allowed to run only when the selected QEMU reports both:

* `egl-headless` in `qemu-system-riscv64 -display help`; and
* `virtio-gpu-gl-device` in `qemu-system-riscv64 -device help`.

The checker is `tools/riscv/drm/virgl_host_preflight.py`. Missing executables,
failed help queries, and either missing capability fail closed. This prevents a
plain `virtio-gpu-device` or an Xorg `llvmpipe` session from being reported as
hardware acceleration.

## Observed host state

The default `qemu-system-riscv64` is not installed in the host PATH. The
available external build at
`/home/ubuntu/.codex/qemu-eic7700-10.2.1/build/qemu-system-riscv64` cannot be
started on this host because it requires `GLIBC_2.38`; the host libc does not
provide that version. Both invocations therefore fail at `--version` before
any guest or hardware action:

```text
{"has_egl_headless": false, "has_virtio_gpu_gl": false,
 "passed": false, "qemu": "qemu-system-riscv64",
 "reason": "qemu --version failed: FileNotFoundError: ..."}
{"has_egl_headless": false, "has_virtio_gpu_gl": false,
 "passed": false,
 "qemu": "/home/ubuntu/.codex/qemu-eic7700-10.2.1/build/qemu-system-riscv64",
 "reason": "qemu --version failed: ... GLIBC_2.38 ..."}
```

The existing DRM/Xorg screenshot evidence remains valid for the 2D modesetting
path, but its renderer is `llvmpipe`; it is not 3D acceleration evidence.

The persistent development container does have QEMU 10.2.1 at
`/usr/local/qemu/bin/qemu-system-riscv64`, but the same checker reports
`QEMU lacks egl-headless`. Its device list contains only
`virtio-gpu-device`/`virtio-gpu-pci`, with no `virtio-gpu-gl-device`. Therefore
the 3D gate is intentionally not launched in this environment.

## GL-enabled build

The QEMU source was rebuilt in the dedicated Ubuntu 24.04 builder after
installing `libvirglrenderer-dev`, `libepoxy-dev`, `libgbm-dev`, `libdrm-dev`,
`libegl1-mesa-dev`, `libfdt-dev`, `libglib2.0-dev`, `libpixman-1-dev`, Meson
and Ninja. The resulting QEMU 10.2.1 reports:

```text
Available display backend types:
none
egl-headless
name "virtio-gpu-device", bus virtio-bus
name "virtio-gpu-gl-device", bus virtio-bus
name "virtio-gpu-gl-pci", bus PCI, alias "virtio-gpu-gl"
```

For QEMU 10.2, the correct backend is `-display egl-headless`; it does not
accept `egl-headless,gl=on`. A privileged host-only smoke run with
`-device virtio-gpu-gl-device` and
`-display egl-headless,rendernode=/dev/dri/renderD129` stayed alive through the
bounded initialization window without an EGL error. This proves that the GL
QEMU build and render-node permissions can be made usable; it does not yet
prove an Asterinas guest render or Firefox WebRender result.

Build/runtime logs are retained under `/tmp/qemu-gl-build-check4-20261014-sQX770`
and `/tmp/qemu-gl-runtime-check-20261014-BD9iCY`.
