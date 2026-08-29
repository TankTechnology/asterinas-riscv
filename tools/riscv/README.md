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

## Simulation-first Megrez debug attempt

The Megrez debug workflow binds one immutable Asterinas artifact plan to a
generic Sv39/SMP=4 QEMU fast result before touching the board. The physical
step holds one serial descriptor, verifies the kernel, stage-1 initramfs, and
Megrez DTB already in RAM by exact CRC32, and transfers only cache misses over
XMODEM. It patches the live DTB for the firmware framebuffer and USB host,
runs exactly one `booti`, and waits for the plan's guest marker followed by
the automatic U-Boot recovery prompt.

It never transfers the 1-GiB Debian root image, runs `saveenv`, resets the
board, or boots Linux. A failed or interrupted attempt publishes
`passed:false`; `result.json` is written after the serial and transport
evidence.

```bash
make test_riscv_megrez_debug_board \
  MEGREZ_DEBUG_PLAN="$PWD/target/megrez-debug/debug-plan.json" \
  MEGREZ_DEBUG_DEVICE=/dev/ttyUSB0 \
  MEGREZ_DEBUG_SIMULATION_RESULT="$PWD/target/qemu-uboot/megrez-debug/fast/result.json"
```

The command has one declining timeout, capped at 300 seconds. Reusing RAM is
safe only when U-Boot reports the exact planned size/address CRC; otherwise
the artifact is retransmitted and verified again before `booti`.

For a diagnostic boot that may hang before Asterinas can arm
`asterinas.reboot_after`, add `--hardware-watchdog`. This is an explicit
pre-boot recovery option, not the desktop default:

```bash
PYTHONPATH="$PWD" python3 -m tools.riscv.megrez_debug board \
  /absolute/path/to/plan.json /dev/ttyUSB0 \
  --simulation-result /absolute/path/to/fast/result.json \
  --output-directory /absolute/path/to/physical-evidence \
  --timeout 120 \
  --hardware-watchdog
```

The option follows the EIC7700X TRM's Synopsys DesignWare watchdog contract at
`0x50800000`. Before touching that block, it reads the system-controller clock
gate at `0x51828200` and active-low reset at `0x51828444`, preserves unrelated
bits, deasserts only WDT0 reset when required, and verifies both values again.
It then verifies component type `0x44570120`, selects maximum `TOP=0xf`
(`TORR[7:4]` is reserved on EIC7700X), kicks with `0x76`, enables
interrupt-then-reset mode, and reads the control registers back. Any
prerequisite, type, or readback mismatch aborts before the kernel starts. It
writes neither storage nor U-Boot environment. A watchdog recovery that occurs
after the first current-guest marker but before the terminal marker is reported
immediately as `guest-reboot-before-terminal`; a bare pre-boot prompt is not
mistaken for current-attempt evidence. The host retains its independent
300-second cap even though the current DT describes a 200 MHz watchdog clock.

## Megrez SDHCI read-only evidence

The Megrez SDHCI gate classifies a bounded Asterinas serial transcript. It
requires an aligned 512 KiB SDMA buffer whose CPU and device addresses are
identical inside `0xc0000000..0x100000000`, the EIC7700 removable-card
controller, a nonzero SDHC capacity, and read-only `mmcblk0` registration in
that order. For the physical data-path gate it then requires one exact 32 MiB
read whose CRC32 matches the value measured by U-Boot. That read covers the
partition table and is stronger than the old, never-implemented
`partition-table sha256` log requirement. The identity address is the RockOS
U-Boot handoff contract; Linux's `0x20000000` IOVA requires SMMUv3 SID 16 and
is not usable as a fixed offset while Asterinas RISC-V has no IOMMU. Panic,
fatal, probe-failure, writable, translated, misaligned, duplicate, and
out-of-order evidence is rejected. Linux boot output is not an accepted
substitute.

Run the host tests with:

```bash
python3 -m unittest tools.riscv.tests.test_megrez_sdhci_gate -v
```

