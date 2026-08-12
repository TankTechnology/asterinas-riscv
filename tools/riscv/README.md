# RISC-V firmware boot validation

This directory validates the firmware-to-kernel handoff without modeling a single development board in QEMU.
The same runner describes a machine contract, a boot flow, and the observable milestones expected from the guest.
The registered profiles currently cover QEMU `virt`, SiFive U, Sv39, and the Sv48/Svade/Svadu envelope used by the Megrez integration.

The checks are evidence, not hardware emulation.
A passing profile proves the declared CPU, MMU, DTB, U-Boot `booti`, and userspace contracts.
It does not claim that QEMU reproduces unmodeled clocks, resets, cache controllers, or board peripherals.

## Unit tests

Run the repository-contract tests before launching QEMU:

```bash
make test_riscv_uboot_booti_unit
```

These tests validate immutable profile definitions, address ranges, generated commands, DTB policy, milestone accounting, result classification, cleanup, and artifact identity checks.

## Generic U-Boot `booti`

Build the deterministic marker initramfs, then provide it with a RISC-V Linux Image.

```bash
python3 tools/riscv/make_qemu_uboot_initramfs.py \
  target/qemu-uboot/marker-initramfs.cpio.gz
make test_riscv_uboot_booti \
  ASTERINAS_RISCV_BOOTI="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  ASTERINAS_INITRAMFS="$PWD/target/qemu-uboot/marker-initramfs.cpio.gz"
```

Generated U-Boot, DTB, disk, logs, and JSON evidence stay below `target/`.

## SiFive U

Use the same Asterinas artifacts to validate the SiFive UART path through U-Boot and userspace.

**The kernel must be built in Sv39 mode** (`FEATURES=riscv_sv39_mode`): the
QEMU `sifive_u` machine models only Sv39-capable harts (see the contract's
`mmu_types`), and a default Sv48 image page-faults on the early DTB read.
This is the single most common cause of a failed sifive_u run.

```bash
make kernel TARGET_ARCH=riscv64 FEATURES=riscv_sv39_mode
python3 tools/riscv/make_qemu_uboot_initramfs.py target/qemu-uboot/marker-initramfs.cpio.gz
make test_riscv_sifive_u \
  ASTERINAS_RISCV_BOOTI="$PWD/target/osdk/aster-kernel-osdk-bin.Image" \
  ASTERINAS_INITRAMFS="$PWD/target/qemu-uboot/marker-initramfs.cpio.gz"
```

An optional Linux control run uses the same machine and evidence path.

```bash
make test_riscv_sifive_u_linux_reference \
  RISCV_LINUX_IMAGE=/absolute/path/to/Image \
  RISCV_LINUX_INITRAMFS=/absolute/path/to/initramfs
```

The Asterinas run is accepted only after its userspace marker appears.
Firmware output alone is not sufficient.

## QEMU framebuffer display boot

Boot the kernel through U-Boot with a bochs display, inject a
`simple-framebuffer` DTB node, and watch the VT console render on the QEMU
display. This validates the firmware-framebuffer handoff software chain
(bochs -> U-Boot -> simple-framebuffer -> Asterinas VT) without hardware.
See `docs/porting/riscv-qemu-desktop.md` for the full setup and pitfalls.

The kernel must be Sv39 (`FEATURES=riscv_sv39_mode`) and the initramfs must
be the real marker initramfs (not the nix-build stub). Then run the driver:

```bash
make kernel TARGET_ARCH=riscv64 FEATURES=riscv_sv39_mode
python3 tools/riscv/make_qemu_uboot_initramfs.py target/qemu-uboot/initramfs.cpio.gz
# ... prepare_qemu_uboot_booti.sh prepare (see the doc for env vars)
python3 tools/riscv/qemu_desktop_boot.py             # headless + screendump
python3 tools/riscv/qemu_desktop_boot.py --display-gtk  # open a window
```

## LVGL image display (framebuffer GUI demo)

Build a static riscv64 `/init` that renders a full-screen image through LVGL on
`/dev/fb0`, then packs it into the marker initramfs. Verified to reproduce the
source image 1:1 on the QEMU virt display chain (bochs -> simple-framebuffer
-> VT -> userspace fbdev).

```bash
# default Asterinas title-card image, or pass your own 1280x1024 PNG
python3 tools/riscv/lvgl/build_lvgl_initramfs.sh            # -> target/qemu-uboot/initramfs-lvgl.cpio.gz
python3 tools/riscv/lvgl/build_lvgl_initramfs.sh my-image.png
```

Then rebuild the boot disk with that initramfs and run the display boot
(see the "QEMU framebuffer display boot" section). The build clones LVGL
`v8.3.9` + lv_drivers `v8.3.0` into `target/lvgl` and applies the needed
patches (32-bit colors, resolution caps, enabled fbdev/evdev, non-fatal
FBIOBLANK). Compile in one gcc invocation to avoid stale-object-color-depth
mismatches.

## Dependencies

Use the repository development container.
Preparing U-Boot additionally needs the RISC-V cross compiler, `dtc`, OpenSSL/GnuTLS development packages, and the Python development headers and `setuptools` used to build `pylibfdt`.
The unit tests use only the Python standard library and repository files.

Profiles are reviewed code objects; command-line CPU, memory, bootarg, and resource-gate overrides are intentionally restricted.
Add a new machine by defining its contract and tests instead of adding board-specific branches to the runner.
