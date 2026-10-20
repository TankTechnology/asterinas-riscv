# QEMU DRM scanout runtime gate r2

Date: 2026-10-17

## Scope

This is a single isolated DRM scanout runtime gate. QEMU ran inside the local
GL image with networking disabled. U-Boot, the boot disk, and the manifest were
mounted read-only; gate output was written to a temporary host directory. No
physical board, serial port, or Firefox was used.

## Reproducible command

```text
docker run --rm --network=none --device /dev/dri/renderD129 \
  -v /mnt/shared/xaj/Program/asterinas:/root/asterinas:ro \
  -v /mnt/shared/xaj/Program/asterinas/target/qemu-uboot/drm-current-scanout-20261015/u-boot:/inputs/u-boot:ro \
  -v /mnt/shared/xaj/Program/asterinas/target/qemu-uboot/drm-current-scanout-20261015/boot.ext4:/inputs/boot.ext4:ro \
  -v /mnt/shared/xaj/Program/asterinas/target/qemu-uboot/drm-current-scanout-20261015/artifacts.json:/inputs/artifacts.json:ro \
  -v "$RECEIPT_DIR:/out:rw" \
  -w /root/asterinas asterinas/qemu-gl-env:20261014 sh -lc \
  'export PATH=/root/qemu-gl-src/build:$PATH PYTHONPATH=/root/asterinas/tools/riscv; \
   python3 tools/riscv/drm/scanout_gate.py \
     --uboot /inputs/u-boot --boot-disk /inputs/boot.ext4 \
     --manifest /inputs/artifacts.json --output-directory /out \
     --device-set drm-scanout'
```

Image: `asterinas/qemu-gl-env:20261014`,
`sha256:de41e7c840a4861d654dd80996c0e9ce99d22eb08c37996860500c75b9e203b4`.

Input hashes:

| input | bytes | SHA-256 |
| --- | ---: | --- |
| U-Boot | 9,142,960 | `3e5ae37dcd7071a274780414bc37c74ac1df12b916d77532b290e4eb61a5fc80` |
| boot disk | 67,108,864 | `fd769cc7b537a5a9ab19f33c7dca9ae6099a10dd6b8f5e04083c5d56ab18f58a` |
| artifact manifest | 419 | `2ef633f50ecec36d53f29b79565cbb709d99bba5e19f7d8d93dab229aec46ac9` |

## Receipt

```json
{
  "device_set": "drm-scanout",
  "display_backend": "none",
  "passed": true,
  "reason": "passed",
  "screenshot_audit": {
    "bounding_box": [0, 0, 1279, 799],
    "distinct_colors_lower_bound": 3,
    "height": 800,
    "max_value": 255,
    "non_black_pixels": 1023935,
    "passed": true,
    "width": 1280
  },
  "screenshot_sha256": "50adbf5d9bd46e1afd982876b5bd7efda93a77583be29adbfb2d36b5ad252e2d",
  "trace_count": 5
}
```

The gate exited with status `0`. This closes one fresh DRM scanout runtime
leg: device discovery, resource setup, SETCRTC, transfer, flush, readback, and
the 1280×800 screenshot audit passed. It does not claim Xorg glamor or Firefox
WebRender acceleration.

