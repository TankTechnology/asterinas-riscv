# QEMU U-Boot handoff with current kernel

This is the minimal host/QEMU isolation requested after r13. It uses the current kernel and U-Boot profile with marker initramfs only; it does not mount the Debian desktop rootfs, start DRM, or start Firefox.

Gate: `megrez-sv48-svade-fast` / `uboot-booti` / `headless`

Result: **PASS**

- U-Boot loaded the current kernel, DTB and marker initramfs.
- `KernelHandoff` at 2.213 s.
- `KernelEarly` at 2.215 s.
- `UserspaceReady` and marker at 7.548 s.
- QEMU cleanup completed; no reboot or surviving process.

Artifacts:

- serial: `/mnt/shared/xaj/Program/asterinas/target/qemu-uboot/current-kernel-probe-20261019/serial.log`
- result: `/mnt/shared/xaj/Program/asterinas/target/qemu-uboot/current-kernel-probe-20261019/result.json`
- marker: `/mnt/shared/xaj/Program/asterinas/target/qemu-uboot/current-kernel-probe-20261019/marker-event.txt`

| Artifact | SHA-256 |
|---|---|
| serial.log | `c1956ae2b89b538362dff4c24b7df0bb9e84e52303f065a1317d2db75683e92b` |
| result.json | `80e41f303354a18f51382c8e18984126af8836bb0aabfba227aca6cbca8b718c` |
| marker-event.txt | `f704813256f207f5d3020d7a671a2379b0b4c91a97eb0b1f5a97da1e1d72ceae` |
| boot.ext4 | `8840800cd35dacb0a569499c834ebb235b26a7c9cf7ec29654f180e8446ef6ed` |
| kernel | `abeeb0faa1beb9b7ef4de1aee0b5be265c77f9dd1f65ea07e11aa6586952554d` |
| U-Boot | `47516422aea981a45eb3da6de786d356dbd9059c96bd9fc11d8e3ba28aec7210` |

This proves the current kernel is handoff-bootable through U-Boot. The r13 failure therefore remains specific to the full Debian/DRM boot path or its protocol timing, not a generic kernel/U-Boot handoff failure.
