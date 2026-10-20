# QEMU DRM GEM/PRIME/ENOMEM receipt — 2026-10-16

This is a current-kernel QEMU receipt for the Task 2 object-space contract. It
does not access the physical board and does not claim native EIC7700 GPU
acceleration.

## Inputs

The gate used the generic Sv39/SMP=4 `drm-gem` profile with one plain
`virtio-gpu-device`, no network and no input devices. The prepared payload
manifest is:

```text
kernel sha256=08c7acecb9374b86be24c9ae8f73d8b4d5de9d87f7a05b89c466d99c15bdb2f5
initrd sha256=2c536c2828bc41b67bd6d9c0e6068a7239616effa909fcc304498e449fc4f6b1
dtb sha256=bec4d67f56c846eb059864d8cd67b7ea69f5a802c955e205bcaacc743a3ad1bd
```

The gate was run with:

```text
PYTHONPATH=tools/riscv:.:$PYTHONPATH \
python3 tools/riscv/drm/gem_gate.py \
  --uboot target/qemu-uboot/cache/u-boot-build/u-boot \
  --boot-disk target/qemu-uboot/drm-current-gem-20261016/boot.ext4 \
  --manifest target/qemu-uboot/drm-current-gem-20261016/artifacts.json \
  --output-directory target/qemu-uboot/drm-current-gem-20261016/gem-evidence
```

The classifier returned:

```json
{"failed_stage": "", "passed": true, "reason": "passed", "stage_count": 11}
```

## Guest receipt

The serial markers are ordered and come from one guest run:

```text
DRM_GEM_CREATE PASS
DRM_GEM_FLINK PASS
DRM_GEM_OPEN PASS
DRM_GEM_CLOSE PASS
DRM_GEM_REJECT PASS
DRM_GEM_TWO_FILE PASS
DRM_PRIME_EXPORT PASS
DRM_PRIME_IMPORT PASS
DRM_PRIME_STALE_REJECT PASS
DRM_GEM_ENOMEM PASS allocations=15
ASTERINAS_DRM_GEM_R1_READY
```

The first sequence creates/maps a dumb buffer, names it, reopens it through a
second `card0` file, checks that both handles share the same offset, and proves
that one close does not invalidate the remaining handle. The second sequence
exports a handle as a dma-buf, imports it into the independent file, maps the
imported handle to the same offset, then rejects an import after the exported
descriptor is closed. The final sequence allocates fifteen fixed 4-MiB dumb
buffers and requires the sixteenth allocation to return `ENOMEM`; successful
allocation there would indicate a wrapped or unbounded pool cursor.

Evidence hashes:

```text
af5b5a4bad5d1b7b81055c6bcc2059d7685c85b09daed47bcea548a402d1984e  target/qemu-uboot/drm-current-gem-20261016/gem-evidence/serial.log
2e109fe3e74041eb9d41825ae46699687d3e5a1db936a8fdcc44fb2ab7e9064f  target/qemu-uboot/drm-current-gem-20261016/gem-evidence/result.json
f915edc7f8b679812f5e5ec038ee0af812bdd3ec6de4f7829132d2c8bb953787  target/qemu-uboot/drm-current-gem-20261016/artifacts.json
```

The Task 2 runtime gate is complete for QEMU. The independent `prime.rs`
ktest is not claimed; the lifetime ktest lives in `dri.rs` and the runtime
receipt above covers PRIME sharing. Task 3 transfer directions and the
992/993-byte submission boundary are now closed by
`2026-10-16-qemu-virgl-transfer-boundary.md`; the remaining open QEMU gate is
the Task 5 2D SETCRTC/transfer/flush fault matrix.
