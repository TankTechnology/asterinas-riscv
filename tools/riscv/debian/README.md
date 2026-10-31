# Debian direct-boot gate

This directory starts the downstream Debian layer on the upstream-rebase line.
QEMU direct-boots an upstream Asterinas RISC-V kernel with an existing Debian
rootfs and checks the toolchain over the serial console.

The gate does not replace the Stage1/rootfs work. It is the repeatable
checkpoint that keeps the Debian userspace usable while the rest of the
upstream intake is ported. The rootfs builder and its two current profiles are
documented in [`rootfs/README.md`](rootfs/README.md).

## Run

Run inside the Asterinas development container, where `qemu-system-riscv64`
is available:

```bash
make test_riscv_debian_direct \
  TARGET_ARCH=riscv64 \
  DEBIAN_DIRECT_ROOTFS=/absolute/path/to/rootfs.ext2 \
  DEBIAN_DIRECT_DSH_IMAGE=/absolute/path/to/dsh-repo.ext2
```

`DEBIAN_DIRECT_DSH_IMAGE` is optional. The kernel defaults to OSDK's
`target/osdk/asterinas/asterinas-osdk-bin.elf`; override it with
`DEBIAN_DIRECT_KERNEL` when testing a different build.

The gate uses QEMU `snapshot=on` for every disk, so it does not write to the
input images. It checks:

- the kernel reports `riscv64`;
- `gcc` compiles and runs a real C program (not just `gcc -dumpversion`);
- `python3`, `node`, and `git` are usable;
- with a DSH image, the dsh entry point and `dsh --help` are usable.

`init=/bin/bash` starts with an empty environment. The gate exports the same
standard `PATH` a login/systemd session provides before checking the
toolchain; gcc otherwise inherits no `PATH` and cannot locate `cc1` or `ld`.

The serial log is written to `target/riscv-debian-direct.log` by default. If
the image is ext4, pass `--rootfstype ext4` to `direct_boot_gate.py` directly
or use the dedicated ext4 gate below.

## ext4/JBD2 gate

The ext4 gate copies the supplied image for every pass, so it never writes the
input. It boots with `rootfstype=ext4` and runs:

- a write suite covering directories, ordinary and fragmented extent files,
  random-data SHA-256, truncate, hard links, rename, fsync, a 200-file
  directory, a 20-level directory tree, hole reads, `fallocate` allocation,
  and zero-range (`fallocate -z`) verification;
- crash/replay passes for JBD2 fault stages 1-4, each writing a directory of
  32 files before the crash and verifying every entry after recovery;
- `e2fsck -fn` after every recovery.

```bash
make test_riscv_debian_ext4 \
  DEBIAN_EXT4_ROOTFS=/absolute/path/to/rootfs.ext4

# Derive a 64-bit-descriptor image from an ext4 base and run the same gate.
make test_riscv_debian_ext4_64bit \
  DEBIAN_EXT4_64BIT_BASE=/absolute/path/to/rootfs.ext4
```

`DEBIAN_EXT4_FAULT_STAGES` defaults to `1,2,3,4`; set it to an empty string to
run only the write suite. Logs and e2fsck receipts go to
`target/riscv-debian-ext4/` (or `target/riscv-debian-ext4-64bit/`). The host
needs `e2fsck`, `resize2fs`, `dumpe2fs`, and `cp` with sparse-file support.
The image must be a compatibility ext4 image with `extent` and `has_journal`;
64-bit group descriptors are supported for low block numbers, but metadata
checksums, `flex_bg`, and `orphan_file` are not. The 64-bit target keeps the
base image untouched, enables `64bit` on a sparse copy, and verifies the
64-byte group descriptor size before booting.

## Host tests

The host suite covers the active Python/shell surface: direct and ext4 gate
command contracts, rootfs profiles and manifest parsing, signed sources,
descriptor-pinned publication, builder CLI contracts, and tooling syntax.

```bash
make test_riscv_host HOST_TEST_JOBS=32
make test_riscv_host_list
```

Tests live under `tools/riscv/tests`; the runner executes each module in an
isolated worker process and checks the collected IDs against
`tools/riscv/tests/expected_host_tests.json`. Per-module logs go to
`target/host-tests/` by default (`HOST_TEST_LOG_DIR` overrides it). The
baseline manifest records the module names, exact test IDs, and allowed skip
reasons; adding or removing a
test requires updating the baseline in the same change.

## Desktop gate

The desktop gate is the compact direct-boot path for the QEMU-supported
`browser-web` image. It loads the kernel, full Stage1 initramfs, and a
RAM-backed simple-framebuffer DTB directly, then checks the ordered
`BROWSER_WEB_DESKTOP_STAGE` markers from systemd/Xorg/window-manager/panel.
It is independent of U-Boot and the removed M3/M4 gate stack.