After a real Asterinas board run has completed the bounded read, publish the
complete log and atomic JSON result with the U-Boot CRC32 bound explicitly:

```bash
python3 tools/riscv/megrez_sdhci_gate.py \
  --transcript /absolute/path/to/megrez.serial.log \
  --output-dir /absolute/path/to/evidence \
  --expected-crc32 5f85f90e
```

The 2026-08-29 physical gate completed the exact 32 MiB read in 5.195899
seconds with CRC32 `5f85f90e`, then returned to U-Boot through the pre-boot
hardware watchdog. Both the board lifecycle result and this independent SDHCI
classifier reported pass.

## Megrez firmware framebuffer handoff

`megrez_board_session.py` can add the physically established 1920x1080
scanout at `0xfd800000` to the live DTB before `booti`. The change is RAM-only:
the tool never runs `saveenv`, and the default serial-only path remains
unchanged unless `--firmware-framebuffer` is present.

Asterinas currently selects only the first `console=` value for `/dev/console`.
The framebuffer gate therefore requires `console=tty0` to be first. The closed
`firmware-framebuffer` final profile returns success when the serial log has
observed the kernel register the handoff; Debian/systemd output after that is
expected on HDMI rather than on the serial console.

```bash
PYTHONPATH=tools/riscv python3 tools/riscv/megrez_board_session.py /dev/ttyUSB0 \
  --booti ASTERINAS_IMAGE_ON_BOOT_FS \
  --initrd STAGE1_INITRAMFS_ON_BOOT_FS \
  --dtb DTB_ON_BOOT_FS \
  --expected-crc32 booti=8hex,dtb=8hex,initrd=8hex \
  --bootargs "console=tty0 loglevel=info init=/init asterinas.reboot_after=600 -- --root-init=systemd" \
  --firmware-framebuffer \
  --final-profile firmware-framebuffer \
  --yes \
  --log /absolute/path/to/megrez-framebuffer.serial.log
```

This proves the current-main firmware framebuffer registration boundary. It
does not by itself prove Xorg, a desktop session, or native EIC7700 DRM.

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

### Host-side Megrez debugging

Keep builds and QEMU runs in the pinned development container.
On the host, install only the tools that observe the physical serial and Ethernet paths:

```bash
sudo apt-get install -y ethtool iperf3 arping
```

`ethtool` reports the host-side link state, negotiated speed, and error counters.
`arping` separates layer-2/ARP reachability from DNS, TCP, and the browser.
`iperf3` provides a controlled throughput test when a matching guest endpoint has explicitly been installed;
it is not a substitute for the HTTPS browser gate.

Packet decoding and screenshot OCR are optional:

```bash
sudo apt-get install -y tshark tesseract-ocr tesseract-ocr-chi-sim
```

The repository workflow already uses `picocom`/Python for serial I/O,
`tcpdump` for packet capture,
`dtc`/`fdtget` for device trees,
`gdb-multiarch` for debugging,
and e2fsprogs for Debian root images.
Do not install a second host QEMU merely for this workflow:
use the pinned container so that the emulator and cross-toolchain versions remain reproducible.

Verify the host once before a physical session:

```bash
for tool in docker picocom socat tcpdump ethtool iperf3 arping; do
  command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done
id -nG | tr ' ' '\n' | grep -qx dialout
test -r /dev/ttyUSB0 && test -w /dev/ttyUSB0
MEGREZ_HOST_IFACE=${MEGREZ_HOST_IFACE:-enp12s0}
ip -br link show "$MEGREZ_HOST_IFACE"
ethtool "$MEGREZ_HOST_IFACE"
```

The `dialout` check avoids running the serial gate as root.
`ttyUSB0` is the currently validated path;
resolve the actual USB-serial node again after unplugging or re-enumerating the adapter.
Broad passwordless `sudo` access is not required.

Profiles are reviewed code objects; command-line CPU, memory, bootarg, and resource-gate overrides are intentionally restricted.
Add a new machine by defining its contract and tests instead of adding board-specific branches to the runner.
