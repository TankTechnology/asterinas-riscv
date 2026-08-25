# Debian RISC-V systemd M2 Design

Date: 2026-08-25

## Goal

Boot a signed Debian Trixie `riscv64` root on current Asterinas with Debian's
packaged systemd as PID 1, reach a bounded base-system target, perform a normal
userspace-requested reboot, and prove that a second boot reaches the same target
from the same persistent ext2 root. The final gate must pass first in
generic-Sv39 QEMU with four harts and then on Megrez.

## Scope

M2 includes systemd PID 1, basic target execution, a serial evidence service,
normal reboot, and persistent boot-count evidence. It does not include guest
networking, online apt, SSH, udev completeness, USB/xHCI, graphics, a display
manager, or a desktop.

The M1 image, schema, builder default, artifacts, and evidence remain frozen.
M2 produces a separate profile and output tree. Existing M1 verification must
continue to accept the already-built M1 artifacts without modification.

## Chosen approach

Use a new signed Debian profile rather than modifying M1 or copying the old
hand-built systemd initramfs. Debian's signed `systemd-sysv`, `systemd`, `dbus`,
and required dependency closure are resolved from the same authenticated
Trixie indexes as the existing rootfs builder. This avoids the historical
systemd build's baked host paths and preserves package provenance.

The rootfs builder gains an explicit `systemd-m2` profile while retaining the
current minimal profile as the default. A schema-v2 manifest adds the profile
identity; schema v1 stays byte-for-byte compatible. The complete installed
package lock and downloaded-package provenance remain equal sets under both
schemas.

## Components

### Profile contract

A small Python profile definition owns the exact additional requested package
names and the M2 identity packages. The shell builder obtains the package list
from this definition instead of duplicating it. The systemd profile requires at
least `systemd`, `systemd-sysv`, and `dbus`; transitive dependencies remain
locked and hashed exactly like M1.

M2 artifacts are published under
`target/debian-riscv/systemd-m2/rootfs/`. They use a distinct filesystem UUID
and label `ASTER_DEBIANM2` so Stage1 cannot confuse M1 and M2 roots.

### Stage1 handoff

Stage1 accepts one exact selector forwarded in its argv:
`--root-init=interactive` or `--root-init=systemd`. The default remains
`interactive`, preserving the existing Bash gate. Unknown, duplicate, or
malformed selectors fail before mounting the root.

Interactive mode keeps the current handoff. Systemd mode mounts the persistent
root and the writable runtime filesystems, binds `/dev`, creates the proc/sys
mount points, chroots, and execs `/sbin/init`. It does not pre-mount proc, sysfs,
or cgroupfs; systemd owns those mounts.

### M2 evidence service

The builder installs one oneshot unit ordered after `multi-user.target`'s base
dependencies. Its executable script:

1. validates `uname -m`, `/etc/debian_version`, the root filesystem type, and
   the installed systemd package identity;
2. increments and synchronizes
   `/var/lib/asterinas-debian-m2/boot-count`;
3. emits one canonical console marker with the boot count;
4. on boot 1, invokes Debian's `/sbin/reboot -f`;
5. on boot 2, emits `DEBIAN_SYSTEMD_M2_PASS` and remains successful.

Any invalid counter, identity mismatch, write/sync error, or unexpected count
prints `DEBIAN_SYSTEMD_M2_FAIL reason=<stable-reason>` and fails the unit.

### QEMU and board gates

The QEMU gate reuses the current generic-Sv39 CPU profile, four-hart DTB,
2 GiB RAM, no network, no display, bounded serial transcript, process-group
cleanup, and full fatal-marker scan. It boots a private writable copy of the M2
root twice and requires ordered boot-1 ready, firmware restart, boot-2 ready,
and final pass markers.

The Megrez gate uses the compiled-Sv39 Image, the same Stage1 archive, the
board DTB, and only the exact partition-2 write capability. Installation uses
the existing resumable chunk installer with the M2 image hash. Linux may stage
immutable boot files only; it must not write the M2 root partition.

## Safety and failure handling

- No command enables whole-device writes.
- Every rootfs input remains signature-, hash-, and package-lock bound.
- M1 and M2 labels, UUIDs, paths, manifests, and evidence never alias.
- Builder publication remains descriptor-pinned and rollback-protected.
- Stage1 rejects ambiguous root matches and unsafe init selectors.
- A missing `reboot`, a failed reboot syscall, an early process exit, a panic,
  or a marker out of order fails the gate.
- QEMU runtime is networkless; mirror access occurs only during the one signed
  M2 build.

## Verification order

1. Host unit tests for profile/schema compatibility, Stage1 selection, unit
   script state transitions, and gate classification.
2. One signed M2 build with exact provenance verification and ext2 inspection.
3. QEMU Sv39/SMP=4 two-boot systemd gate.
4. Resumable Asterinas-only Megrez installation with full-image SHA-256.
5. Megrez two-boot systemd gate and evidence document.

The milestone is complete only after step 5. QEMU success alone does not imply
Megrez success, and reaching a shell does not imply systemd M2 success.
