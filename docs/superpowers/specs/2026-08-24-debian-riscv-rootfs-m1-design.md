# Debian RISC-V Rootfs M1 Design

Date: 2026-08-24  
Status: Approved design, implementation not started

## Decision Summary

Build a minimal Debian 13 `riscv64` userspace on a persistent ext2 disk and
boot it on Asterinas through a small stage-1 initramfs. The stage-1 program
finds the root disk by an ext2 volume label, mounts the required filesystems,
changes root, and executes Debian's dynamically linked Bash. A bounded host
gate boots the same private root disk twice with Sv39 and four harts, verifies
the Debian userspace contract, and proves that a file written during the first
boot survives the second.

This milestone establishes the distribution foundation needed before Debian
systemd, Xorg, a browser, or PCI xHCI keyboard integration. It deliberately
does not combine those later layers with root-filesystem bring-up.

## Context

The current RISC-V desktop demonstrations use hand-assembled initramfs images
and packages drawn from several build environments. They prove substantial
kernel and graphical functionality, but they are not a Debian installation.
The separate `codex/debian-riscv-input` branch likewise proves only a
distro-neutral VirtIO keyboard-to-evdev path; its guide explicitly excludes a
Debian root filesystem, package installation, systemd, and a graphical
session.

Asterinas currently constructs its initial root from an initramfs and rejects
a boot without one. Therefore, an external Debian disk cannot be the initial
root directly. Historical commit `b03e12321` on `origin/track/nixos` proved a
useful narrower mechanism: a static stage-1 process can mount a second VirtIO
ext2 disk, carry `/dev` into it, change root, and execute a disk-resident
userspace. That experiment depended on private backup artifacts, used one
hart, and targeted a hand-built NixOS tree. This design reuses the mechanism,
not its code or artifacts.

Debian's signed `trixie` Release metadata currently identifies `riscv64` as a
release architecture. TUNA is the default transport mirror for local builds;
USTC and `deb.debian.org` are supported alternatives. The Debian archive
signature, rather than trust in a particular transport mirror, is the source
authentication boundary.

## Goals

1. Build an auditable Debian `trixie` `riscv64` minbase from signed Debian
   archive metadata.
2. Store that userspace on a writable, persistent ext2 image that Asterinas
   can mount.
3. Boot the current Asterinas RISC-V kernel in Sv39 mode with exactly four
   harts and enter Debian's dynamically linked `/bin/bash` over the serial
   console.
4. Verify Debian identity, architecture, dynamic execution, package database,
   pseudo-filesystem mounts, root-disk writability, and persistence across two
   boots.
5. Publish bounded, machine-readable evidence that identifies every boot
   artifact and records failure at the first missing boundary.

## Non-goals

- Starting Debian systemd or reaching `multi-user.target`.
- Running `apt update`, downloading packages, or validating guest networking.
- Starting Xorg, a window manager, a browser, or any desktop service.
- Attaching a VirtIO or USB keyboard; the M1 console is serial.
- Supporting ext4 as an Asterinas root filesystem.
- Proving hotplug, partitions, arbitrary disk layouts, or physical Megrez
  storage.
- Merging the old Debian-input or `track/nixos` branches wholesale.
- Claiming byte-for-byte rebuilds after the mutable Debian stable archive has
  changed. The first milestone freezes and audits each produced artifact;
  snapshot-based replay is a follow-up.

## Approaches Considered

### Selected: a dedicated Debian ext2 root disk

Build a Debian minbase directory, populate an ext2 image, and mount it from a
small stage-1 initramfs. This matches Asterinas's current filesystem support,
keeps persistent state outside the boot payload, and isolates rootfs failures
from systemd and desktop behavior.

### Rejected for M1: adapt a Debian cloud image

An official cloud image is more complete but also introduces a partition
table, ext4, cloud-init, Linux initrd assumptions, and a large service graph.
Those variables obscure the first failure boundary and depend on filesystem
features Asterinas does not currently provide.

### Rejected for M1: put Debian inside the initramfs

This could reach a shell quickly, but it consumes guest memory, is not a real
persistent disk root, and cannot prove state survival across reboot. It would
repeat the main limitation of the existing desktop demonstrations.

## System Architecture

The run has two private disks:

```text
boot.ext4                         debian-root.run.ext2
├── asterinas.booti               └── Debian trixie riscv64 minbase
├── qemu-virt.dtb                     label: ASTER_DEBIANROOT
└── stage1-initramfs.cpio
```

The boot disk is an ext4 container consumed only by U-Boot. Asterinas does not
mount it as the Debian root. The second disk uses ext2 with 4096-byte blocks
and is attached as another VirtIO block device. The gate copies the frozen
base root image into a run-private image before the first boot. Both boots use
that same private copy.

The complete data path is:

```text
U-Boot booti
  -> Asterinas initramfs root
  -> static stage-1 /init
  -> locate ASTER_DEBIANROOT
  -> mount ext2 at /newroot
  -> bind /dev and mount proc/sysfs/tmpfs
  -> chroot /newroot
  -> Debian dynamic loader
  -> Debian /bin/bash
  -> serial command gate
```

