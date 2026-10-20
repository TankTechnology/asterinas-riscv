# QEMU GL build manifest — 2026-10-16

This is a host-side manifest for the QEMU binary used by the DRM/virgl
receipts. It is a build identity record, not a guest or physical-board
result.

## Binary and capability evidence

```text
QEMU version: 10.2.1
QEMU binary: /root/qemu-gl-src/build/qemu-system-riscv64
QEMU binary SHA-256: d867a49eda903ff33cdc9138b6f1b0596e94c8445b912a9a83c7337ddca1f31a
Display backend: egl-headless
Device type: virtio-gpu-gl-device
virglrenderer: 1.0.0
OpenGL support (epoxy): 1.5.10
GBM: 25.2.8-0ubuntu0.24.04.4
```

The recorded `-display help` output contains `egl-headless`; the recorded
`-device help` output contains `virtio-gpu-gl-device`. The configure summary
reports `VirGL support: YES 1.0.0`, `OpenGL support (epoxy): YES 1.5.10`, and
`GBM: YES 25.2.8-0ubuntu0.24.04.4`.

## Input log hashes

These hashes bind the summary above to the builder output that was inspected:

```text
b609c51a4a7cde3b17b8cb8267dc4774c15d0beed71dc3874e11a2f8c5756bb8  configure.log
5188552714cebf1d8c4d725c8a143e5d21b954a9cbc627bd06cf3c014c395ba4  version.log
bfaa903e2336d6f86359adf6878b905ff97b233268e6de417a0cbce2094d89f9  display.log
7845be229321a17ac3404326d371db0b93bc9a44a898d39017836e4116537db5  device.log
```

The source logs were produced by the dedicated GL builder at
`/tmp/qemu-gl-build-check4-20261014-sQX770/`. The guest runtime receipts bind
this build to the GL desktop through the binary hash above and the explicit
`egl-headless`/`virtio-gpu-gl-device` command line.
