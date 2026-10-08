# Debian Desktop M4 Basic Applications Design

Date: 2026-08-26

## Goal

Extend the verified Debian Desktop M3 foundation into a basic usable desktop
on Asterinas: a non-root Matchbox session with xterm, PCManFM, and NetSurf,
first proven in QEMU and then installed on the Milk-V Megrez. The physical
acceptance path always runs Debian on Asterinas; RockOS may only transfer a
frozen image to the SD card.

## Chosen approach

Create a new signed `desktop-m4` rootfs profile. Do not mutate the frozen M3
profile or its evidence. Debian Trixie provides native riscv64 `pcmanfm` and
`netsurf-gtk` packages, so M4 uses distribution packages rather than reviving
the older NixOS cross-built application stack.

Two alternatives are rejected:

- modifying M3 in place would invalidate its package and filesystem identity;
- copying binaries from the old hand-built desktop would lose signed Debian
  package provenance and make dependency failures difficult to reproduce.

## Rootfs and session

M4 receives schema version 4, label `ASTER_DEBIANM4`, a distinct fixed UUID,
and a separate output directory. Its requested packages are the exact M3 set
plus `pcmanfm` and `netsurf-gtk`; dependencies remain fully captured in the
existing package lock, checksum set, and manifest.

The `asterinas` X session starts Matchbox first, then maps:

1. PCManFM in file-manager mode on `/home/asterinas`, without taking over the
   root desktop;
2. NetSurf on a packaged local `file://` welcome page, so application startup
   is not confused with the still-separate networking milestone;
3. xterm last, retaining a predictable terminal focus target.

The evidence service requires the M3 udev/logind/PAM/fbdev/evdev foundation,
all four client processes, and mapped PCManFM, NetSurf, and xterm windows before
emitting `DEBIAN_DESKTOP_M4_READY`.

## QEMU and physical flow

The QEMU gate reuses the descriptor-pinned Desktop M3 lifecycle and adds only
the M4 profile/marker/window contract. It captures a non-blank screenshot and
uses the already verified xHCI input gate as the exact keyboard/mouse event
proof. A second unchanged QEMU run is not required.

After QEMU passes, the existing restart-safe Megrez installer writes the
frozen M4 ext2 image to SD partition 2. The first physical boot remains bounded
and must reach M4 READY with both DWC3 workers. Only after that gate passes is a
second RAM-only U-Boot launch performed without `asterinas.reboot_after`,
creating the long-lived desktop session. No `saveenv` is used.

## Failure and safety boundaries

- M3 artifacts are immutable and remain usable as rollback evidence.
- All package metadata and `.deb` payloads retain the existing signed
  InRelease/hash contract.
- NetSurf acceptance is local-page rendering only; online browsing is not
  claimed until Asterinas networking is separately enabled and verified.
- Physical installation retains the exact partition-table/write gate and never
  writes partitions 1 or 3.
- A physical pointer is accepted only after both kernel registration and Xorg
  evdev selection; human-visible motion/click remains an operator observation.

## Acceptance

M4 is complete when one QEMU cold boot publishes a passing result and useful
screenshot containing the three applications, then one Megrez Asterinas boot
reaches the ordered M4 markers with dual xHCI keyboard/mouse and the operator
can see and interact with the desktop. Accelerated DRM, audio, hotplug, and
modern JavaScript browsing remain later milestones.