## Components

All new source lives below `tools/riscv/debian/rootfs/`, with tests in the
existing RISC-V test package.

### `build_rootfs.sh`

- Runs only in the documented Asterinas development container.
- Uses `debootstrap --foreign --variant=minbase --arch=riscv64` for the first
  stage.
- Completes the second stage under `qemu-riscv64-static` in an ephemeral
  build container with a verified RISC-V binfmt registration; the builder
  refuses to run a target maintainer script natively on the host.
- Removes the emulator from the finished root so the gate cannot accidentally
  execute translated binaries.
- Installs the Debian essential/minbase set plus `bash`, `coreutils`,
  `util-linux`, `procps`, and `ca-certificates`. `apt` and `dpkg` remain part
  of the root, but the runtime gate does not access the network.
- Normalizes volatile host identity such as `/etc/machine-id` and removes
  transient package caches and build logs.
- Creates a 1-GiB ext2 image with a fixed filesystem type, 4096-byte blocks,
  and volume label `ASTER_DEBIANROOT`.
- Uses the corrected 16-byte label because ext2 volume labels cannot exceed
  16 bytes.
- Does not replace an existing output until all provenance, content, and
  filesystem checks pass.

### `stage1_init.c`

- Is the only executable in a raw stage-1 `newc` initramfs.
- Opens the serial console for standard input, output, and error.
- Waits for candidate VirtIO block nodes for at most 30 seconds.
- Reads the ext2 superblock and selects exactly one block device whose magic
  and volume label match the contract. Device numbering such as `/dev/vdb` is
  not treated as identity.
- Fails closed if no matching disk exists, more than one matches, the device
  is not a block device, or the image metadata is malformed.
- Mounts the root read-write at `/newroot`, bind-mounts the registry-provided
  `/dev`, and mounts `proc`, `sysfs`, and tmpfs instances for `/run` and
  `/tmp` below the new root.
- Changes root and working directory, then executes Debian Bash with a small
  reviewed rcfile. The Bash rcfile emits the shell-ready marker and sets a
  fixed prompt; seeing that marker proves the Debian dynamic loader and Bash
  both executed.
- Emits one stable failure marker and then remains alive if handoff fails, so
  the host can collect complete evidence instead of observing a PID-1 exit
  panic.

### `build_stage1.sh`

- Cross-compiles `stage1_init.c` as a static RISC-V ELF with strict warnings.
- Creates a deterministic raw `newc` archive containing only `.` and `/init`.
- Uses an atomic output replacement and preserves an existing good archive if
  compilation or packaging fails.

### `rootfs_gate.py`

- Validates and snapshots the kernel, U-Boot, DTB, stage-1 archive, root-image
  manifest, and frozen ext2 image before launching QEMU.
- Requires an Sv39-compatible CPU contract and exactly four enabled DTB CPU
  nodes for `--smp 4`.
- Creates a private boot disk and a private writable copy of the Debian root.
- Launches QEMU with `-nic none`, two VirtIO block devices, bounded serial and
  monitor operations, and no keyboard fallback.
- Prints progress at every boot and command boundary. It never performs an
  unbounded connect, read, process wait, or silent retry.
- Invalidates stale success evidence before preparation starts and atomically
  publishes the final result after serial logs and cleanup are complete.
- Terminates the full QEMU process group on every failure path and records a
  cleanup failure as a failed gate.

### `test_debian_rootfs.py`

- Tests the build command and package/provenance contract without downloading
  Debian.
- Compiles a native stage-1 self-test that exercises missing, malformed,
  ambiguous, and delayed root-device discovery.
- Inspects a generated ext2 fixture for its label and required tree.
- Tests QEMU arguments, DTB/SMP rejection, command ordering, timeouts, stale
  evidence invalidation, serial classification, atomic publication, signal
  handling, and stubborn process-group cleanup.

## Debian Provenance and Artifact Identity

The default mirror is
`https://mirrors.tuna.tsinghua.edu.cn/debian`; builders may select USTC or
`https://deb.debian.org/debian`. The build always verifies Debian's signed
metadata using `debian-archive-keyring`. A proxy may transport bytes but does
not alter or replace this signature check.

The build output is:

```text
target/debian-riscv/rootfs/
├── debian-root.ext2
├── packages.lock
├── rootfs-manifest.json
└── source-metadata/
    ├── InRelease
    └── package-checksums
```

`packages.lock` records installed package names, architectures, and exact
versions. `rootfs-manifest.json` records the suite, release version, mirror,
architecture, signed-metadata hash, package-lock hash, downloaded package
hashes, filesystem label/UUID/size, tool versions, and final root-image hash.
The final root-image hash describes the immutable base image. The gate binds
its result to this manifest, validates that base before copying it, and records
separate pre-boot and post-boot hashes for the writable run-private copy. A
post-boot hash is expected to differ after the persistence write; it is never
compared to the immutable base as if mutation were corruption.

