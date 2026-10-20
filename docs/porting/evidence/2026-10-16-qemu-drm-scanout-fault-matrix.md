# QEMU DRM 2D scanout fault matrix — 2026-10-16

This is a QEMU-only fail-closed receipt from the current dirty workspace. No
physical serial, MMIO, reboot, or board GPU operation was performed.

## Fixed inputs

```text
qemu: QEMU emulator version 10.2.1, machine virt, SMP=4, TCG
profile: generic-sv39-drm-scanout-fault-{setcrtc,transfer,flush}-smp4
device set: drm-scanout-fault (virtio-gpu-device, no capture output)
boot disk sha256=8dab39832514be6803a1e40543f9b8feaf6eb8555e051a7fd9b583e1460a1dd2
manifest sha256=6beb7f4272f756fa5de3c7a4a4d14141d53d7277b58a7d2dd3471ba8a4f3d45c
dtb sha256=19d379cd587cfaf3c6f8f6842a7bb064217a4c7569258c482527d01aea28afec
kernel sha256=f650597916f0d54668ee953ab60af92936e230afd7b16fa9d2fd1c66c3eb64d3
initrd sha256=387a5607b810d642f8544a7eec76496d1db02a25aa18d742add0181bf5863af8
```

The fault selector is dormant during the boot-time test pattern and is armed
only after initialization, so the negative test reaches the userspace DRM
path. The kernel emits one exact stage marker with an early serial print; the
probe emits the outer ioctl failure marker.

## Results

| Fault point | Result | Serial SHA-256 | Result SHA-256 | Trace boundary |
| --- | --- | --- | --- | --- |
| `setcrtc` | **PASS** | `1e5276e1d9854380efe27db90af6aeac26a6b358fc60528daf43c82670b667c1` | `4086b25d02b4deb2398ddf143a76fa3ac18206e0069799767ee252b5e1fa5e45` | create + attach, then fault before `SET_SCANOUT` |
| `transfer` | **PASS** | `8faea372e1531cd44c003543d9438d8968145b8979e99277b11153fb14d881e0` | `0d639ab60223a062aec64fb2fdb5d3be0c7fc97f7bace58584735188182a0d7b` | `SET_SCANOUT`, then fault before transfer |
| `flush` | **PASS** | `39c21e83b19a0394aecb5b991f8a2aea61b2ff13779244dd249fc2a913368e79` | `9ce132ec9657fc5e99a4e6e42ea913e188714dafb11411c92ecd6a5bcccc077c` | `SET_SCANOUT` + transfer, then fault before flush |

Each leg passed `result.json`, contains one `ASTERINAS_SOFTWARE_REBOOT_ARMED
seconds=3`, one stage-specific `DRM_SCANOUT_FAULT`, one outer
`DRM_SCANOUT_FAIL`, a second OpenSBI/U-Boot banner and a fresh U-Boot prompt.
The classifier rejects duplicate fault markers, fatal markers, a success
marker, or missing recovery.

Evidence directories:

```text
target/qemu-uboot/drm-scanout-fault-20261016/{setcrtc,transfer,flush}/
```

