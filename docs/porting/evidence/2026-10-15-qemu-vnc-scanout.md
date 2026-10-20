# QEMU VNC scanout gate (2026-10-15)

This is a host-only display-backend qualification. It does not touch the
development board and does not claim that the normal Xorg client scene has
been painted.

The custom QEMU used by the RISC-V gate does not provide GTK or SDL display
backends. The gate therefore uses its built-in private Unix-socket VNC
backend:

```text
-display vnc=unix:/tmp/.../display.vnc
-device bochs-display,xres=1280,yres=1024
```

The U-Boot handoff executes `pci display 0.1.0` before installing the
`simple-framebuffer` node. The guest sets a deterministic three-band pattern
through `/dev/fb0`, holds it while the host issues QEMU HMP `screendump`, and
then restores the saved framebuffer bytes. This separates the scanout
transport from the timing of Xorg client first paint.

## Result

Command:

```text
PYTHONPATH=. python3 tools/riscv/debian/rootfs/desktop_m4_scanout_gate.py \
  --kernel target/qemu-uboot/display-safe-smp4-20261015/fs-root/asterinas.booti \
  --uboot target/qemu-uboot/cache/u-boot-build/u-boot \
  --dtb target/qemu-uboot/display-safe-smp4-20261015/fs-root/qemu-virt.dtb \
  --stage1-initramfs target/qemu-display-safe/stage1-initramfs.cpio \
  --root-image target/desktop-m4-current-overlay-20261015-v2/rootfs/debian-root.ext2 \
  --root-manifest target/desktop-m4-current-overlay-20261015-v2/rootfs/rootfs-manifest.json \
  --packages-lock target/desktop-m4-current-overlay-20261015-v2/rootfs/packages.lock \
  --package-checksums target/desktop-m4-current-overlay-20261015-v2/rootfs/source-metadata/package-checksums \
  --output-directory target/qemu-display-scanout-pattern-vnc-20261015 \
  --boot-timeout 180 --command-timeout 30 --cleanup-timeout 30
```

The gate passed. The reviewable artifacts are:

- `target/qemu-display-scanout-pattern-vnc-20261015/desktop-m4-scanout.serial.log`
- `target/qemu-display-scanout-pattern-vnc-20261015/desktop-m4-scanout.ppm`
- `target/qemu-display-scanout-pattern-vnc-20261015/result.json`

The result records a 1280x1024 PPM with 1,310,720 pixels, 3 sampled colors,
874,496 pixels different from the background, and these hashes:

```text
pixel_sha256=44b1d582f4d56d27b4c1715bfbc317f01dd2f8ed3c0c5742f2c2bda30f4b6b44
ppm_sha256=3fc3cfaebf37f1f6000c34ac9f344f7673360d09102f1d70a2c38a7b4dd97105
```

The serial receipt also contains the guest framebuffer metadata and the exact
write/restore markers. The normal desktop scene remains a separate gate: a
black initial Xorg frame is not treated as a transport failure when the
deterministic scanout transaction passes.
