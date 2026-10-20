# Current QEMU DRM GEM gate — 2026-10-15

The current kernel also passes the independent GEM object-space probe on the
same QEMU `virt` SMP=4 profile. The static probe initramfs is
`target/drm-gem/current-20261015-initramfs.cpio.gz`; its manifest records
kernel SHA-256
`35edfd0fa024396eef9d038c029bce6b33612dea64f6216e34ed6abffbcd1db5` and
initrd SHA-256
`986a9d3773ea88807e076afabacbfc688c530127f59b2ab4de4161fee9850a63`.

The gate returned:

```json
{"failed_stage": "", "passed": true, "reason": "passed", "stage_count": 6}
```

Guest evidence, in order:

```text
DRM_GEM_CREATE PASS
DRM_GEM_FLINK PASS
DRM_GEM_OPEN PASS
DRM_GEM_CLOSE PASS
DRM_GEM_REJECT PASS
ASTERINAS_DRM_GEM_R1_READY
```

```text
537083f2e1cd77226891814c76edbe7149db0f8a7508cf7086612aecd8a3e75d  target/qemu-uboot/drm-current-gem-20261015/gem-evidence/serial.log
6334a695df2ac89264590ce1763afcadd146b81b18ad525b8549558086e7e435  target/qemu-uboot/drm-current-gem-20261015/gem-evidence/result.json
```

This covers GEM creation, naming/opening, close, and rejection behavior in
one open `card0` file descriptor. It is a prerequisite for userspace buffer
management; it does not yet cover two independent card files, PRIME
export/import, or bounded ENOMEM behavior, and it does not claim virgl 3D or
Firefox acceleration.
