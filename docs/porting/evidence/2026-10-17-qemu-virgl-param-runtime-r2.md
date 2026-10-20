# QEMU virgl parameter gate r2

Date: 2026-10-17

## Scope

This is one bounded QEMU-only runtime leg for the Task 3 virgl contract. The
guest was launched inside the pinned local GL image with networking disabled.
The repository and all boot inputs were mounted read-only; the gate result was
written to a container tmpfs and disappeared with the disposable container.
No physical board, serial port, host filesystem output, or Firefox was used.

## Reproducible launch

```text
docker run --rm --network=none --device /dev/dri/renderD129 \
  -v /mnt/shared/xaj/Program/asterinas:/root/asterinas:ro \
  -v /mnt/shared/xaj/Program/asterinas/target/qemu-uboot/drm-current-virgl-20261015/u-boot:/inputs/u-boot:ro \
  -v /mnt/shared/xaj/Program/asterinas/target/qemu-uboot/drm-current-virgl-20261016/boot.ext4:/inputs/boot.ext4:ro \
  -v /mnt/shared/xaj/Program/asterinas/target/qemu-uboot/drm-current-virgl-20261016/artifacts.json:/inputs/artifacts.json:ro \
  --tmpfs /run/gate:rw,size=64m \
  -w /root/asterinas asterinas/qemu-gl-env:20261014 sh -lc '
    mkdir -p /run/gate/virgl
    export PATH=/root/qemu-gl-src/build:$PATH PYTHONPATH=/root/asterinas/tools/riscv
    python3 tools/riscv/drm/virgl_param_gate.py \
      --uboot /inputs/u-boot --boot-disk /inputs/boot.ext4 \
      --manifest /inputs/artifacts.json --output-directory /run/gate/virgl
    cat /run/gate/virgl/result.json
  '
```

Image: `asterinas/qemu-gl-env:20261014`,
`sha256:de41e7c840a4861d654dd80996c0e9ce99d22eb08c37996860500c75b9e203b4`.

Input hashes (computed inside the container because the raw directories are
ACL-protected on the host):

| input | bytes | SHA-256 |
| --- | ---: | --- |
| U-Boot | 9,142,960 | `3e5ae37dcd7071a274780414bc37c74ac1df12b916d77532b290e4eb61a5fc80` |
| boot disk | 67,108,864 | `af5f1249ae81b0e463dd3fb3408f1560492f63a3fe25d31c724b775149bbc89f` |
| artifact manifest | 420 | `5714bb56a012e793a02eaa1d39fc2afc39ef9744f5d3e8f5939430e5ce3a84e4` |

## Receipt

```json
{
  "backing_us": 8976,
  "caps_bytes": 308,
  "caps_context_us": 13886,
  "expected_3d": 1,
  "passed": true,
  "reason": "passed",
  "reported_3d": 1,
  "reported_capsets": 2,
  "resource_bo": 1,
  "resource_res": 2,
  "resource_us": 420653,
  "submit_us": 11908
}
```

The process exited with status `0`. This closes one fresh virgl runtime happy
leg: 3D capability discovery, capset reporting, resource creation, backing,
submit, and fence completion all passed in QEMU. It does not claim a Mesa/Xorg
or Firefox WebRender result.

