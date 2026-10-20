# QEMU virgl context and fence fault gates r1

Date: 2026-10-17

## Scope

This closes the remaining two registered virgl fault stages after the submit
stage was fixed and passed. Each stage ran as a separate bounded QEMU process
with networking disabled, the GL render node explicitly passed through, and the
boot inputs mounted read-only. No physical board, serial port, or hardware
MMIO was used.

## Reproducible launch

Both runs used `asterinas/qemu-gl-env:20261014` (digest
`sha256:de41e7c840a4861d654dd80996c0e9ce99d22eb08c37996860500c75b9e203b4`)
and its internal `/root/qemu-gl-src/build` QEMU. The only change between runs
was `--fault context` versus `--fault fence`:

```text
docker run --rm --network=none --device /dev/dri/renderD129 \
  -v /mnt/shared/xaj/Program/asterinas:/root/asterinas:ro -v /tmp:/tmp \
  -w /root/asterinas asterinas/qemu-gl-env:20261014 sh -lc '
    export PATH=/root/qemu-gl-src/build:$PATH
    PYTHONPATH=tools/riscv python3 tools/riscv/drm/virgl_fault_gate.py \
      --fault <context|fence> \
      --uboot /root/asterinas/target/qemu-uboot/drm-current-virgl-20261015/u-boot \
      --boot-disk /tmp/asterinas-virgl-fault-build5-current/fault-boot.ext4 \
      --manifest /tmp/asterinas-virgl-fault-manifest4-current.json \
      --output-directory /tmp/<run-output>
  '
```

The shared inputs are unchanged from the submit-fault receipt:

| input | SHA-256 |
| --- | --- |
| U-Boot | `3e5ae37dcd7071a274780414bc37c74ac1df12b916d77532b290e4eb61a5fc80` |
| boot disk | `87618cc68019635bb62f52e75ab0d3d23d73855eecd06906cfcd5c1df54089ca` |
| manifest | `38856033f57f0c61ec0591ed8baab167f0783d6a41115c79aea19d72c5a7b5dd` |
| initramfs payload | `90b3013203bc1e40188ae67706f8eefd7585586c6912722d34025766d9014ee7` |

## Receipts

| fault | result | reported marker | recovery | serial SHA-256 |
| --- | --- | --- | --- | --- |
| context | `passed=true` | `context-init` | `true` | `cd2e5afb97b848eb4c467f20a108123ba1fd2fdd93e57a16c4bba9178b1661eb` |
| fence | `passed=true` | `fence` | `true` | `b61147a60ccb401a4099e6eff6496275c64fc5a4af4bb9d21987d3412781ed9c` |

The context transcript contains:

```text
ASTERINAS_SOFTWARE_REBOOT_ARMED seconds=3
DRM_VIRGL_FAIL stage=context-init errno=22
DRM_VIRGL_FAULT stage=context-init
OpenSBI v1.7
U-Boot 2026.07 (Oct 13 2026 - 17:16:34 +0000)
```

The fence transcript contains:

```text
ASTERINAS_SOFTWARE_REBOOT_ARMED seconds=3
DRM_VIRGL_SUBMIT PASS refused=0
DRM_VIRGL_FAIL stage=fence errno=0
DRM_VIRGL_FAULT stage=fence
OpenSBI v1.7
U-Boot 2026.07 (Oct 13 2026 - 17:16:34 +0000)
```

Raw receipts are archived in
`2026-10-17-qemu-virgl-context-fault-r1/` and
`2026-10-17-qemu-virgl-fence-fault-r1/`. Together with the submit-fault
receipt, all three registered virgl fault stages now have independent QEMU
fail-closed and recovery evidence. This does not claim Mesa/Xorg, Firefox, or
physical-board acceleration.
