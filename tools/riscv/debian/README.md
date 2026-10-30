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

The serial log is written to `target/riscv-debian-direct.log` by default.

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
Stage1. The full Stage1 browser root and the manual QEMU/U-Boot desktop boot
are verified separately; the repository still has no automated U-Boot desktop
scenario. A direct-gate failure means the upstream kernel cannot run the Debian
tools, not that the full desktop flow has regressed.
