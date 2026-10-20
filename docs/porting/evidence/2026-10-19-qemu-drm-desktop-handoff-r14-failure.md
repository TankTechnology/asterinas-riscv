# QEMU DRM desktop handoff isolation r14

r14 used the current kernel, the GL-capable QEMU device set, and a Debian desktop rootfs whose Firefox service was replaced with a no-op. The host gate ran with `ASTERINAS_DESKTOP_SKIP_FIREFOX=1`; it therefore waited only for the desktop/DRM path and did not start Firefox.

Result: **FAIL / protocol boundary**.

- U-Boot loaded kernel, DTB and Stage1 successfully.
- QEMU used `-display egl-headless,rendernode=/dev/dri/renderD129` and `virtio-gpu-gl-device`.
- The serial trace ended at `Starting kernel ...`.
- No `ASTERINAS_BOOT_PHASE`, Stage1, DRM, systemd, Xorg, or recovery marker appeared within the 600-second bound.
- Firefox was disabled in the rootfs; this is not a Firefox result.

Artifacts:

- output group: `/mnt/mydisk/qemu-desktop-handoff-r14-container/`
- serial SHA-256: `a240c969a6acd065840f8619af666a883566d5327e25af9ed88558fd25a6e60a`
- result SHA-256: `0c12e8571010e25f9fcf58c665100c14b8e769afdebce21eb941bc9fe4da82f6`
- lifecycle SHA-256: `0dca1715f6128632925c70bf34fbb7372d59f83cacedbf23b22f417fcfd3cab0`
- kernel SHA-256: `abeeb0faa1beb9b7ef4de1aee0b5be265c77f9dd1f65ea07e11aa6586952554d`
- rootfs SHA-256: `f9fc59fdc62a6934fc26b448a7c32a0bbf4593eb440989a319c406ad2ca97581`

This isolates the remaining failure to the early kernel path when the GL virtio GPU and DRM provider are present. The generic U-Boot/current-kernel marker handoff remains passing.