This is an auditable frozen-build contract, not a promise that a later build
against an updated stable mirror will be byte-identical. An optional Debian
Snapshot replay mode may be added after M1 without changing the runtime
contract.

## Boot and Command Protocol

QEMU uses the repository's registered generic-Sv39 CPU contract, 2 GiB of
memory, four harts, no networking, no display, and no input device. U-Boot
loads the kernel, the matching four-hart DTB, and stage-1 initramfs from the
private boot disk.

The host does not accept a shell prompt alone as success. It waits for the
ordered stage-1 and Debian markers and then sends nonce-bound commands. Each
command prints a unique completion marker containing its exit status, so an
echoed command or stale prompt cannot satisfy the gate.

The first boot verifies:

```sh
uname -m
cat /etc/debian_version
printf '%s\n' "$BASH_VERSION"
dpkg-query -W base-files libc6 bash coreutils util-linux
stat -f /
mkdir -p /var/lib/asterinas-debian-m1
printf '%s\n' "$RUN_ID" > /var/lib/asterinas-debian-m1/persist
sync
```

After `sync`, the host requests a normal QEMU monitor quit and waits for the
complete process group to exit. The second boot uses the same run-private root
disk and verifies the exact `RUN_ID`, rewrites a second probe file, calls
`sync`, and exits through the same monitor path.

## Acceptance Criteria

M1 passes only if all of the following hold in one current run:

1. The signed Debian metadata, package lock, base root image, kernel, U-Boot,
   DTB, and stage-1 archive match the recorded identities.
2. The DTB has exactly four enabled CPU nodes, QEMU runs with `-smp 4`, and
   the kernel is built for Sv39.
3. Stage-1 selects exactly one `ASTER_DEBIANROOT` disk and mounts it
   read-write as ext2.
4. `/dev`, `/proc`, `/sys`, `/run`, and `/tmp` are available after the root
   handoff.
5. Debian's dynamic loader successfully starts Debian `/bin/bash`.
6. `uname -m` reports `riscv64`; `/etc/debian_version` is nonempty; and
   `dpkg-query` reports the expected package set and locked versions.
7. The root filesystem reports the expected ext2 identity and accepts a file
   create, read, and `sync`.
8. The second boot reads the exact nonce written by the first boot, proving
   persistence rather than same-boot page-cache behavior.
9. Neither transcript contains an Asterinas panic, block-I/O failure, ext2
   error, stage-1 failure marker, or unexpected QEMU exit.
10. Both QEMU process groups and monitor/serial resources are completely
    cleaned up before `result.json` is published with `passed: true`.

## Failure Taxonomy

The first missing boundary becomes the primary result reason:

```text
artifact-identity
dtb-smp-mismatch
root-device-not-found
ambiguous-root-device
invalid-root-superblock
ext2-mount-failed
bind-dev-failed
pseudo-fs-mount-failed
chroot-failed
debian-shell-exec-failed
command-failed:<name>
persistence-failed
kernel-panic
block-io-error
timeout:<phase>
qemu-exit:<phase>
qemu-cleanup-failed
evidence-publication-failed
```

Every failure retains the complete serial transcript available at cleanup,
the attempted QEMU argument vector, input hashes, phase, and error details.
The result schema never reports `passed: true` if lifecycle or evidence
publication is incomplete.

## Verification Strategy

Implementation follows red-green test-driven development in four layers:

1. Host unit tests for package/build contracts, manifest validation, argument
   construction, protocol classification, deadlines, and cleanup.
2. Native and cross-compiled stage-1 self-tests for root-device discovery and
   handoff failure states.
3. Static artifact inspection of the stage-1 archive and Debian ext2 image,
   including ELF architecture/interpreter checks, filesystem label, required
   files, package database, and absence of `qemu-riscv64-static`.
4. One final real QEMU Sv39/SMP=4 two-boot gate. Already-passing unrelated
   USB, desktop, LTP, or remote CI jobs are not repeated.

Network downloads occur only during rootfs construction. Unit tests and the
runtime gate are network-free. The builder caches verified `.deb` inputs by
hash, supports the configured Clash proxy, and may use Chinese mirrors without
weakening Debian signature verification.

## Scope After M1

The next independent milestones are:

1. Debian systemd to `multi-user.target`, including a precise inventory of
   udev, netlink, devtmpfs, cgroup, keyring, and mount-semantic gaps.
2. Guest networking and signed `apt` package installation on a disposable
   copy of the root disk.
3. Xorg/fbdev plus explicit evdev input on Debian.
4. PCI xHCI USB keyboard integration with the Debian desktop.
5. Physical Megrez storage, DWC3/xHCI, interrupt, DMA, and display validation.

Each milestone consumes the frozen result of the previous one and has its own
gate. A later desktop failure must not invalidate or obscure the Debian rootfs
contract established here.
