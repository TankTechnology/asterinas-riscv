# Firefox NewSession probe r52

## Scope

One bounded probe-only launch using the retained Debian trixie Firefox ESR
rootfs and the current Asterinas kernel artifact. This is not a full Firefox
scene gate and did not touch real hardware.

Command (inside `asterinas-qemu-gl-env-r2`):

```text
python3 tools/riscv/debian/rootfs/desktop_m4_drm_gate.py \\
  --kernel target/osdk/aster-kernel-osdk-bin.Image \\
  --uboot target/qemu-uboot/cache/u-boot-build/u-boot \\
  --dtb target/qemu-uboot/debian-root/qemu-virt.dtb \\
  --stage1-initramfs target/debian-riscv/stage1-procfix-20261017/initramfs.cpio \\
  --root-image /mnt/mydisk/asterinas-firefox-rootfs-20261019-r49/debian-root.ext2 \\
  --root-manifest /mnt/mydisk/asterinas-firefox-rootfs-20261019-r49/rootfs-manifest.json \\
  --packages-lock /mnt/mydisk/asterinas-firefox-rootfs-20261019-r49/packages.lock \\
  --package-checksums /mnt/mydisk/asterinas-firefox-rootfs-20261019-r49/source-metadata/package-checksums \\
  --output-directory /mnt/mydisk/qemu-firefox-scheduler-r52b \\
  --boot-timeout 180 --command-timeout 30 --cleanup-timeout 30
```

The first attempt was stopped in host preflight because the `current` DTB had
only two enabled CPUs. It did not start QEMU. The recorded run used the
retained four-CPU DTB under `target/qemu-uboot/debian-root/`.

## Result

| Boundary | Observation | Pass |
|---|---|---:|
| U-Boot status/done markers | all eight U-Boot setup markers passed | yes |
| Boot handoff marker | `__ASTERINAS_DESKTOP_BOOT_46B133788D34C943__` printed | yes |
| Kernel entry | `Starting kernel ...` printed | yes |
| Asterinas/Stage1 marker | no marker after kernel entry during 180 s | no |
| Firefox/Marionette | never reached; no NewSession request | no |
| Recovery | QEMU was terminated by the bounded gate timeout; no guest recovery marker | no |

The run therefore stops at the kernel/Stage1 handoff boundary. It provides no
evidence for or against the futex hypothesis and no WebRender result. The
current kernel artifact used by this run is SHA-256
`245d54df230f0d8878411d1efd982d5e4189d469f6172379c6c246f56c9fba1e`, which is
different from the artifact hash recorded in r51; the two runs must not be
conflated.

Artifacts:

- `/mnt/mydisk/qemu-firefox-scheduler-r52b/desktop-m4-drm.serial.log`
- `/mnt/mydisk/qemu-firefox-scheduler-r52b/lifecycle.json`
- `/mnt/mydisk/qemu-firefox-scheduler-r52b/result.json`

No further Firefox probe was started after this bounded failure.
