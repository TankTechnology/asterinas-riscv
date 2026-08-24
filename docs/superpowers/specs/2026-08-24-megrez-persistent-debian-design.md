# Megrez Persistent Debian on Asterinas

## Goal

Boot a persistent Debian 13 RISC-V system on the Milk-V Megrez with Asterinas
as the kernel. The first board milestone is a writable Debian command-line
system whose root filesystem survives a reboot. Later milestones add systemd,
networking, input, and display support without changing that kernel boundary.

Debian must never run on a Linux kernel as part of the acceptance path. A
provisioning environment may copy a frozen image onto the SD card, but every
runtime result and compatibility claim must come from Asterinas.

## Why the Existing Milestones Did Not Cover This

The existing Megrez boot uses an initramfs loaded by U-Boot, so it can reach a
shell without any block driver. Debian rootfs M1 uses QEMU VirtIO block devices
to validate ext2, Stage1, package provenance, and two-boot persistence. Neither
path exercises the EIC7700 SD controller. Asterinas currently has no SDHCI/MMC
driver, so QEMU success cannot by itself establish board storage support.

## Frozen Hardware and Disk Contract

The reviewed Megrez DTB identifies the removable SD controller as:

- node: `/soc/mmc@0x50460000`;
- compatible: `eswin,sdhci-sdio`;
- MMIO: `0x50460000..0x5046ffff`;
- interrupt: 81;
- bus width: 4;
- properties: `broken-cd`, `disable-wp`, `no-mmc`;
- maximum declared frequency: 208 MHz.
- core clock: `eswin,syscrg_csr` resolves to `0x51828000` with the SD core
  divider at offset `0x164`.

The controller at `0x50450000` is the separate non-removable 8-bit eMMC and is
outside the first milestone.

The existing SD-card layout is preserved except for the approved swap
partition:

- partition 1: RockOS `/boot`, never formatted or overwritten;
- partition 2: existing swap, may be replaced by `ASTER_DEBIANROOT` only after
  its exact start, length, and type are recorded and checked;
- partition 3: RockOS root, never formatted or overwritten.

Every write-capable board tool must compare the live partition table with the
recorded contract before touching partition 2. A mismatch fails closed.

## Architecture

### 1. Safe SDHCI transport

Add a focused SD/MMC component that exposes a sector-oriented block device.
Register access uses OSTD's safe MMIO abstractions; any unavoidable `unsafe`
remains inside OSTD. The kernel and component crates remain safe Rust.

The first implementation uses bounded PIO rather than DMA. This avoids mixing
initial card bring-up with the EIC7700's non-coherent DMA/IOMMU contract. The
transport performs controller reset, low-frequency card discovery, SD command
negotiation, 4-bit selection, capacity discovery, and bounded single/multiple
sector I/O. Timeouts, command errors, CRC errors, and controller resets are
reported instead of retried without limit.

DTB discovery accepts only the reviewed `eswin,sdhci-sdio` node and MMIO/IRQ
contract. U-Boot-provided PHY/reset state may be reused initially, but the
driver verifies controller capabilities and applies a conservative SD clock
through the EIC7700 CRG divider. The standard SDHCI divider is not used as a
substitute for this vendor clock resource, and the DTB's 208 MHz maximum is not
treated as an immediately safe card clock.

### 2. Block and partition integration

Expose the card through the existing Asterinas block registry and reuse the
existing partition parser. Partition devices must have stable Linux-compatible
names. Read-only gates verify the MBR/GPT sectors repeatedly, reject overlapping
or out-of-range partitions, and identify partition 2 before write support is
enabled.

The driver first lands with writes disabled. Write support is enabled only
after real-board sector reads match U-Boot and host-side reference hashes.
Write tests initially target a disposable bounded range, then the approved
partition 2 image; partitions 1 and 3 are never test targets.

### 3. Persistent Debian root handoff

Provision the already signed and validated Debian ext2 image onto partition 2,
with an exact `ASTER_DEBIANROOT` label and recorded image/package identities.
Stage1 discovers the MMC partition in addition to VirtIO devices, verifies the
ext2 magic and full label, mounts it read-write, binds `/dev`, mounts `/proc`,
`/sys`, `/run`, and `/tmp`, then executes the Debian init target.

The first runtime target is Debian Bash with working coreutils, procps,
util-linux, dpkg database access, file creation, `sync`, and clean reboot. The
board gate boots twice: boot 1 writes a nonce and syncs; boot 2 verifies the
same nonce and package identity. Only then is persistent Debian CLI considered
complete.

### 4. Debian completion layers

After persistent CLI passes, extend the same root rather than switching to a
Linux kernel or RAM-only substitute:

1. close syscall and pseudo-filesystem gaps required by Debian tools;
2. add a minimal service supervisor, then systemd multi-user boot;
3. enable Asterinas networking and package retrieval;
4. integrate USB keyboard/input;
5. add framebuffer/DRM and desktop services.

HDMI remaining at `Starting kernel ...` is not a storage failure. Until the
framebuffer/DRM layer exists, serial is the authoritative Debian console.

## Alternatives Rejected

- **Debian packed into initramfs:** runs on Asterinas but bypasses persistent
  storage and would hide the primary board gap.
- **Debian chroot under RockOS/Linux:** validates Debian userspace on the wrong
  kernel and cannot support Asterinas compatibility claims.
- **Port the Linux EIC7700 driver wholesale:** conflicts with Asterinas's safe
  kernel boundary and imports unrelated DMA, clock, and Linux subsystem state.
- **Raw U-Boot RAM disk as the final root:** useful only as a diagnostic and
  cannot satisfy persistence.

## Verification

Host tests use an injected SDHCI register/card model to cover command order,
timeouts, response parsing, capacity math, block bounds, and write-disable
gates. QEMU continues to cover the generic block/ext2/Stage1 contract through
VirtIO; it is not treated as proof of EIC7700 behavior.

Real-board gates are ordered and resumable:

1. DTB and partition inventory only;
2. controller reset and card identity;
3. repeated read-only sector hashes;
4. disposable-range write/read/restore with explicit approval;
5. partition 2 provisioning and ext2 validation;
6. Debian boot 1 write/sync;
7. Debian boot 2 persistence and package verification.

Each gate records the kernel, DTB, image, partition table, serial transcript,
and relevant sector hashes. Failure leaves U-Boot/RockOS boot files and
partitions 1 and 3 unchanged.

## Delivery Boundaries

This is a multi-milestone effort rather than one large unreviewable change:

- M2a: SDHCI model, DTB binding, read-only card discovery;
- M2b: block registry, partitions, bounded write support;
- M2c: partition 2 provisioning and two-boot ext2 persistence;
- M3: persistent Debian CLI on Asterinas;
- M4: systemd multi-user and networking;
- M5: input and display integration.

Each milestone receives one focused implementation review and one proportional
local verification pass. Remote CI is not monitored as a substitute for local
evidence.
