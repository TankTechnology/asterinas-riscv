# QEMU GL runtime smoke r3 (2026-10-19)

The matched QEMU 10.2.1 build was smoke-tested inside
`asterinas/qemu-gl-env:20261014` with Mesa EGL and `/dev/dri/renderD129`:

```text
capability: egl-headless=true, virtio-gpu-gl-device=true, QEMU 10.2.1
runtime: QEMU GL runtime stayed alive for 3.0s
```

The timeout cleanup was also verified: no `-S` smoke QEMU process remained in
the container after the run. The smoke now uses `-bios none`, null serial and
monitor endpoints instead of `-nographic`, and reaps container children on a
bounded timeout. This is host evidence only; the guest desktop and Firefox
receipts are recorded separately.

Current source hashes:

```text
tools/riscv/drm/virgl_host_preflight.py
  eb3d9965dc9ab8148ee3c3e7f809de18df3d631cbfb7acf2a0caae9a79c94da2
tools/riscv/drm/qemu_virgl_env.sh
  3a16de8c9825b3981df48bd2bb2b5331efc45ef43530e863913ab27a534a1bf0
tools/riscv/debian/rootfs/desktop_m4_gate.py
  d461b3f2db40a99e32762958b49d14074c670a89c3e1f7424992a5b2e3ea0cf6
tools/riscv/debian/rootfs/desktop_m4_drm_gate.py
  3269b3dc7d8196e98896df69e6e1cbe80de3719d850691b744b9906882dfb1c0
```