The gate does not treat those markers as pixel evidence. The rootfs installs
`asterinas-desktop-frame-evidence.service`; it waits for a visible Firefox
window with `xdotool` and emits `BROWSER_WEB_DESKTOP_FIREFOX_WINDOW=ready`.
The host then reads the guest framebuffer with the QEMU monitor's `pmemsave`,
validates that it is non-blank and colour-rich, and requires the Firefox
window to change the frame. The desktop and final images are saved as raw and
PPM files next to the serial log.

Why the DTB is rebuilt: direct boot has no firmware to program the bochs PCI
BAR. A `bochs-display` BAR stays unassigned, so the historical DTB address
`0x40000000` reads back `0xffffffff` even while every startup marker passes.
The Makefile's `make_fb_ram_dtb.sh` moves the framebuffer to the top of QEMU
RAM (`0xf0000000` for the 2 GiB contract) and shrinks the memory node so the
kernel keeps it out of the allocator. The gate then reads real guest pixels.

```bash
make test_riscv_debian_desktop \
  DEBIAN_DESKTOP_ROOTFS=/absolute/path/to/debian-root.ext2 \
  DEBIAN_DESKTOP_STAGE1=/absolute/path/to/initramfs.cpio \
  DEBIAN_DESKTOP_DTB=/absolute/path/to/fb-bochs.dtb
```

Use the full Stage1 image and a `browser-web` rootfs built from this tree (it
must include `desktop_frame_evidence.sh` and its unit). The kernel defaults to
`target/osdk/asterinas-osdk-bin.Image`; override it with
`DEBIAN_DESKTOP_KERNEL`. The target requires `fdtget`/`fdtput`, generates
`fb-ram.dtb` in the log directory, and passes `--fb-address 0xf0000000`
(override with `DEBIAN_DESKTOP_FB_ADDRESS` if the generated DTB differs). The
serial log, `framebuffer.raw`/`.ppm`, `desktop-framebuffer.raw`/`.ppm`, and
`result.json` go to `target/riscv-debian-desktop/`. The rootfs is attached with
`snapshot=on`.

## Product gate

The product gate runs the active QEMU gates and writes one receipt:

```bash
make test_riscv_debian_product \
  DEBIAN_PRODUCT_ROOTFS=/absolute/path/to/toolchain-rootfs.ext2 \
  DEBIAN_PRODUCT_EXT4_ROOTFS=/absolute/path/to/rootfs.ext4 \
  DEBIAN_PRODUCT_DSH_IMAGE=/absolute/path/to/dsh-repo.ext2
```

`DEBIAN_PRODUCT_ROOTFS` is required and is checked by the direct gate.
`DEBIAN_PRODUCT_EXT4_ROOTFS` and `DEBIAN_PRODUCT_DSH_IMAGE` are optional; the
former enables the write/crash gate, the latter enables the dsh check inside
the direct gate. `DEBIAN_PRODUCT_DIRECT_ROOTFSTYPE=ext4` selects ext4 when the
direct rootfs itself is ext4. The kernel defaults to
`target/osdk/asterinas-osdk-bin.Image`.

Receipts and gate logs go to `target/riscv-debian-product/`
(`result.json`, the parsed `direct-gate.json` / `ext4-gate.json`, their
`direct-gate.log` / `ext4-gate.log` transcripts, `direct-serial.log`, and the
`ext4/` crash receipts). `make test_riscv_debian_full` runs the host suite
followed by the product gate.

## Stage1

The first Stage1 migration keeps the downstream root handoff and adds the
devtmpfs setup required by the upstream kernel:

```bash
make build_riscv_debian_stage1 \
  RISC_V_CC=riscv64-linux-gnu-gcc \
  DEBIAN_STAGE1_OUT=/absolute/path/to/initramfs.cpio
```

The full builder, including the current browser/input helpers, is:

```bash
make build_riscv_debian_stage1_full \
  RISC_V_CC=riscv64-linux-gnu-gcc \
  DEBIAN_STAGE1_FULL_OUT=target/debian-riscv/stage1/initramfs.cpio
```

The handoff accepts `ASTER_DEBIANROOT` for an interactive root and
`ASTER_BROWSERWEB` for the systemd/browser root. See `rootfs/README.md` for the
root labels, self-tests, and desktop runtime details.

## dsh launch environment

A dsh process launched from an `init=/bin/bash` serial console inherits an
empty environment. Bash sets a default `PATH` for the interactive shell but
does not export it, so the bash tool spawned by dsh can find a command yet
gcc itself cannot locate `cc1`/`ld`. Export the standard PATH before starting
dsh:

```bash
export PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin
```

A normal login or systemd service already exports PATH; the manual console
case is the one that needs this line. The direct-boot gate exports it and
compiles a real C program so this cannot regress silently.

## Current boundary

The direct gate uses `init=/bin/bash` to keep the checkpoint independent of
Stage1. The desktop gate is a separate direct-boot Stage1 check for the ordered
desktop-session markers, a visible Firefox window, and real guest framebuffer
pixels. A direct-gate failure means the upstream kernel cannot run the Debian
tools, not that the full desktop flow has regressed.
