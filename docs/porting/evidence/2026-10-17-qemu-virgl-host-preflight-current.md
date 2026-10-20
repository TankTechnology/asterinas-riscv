# Current QEMU virgl host preflight

Date: 2026-10-17

## Scope

This is the smallest Task 4 prerequisite check. It inspects the exact QEMU
binary that the virgl gate would launch. It does not launch a guest, access a
DRM device, modify the repository, or touch the development board.

## Reproducible command

The repository was mounted read-only and the container was run without a
network:

```text
docker run --rm --network=none \
  -v /mnt/shared/xaj/Program/asterinas:/root/asterinas:ro \
  -w /root/asterinas asterinas/qemu-gl-env:20261014 sh -lc \
  'PYTHONPATH=tools/riscv python3 tools/riscv/drm/virgl_host_preflight.py \
    --qemu /root/qemu-gl-src/build/qemu-system-riscv64 --json'
```

Container image identity:

```text
asterinas/qemu-gl-env:20261014
sha256:de41e7c840a4861d654dd80996c0e9ce99d22eb08c37996860500c75b9e203b4
```

## Receipt

```json
{"has_egl_headless": true, "has_virtio_gpu_gl": true, "passed": true, "qemu": "/root/qemu-gl-src/build/qemu-system-riscv64", "reason": "passed", "version": "QEMU emulator version 10.2.1"}
```

The command exited with status `0`. This closes the host-side GL-QEMU
selection assertion for this pinned local image. It is not a guest runtime
receipt: the next gate still has to launch the guest with the recorded input
artifacts and prove the virgl markers, pixels, and recovery.

