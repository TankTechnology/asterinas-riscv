# QEMU virgl submit-fault gate r1

Date: 2026-10-17

## Scope

This is one bounded QEMU-only failure leg for the virgl submission contract.
The guest was run in the pinned GL-capable image with networking disabled. The
board, physical serial port, and hardware MMIO were not used.

## Change under test

The minimal virgl initramfs did not mount `/proc`, so a userspace probe could
not obtain the kernel command line from `/proc/cmdline`. The profile now places
the selector after the kernel `--` delimiter, and the probe reads the forwarded
init argument (`asterinas.virtio_gpu_fault=submit`). The selector parser has
native self-tests for context, submit, fence, invalid, duplicate, and prefix
false-positive cases.

## Reproducible launch

The run used `asterinas/qemu-gl-env:20261014` (digest
`sha256:de41e7c840a4861d654dd80996c0e9ce99d22eb08c37996860500c75b9e203b4`)
and its internal `/root/qemu-gl-src/build` QEMU, with `--network=none` and
`--device /dev/dri/renderD129`:

```text
docker run --rm --network=none --device /dev/dri/renderD129 \
  -v /mnt/shared/xaj/Program/asterinas:/root/asterinas:ro -v /tmp:/tmp \
  -w /root/asterinas asterinas/qemu-gl-env:20261014 sh -lc '
    export PATH=/root/qemu-gl-src/build:$PATH
    PYTHONPATH=tools/riscv python3 tools/riscv/drm/virgl_fault_gate.py \
      --fault submit \
      --uboot /root/asterinas/target/qemu-uboot/drm-current-virgl-20261015/u-boot \
      --boot-disk /tmp/asterinas-virgl-fault-build5-current/fault-boot.ext4 \
      --manifest /tmp/asterinas-virgl-fault-manifest4-current.json \
      --output-directory /tmp/asterinas-virgl-submit-fault-fixed7-1792188400889
  '
```

The pinned artifacts were:

| input | SHA-256 |
| --- | --- |
| U-Boot | `3e5ae37dcd7071a274780414bc37c74ac1df12b916d77532b290e4eb61a5fc80` |
| boot disk | `87618cc68019635bb62f52e75ab0d3d23d73855eecd06906cfcd5c1df54089ca` |
| manifest | `38856033f57f0c61ec0591ed8baab167f0783d6a41115c79aea19d72c5a7b5dd` |
| initramfs payload | `90b3013203bc1e40188ae67706f8eefd7585586c6912722d34025766d9014ee7` |

## Receipt

```json
{
  "passed": true,
  "reason": "fault marker is bounded and correctly classified",
  "recovery_complete": true,
  "reported_stage": "submit",
  "requested_fault": "submit",
  "serial_sha256": "226b6b221147fe59118adf46fa81075b8f0a357d9705e86ca681d47800e1a2e1"
}
```

The serial transcript contains exactly one diagnostic pair:

```text
ASTERINAS_SOFTWARE_REBOOT_ARMED seconds=3
DRM_VIRGL_FAIL stage=submit errno=0
DRM_VIRGL_FAULT stage=submit
OpenSBI v1.7
U-Boot 2026.07 (Oct 13 2026 - 17:16:34 +0000)
```

The persisted raw receipt files are in
`2026-10-17-qemu-virgl-submit-fault-r1/` beside this document. Their SHA-256
values are recorded by the directory's `result.json`, `boot-result.json`,
`serial.log`, and `marker-event.txt` files.

The process exited with status `0`. This closes the QEMU submit-fault leg and
its watchdog/software-reboot recovery check. It does not claim a physical
board result, Mesa/Xorg acceleration, or Firefox WebRender support.
