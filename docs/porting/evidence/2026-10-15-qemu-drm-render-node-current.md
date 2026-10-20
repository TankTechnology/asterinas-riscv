# Current QEMU DRM render-node gate — 2026-10-15

This is a fresh runtime receipt for the current kernel and a minimal static
RISC-V probe initramfs. It is independent of the physical board and does not
claim PowerVR or host-GPU acceleration.

## Inputs

```text
kernel:  target/qemu-uboot/drm-current-render-20261015/boot.ext4:/asterinas.booti
initrd:  target/drm-render-node/current-20261015-initramfs.cpio.gz
dtb:     target/qemu-uboot/drm-current-render-20261015/boot.ext4:/qemu-virt.dtb
qemu:    QEMU emulator version 10.2.1, machine virt, SMP=4, TCG
```

The boot-disk manifest records:

```text
kernel sha256=35edfd0fa024396eef9d038c029bce6b33612dea64f6216e34ed6abffbcd1db5
initrd sha256=19e07e4108e46f64fb9771b82bba71bc6cbcecec7452c4cae600ae600143b5a1
dtb sha256=f2bf8e524fe1e4ecb1f2f56665c9827bfe5dd990ffbacbb735ab466e025037d5
```

## Gate result

Command:

```text
PYTHONPATH=tools/riscv:. python3 tools/riscv/drm/render_node_gate.py \
  --uboot target/qemu-uboot/drm-current-render-20261015/u-boot \
  --boot-disk target/qemu-uboot/drm-current-render-20261015/boot.ext4 \
  --manifest target/qemu-uboot/drm-current-render-20261015/artifacts.json \
  --output-directory target/qemu-uboot/drm-current-render-20261015/render-evidence
```

The QEMU runner returned:

```json
{"failed_stage": "", "passed": true, "reason": "passed", "stage_count": 4}
```

The guest serial log contains these ordered markers:

```text
DRM_RENDER_ALLOWED PASS
DRM_RENDER_REFUSED PASS
DRM_RENDER_CARD PASS
ASTERINAS_DRM_RENDER_R1_READY
```

Evidence hashes:

```text
038da68f9129ccfcdac064c8d54e222f241a3063cdf2294104a957e5b6a887fa  target/qemu-uboot/drm-current-render-20261015/render-evidence/serial.log
3618fa4ed9309639f708eaed0cfbfa9ed5b478e1ca9aa073cf3a9734be317e9a  target/qemu-uboot/drm-current-render-20261015/render-evidence/result.json
ead3c1942b2594d73f48f3ae6bc6c65c4f0f499905b6d087ddbeb9bef7727977  target/qemu-uboot/drm-current-render-20261015/artifacts.json
```

The boot runner also reported `passed=true`, one `booti` handoff, one
userspace marker, unchanged boot-disk and manifest hashes before/after, and a
clean QEMU termination. This proves the render-node ioctl permission split and
basic virtio-gpu DRM exposure under the current kernel. It does not yet prove
KMS modesetting, a pixel hash, Mesa/virgl, or Firefox WebRender.
