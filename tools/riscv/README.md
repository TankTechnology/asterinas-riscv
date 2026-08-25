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

## Linux Test Project syscall gate

The isolated LTP gate cross-builds the pinned LTP `20260529` syscall suite,
boots it through the guarded U-Boot runner,
and stores its evidence below `target/ltp/`.
It does not reuse `target/qemu-uboot/current`,
so running it cannot replace the prepared desktop boot disk.

Run its host tests with:

```bash
make test_riscv_ltp_unit
```

After building the RISC-V kernel and LTP initramfs,
record a baseline with:

```bash
python3 tools/riscv/ltp_gate.py run \
  --kernel target/osdk/aster-kernel-osdk-bin.Image \
  --smp 1 --run-id baseline-m1-smp1 --skip-build --baseline
```

See [the LTP gate operator guide](ltp/README.md)
for the exact containers, strict mode, SMP=4 runs, result schema,
and evidence-provenance rules.

## PCI xHCI USB keyboard gate

The PCI xHCI gate boots QEMU `virt` in Sv39 mode with `smp=4`, one PCI
`qemu-xhci` controller, and one USB HID boot keyboard. It proves the
DT-routed INTx, xHCI, USB enumeration, input-core, and evdev path with an exact
press/release sequence and no VirtIO or i8042 keyboard fallback.

Run its host tests with:

```bash
make test_riscv_xhci_input_unit
```

See [the PCI xHCI keyboard operator guide](xhci/README.md) for the Sv39 build,
private U-Boot disk, bounded QEMU command, evidence schema, verified M1 hashes,
and the physical-board limitations.

## Debian persistent root

The Debian M1 gate validates a signed Trixie `riscv64` ext2 root, hands off
from a minimal stage-1 initramfs into Debian `/bin/bash`, and boots the same
writable root twice to prove persistence. It is an infrastructure gate: four
harts, Sv39, two VirtIO block devices, no network, display, USB, or input.

Run its local contract tests with:

```bash
make test_riscv_debian_rootfs_unit
```

See [the Debian persistent-root operator guide](debian/rootfs/README.md) for
the signed root build, current-main kernel/U-Boot/DTB/stage-1 preparation,
explicit two-boot target, and evidence inspection commands.

## VirtIO-GPU hardware cursor gate

The DRM R1 gate boots current-main Asterinas with the generic Sv39, SMP=4
profile and one `virtio-gpu-device`. Its guest performs a 64x64 Cursor2 set,
legacy cursor move, and cursor hide. A pass requires the guest markers and the
QEMU VirtIO cursor traces in the exact order; networking, USB, and input-device
fallbacks are absent.

Run the host contract tests with:

```bash
make test_riscv_drm_cursor_unit
```

Build the dedicated initramfs and a matching Sv39 kernel, then prepare a
private U-Boot disk:

```bash
tools/riscv/drm/build_cursor_gate.sh \
  target/qemu-uboot/drm-cursor/initramfs.cpio.gz
make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode

ASTERINAS_RISCV_BOOTI="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
ASTERINAS_INITRAMFS="$PWD/target/qemu-uboot/drm-cursor/initramfs.cpio.gz" \
QEMU_UBOOT_PROFILE=generic-sv39-drm-cursor-smp4 \
QEMU_UBOOT_OUT_DIR="$PWD/target/qemu-uboot/drm-cursor/prepared" \
QEMU_UBOOT_BUILD_DIR="$PWD/target/qemu-uboot/cache/u-boot-build" \
tools/riscv/prepare_qemu_uboot_booti.sh prepare
```

Run the bounded evidence gate:

```bash
make test_riscv_drm_cursor \
  DRM_CURSOR_UBOOT="$PWD/target/qemu-uboot/cache/u-boot-build/u-boot" \
  DRM_CURSOR_BOOT_DISK="$PWD/target/qemu-uboot/drm-cursor/prepared/boot.ext4" \
  DRM_CURSOR_MANIFEST="$PWD/target/qemu-uboot/drm-cursor/prepared/artifacts.json" \
  DRM_CURSOR_GATE_OUTPUT="$PWD/target/qemu-uboot/drm-cursor/evidence"
```

This gate proves the current-main VirtIO transport and DRM cursor ioctl path;
it is not evidence for the Megrez display controller or physical scanout.

## Megrez SDHCI read-only evidence

The Megrez SDHCI gate classifies a bounded Asterinas serial transcript. It
requires the EIC7700 removable-card controller, a nonzero SDHC capacity,
read-only `mmcblk0` registration, and a partition-table SHA-256 marker in that
order. Panic, fatal, probe-failure, writable, duplicate, and out-of-order
evidence is rejected. Linux boot output is not an accepted substitute.

Run the host tests with:

```bash
python3 -m unittest tools.riscv.tests.test_megrez_sdhci_gate -v
```

After a real Asterinas board run has produced the partition hash marker,
publish the complete log and atomic JSON result with:

```bash
python3 tools/riscv/megrez_sdhci_gate.py \
  --transcript /absolute/path/to/megrez.serial.log \
  --output-dir /absolute/path/to/evidence
```

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

## LVGL desktop GUI (framebuffer interactive demo)

Build a static riscv64 `/init` that renders an interactive keyboard-navigable
desktop through LVGL on `/dev/fb0` (Home screen with three app cards, arrow
keys move focus, Enter opens, ESC returns), then packs it into the marker
initramfs. Verified on the QEMU virt display chain (bochs ->
simple-framebuffer -> VT -> userspace fbdev).

```bash
python3 tools/riscv/lvgl/build_lvgl_initramfs.sh   # -> target/qemu-uboot/initramfs-lvgl.cpio.gz
```

Then rebuild the boot disk with that initramfs and run the display boot
(see the "QEMU framebuffer display boot" section). The build clones LVGL
`v8.3.9` + lv_drivers `v8.3.0` into `target/lvgl` and applies the needed
patches (32-bit colors, resolution caps, fonts, enabled fbdev/evdev,
non-fatal FBIOBLANK). Compile in one gcc invocation to avoid
stale-object-color-depth mismatches. See `docs/porting/riscv-qemu-desktop.md`
for the interactive-GUI input verification status.

## Dependencies

Use the repository development container.
Preparing U-Boot additionally needs the RISC-V cross compiler, `dtc`, OpenSSL/GnuTLS development packages, and the Python development headers and `setuptools` used to build `pylibfdt`.
The unit tests use only the Python standard library and repository files.

Profiles are reviewed code objects; command-line CPU, memory, bootarg, and resource-gate overrides are intentionally restricted.
Add a new machine by defining its contract and tests instead of adding board-specific branches to the runner.
