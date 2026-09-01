# Megrez Debian Persistent Shell on Current Asterinas

**Date:** 2026-08-30
**Status:** Draft for written review

## Decision Summary

The next milestone is a persistent Debian 13 `riscv64` command-line system on
the Milk-V Megrez, running on Asterinas rather than Linux. Success means that
the board boots the signed Debian root from the exact second SD-card partition,
enters an interactive `/bin/bash`, writes and syncs a nonce, reboots, and reads
the same nonce from the same filesystem on the second boot.

This is a convergence and evidence milestone, not a new storage-stack design.
The repository already contains the signed rootfs contract and builder, Stage1
handoff, constrained partition-2 write gate, compressed installer, and generic
two-boot protocol. The work must reuse those components, freshly bind them to
the current kernel and board artifacts, and implement only an adapter or defect
that a focused test or physical result proves missing.

Systemd, networking, Xorg, a window manager, NetSurf, Firefox, and desktop
polish are deliberately outside this milestone. They build on top of this
persistent shell and must not weaken its identity or recovery contracts.

## Existing Foundation

The following capabilities are already present and are inputs to this design:

- The signed Debian rootfs contract and builder produce a Debian 13 point
  release for `riscv64`, a 1 GiB ext2 image, a full package lock, retained
  signed metadata, and package checksums.
- [`gate_protocol.py`](../../../tools/riscv/debian/rootfs/gate_protocol.py)
  defines the two-boot identity and persistence commands. Boot 1 writes a
  64-character nonce under `/var/lib/asterinas-debian-m1`, calls `sync`, and
  reads it back. Boot 2 reads the same nonce, creates a second probe, and calls
  `sync` again.
- [`stage1_init.c`](../../../tools/riscv/debian/rootfs/stage1_init.c) discovers
  both VirtIO roots and `/dev/mmcblk0p1` through `/dev/mmcblk0p3`, selects the
  exact ext2 label, mounts the root, installs the pseudo-filesystems, and hands
  off to Debian Bash.
- [`kernel/comps/mmc/src/block.rs`](../../../kernel/comps/mmc/src/block.rs)
  keeps the whole MMC disk read-only by default. The opt-in
  `asterinas.mmc_write_partition2` gate permits writes only through an exact
  partition-2 node with start LBA `0x000fa022` and length `0x00800000`
  sectors; writes outside that logical range are rejected.
- [`megrez_installer.py`](../../../tools/riscv/debian/rootfs/megrez_installer.py)
  already provides Asterinas-only install and read-only verification payloads
  for `/dev/mmcblk0p2`, exact geometry, and root-image identity.
- The [compressed root install design](2026-08-29-megrez-compressed-root-install-design.md)
  streams a gzip root image into the exact partition without staging a second
  1 GiB copy in memory.
- The [pre-board design](2026-08-28-megrez-debian-preboard-design.md) binds
  kernel, DTB, U-Boot, initramfs, rootfs, boot arguments, and QEMU evidence into
  a physical-run permit.
- The current branch has a physical SDHCI SDMA pass: an exact 32 MiB read
  completed in 5.195899 seconds with CRC32 `5f85f90e`, followed by an automatic
  return to U-Boot through the EIC7700X hardware watchdog.

The remaining gap is one fresh, auditable chain on the current branch:

```text
signed Debian identity
  -> current Stage1 and Asterinas kernel
  -> current Megrez DTB/U-Boot contract
  -> exact /dev/mmcblk0p2 contents
  -> boot-1 write and sync
  -> reboot
  -> boot-2 read and identity verification
  -> interactive Debian Bash
```

Historical success from a different commit or artifact set cannot close this
gap.

## Scope and Non-goals

### In scope

1. Freeze the current source commit and every input artifact by size and
   SHA-256.
2. Freshly run the generic QEMU two-boot Debian gate as a fast protocol and
   userspace regression tier.
3. Identify the actual second SD-card partition without writing it.
4. Reuse an already matching partition, or provision it exactly once when its
   identity is absent or mismatched.
5. Boot the same root twice on Megrez, prove persistence, and publish bounded
   serial evidence.
6. Perform one final normal boot into an interactive Debian Bash and leave the
   system running for the operator.
7. Add only the missing current-main adapter or narrowly reproduced fixes
   needed to complete those steps.

### Out of scope

- booting Linux on the board for installation, validation, or runtime;
- systemd and service management;
- DHCP, DNS, TCP/TLS, browser networking, or package installation at runtime;
- framebuffer, DRM, Xorg, desktop components, input polish, or applications;
- changing partition-table geometry or writing partitions 1 or 3;
- unconditional reinstallation of an already matching root;
- persistent U-Boot environment changes such as `saveenv`;
- redesigning the MMC, rootfs, or installer subsystems without a reproduced
  defect.

## Paging and Platform Contracts

QEMU and Megrez are two different validation tiers and must never silently
share a paging assumption:

| Tier | CPU/DT contract | Purpose |
|---|---|---|
| QEMU fast gate | generic Sv39, SMP=4, 2 GiB | Debian protocol, Stage1, ext2, syscalls, and two-boot persistence |
| Megrez physical gate | Megrez Sv48, SMP=4, board DTB | real SDHCI, partition nodes, board boot chain, and physical persistence |

The QEMU build enables `riscv_sv39_mode` and disables Sv48 in its CPU contract.
The physical build uses the current Megrez Sv48 contract and must not reuse an
Sv39-only image merely because the generic QEMU gate passed. Both tiers share
the same signed rootfs identity, Stage1 source, and persistence protocol, while
recording separate kernel and DTB identities.

This document supersedes any older physical-run instruction that implicitly
treated the generic Sv39 QEMU image as the Megrez image. It does not change the
generic QEMU contract itself.

## Execution Flow

### Phase A: Freeze and audit the current bundle

Create one run directory that records:

- source commit and dirty-worktree refusal;
- QEMU Sv39 kernel and four-hart DTB hashes;
- Megrez Sv48 kernel and board DTB hashes;
- Stage1 archive, U-Boot image, Debian root image, manifest, package lock,
  retained InRelease, and package-checksum identities;
- exact physical boot arguments, including whether the partition-2 write gate
  is armed;
- tool versions and the schema version of every result consumed.

All source files are opened without following symlinks and held or copied into
a private run directory before validation. Existing `passed: true` evidence is
invalidated before any new attempt. No physical action may consume an artifact
that differs from the frozen bundle.

The audit first checks whether the existing pre-board and install results are
current, complete, and bound to the same bundle. Stale evidence may inform the
diagnosis but cannot authorize a write or a pass.

### Phase B: Fresh QEMU two-boot gate

Run the existing networkless, displayless generic-Sv39/SMP=4 gate with two
VirtIO block devices:

1. Boot 1 verifies Debian and package identity, writes the random nonce, calls
   `sync`, reads the nonce back, and performs the normal reboot action.
2. Boot 2 uses the same writable root image, verifies the same identity and
   nonce, creates the second probe, calls `sync`, and exits through the normal
   gate lifecycle.

The full serial transcript is scanned for panic, oops, fatal, timeout, and
protocol-order violations. A successful QEMU run proves the generic software
path only; it does not prove the Megrez SDHCI or Sv48 path.

### Phase C: Read-only board inventory

Before enabling any write, boot a read-only Megrez payload that proves:

- the board exposes exactly the expected MMC disk and partition nodes;
- partition 2 has start LBA `0x000fa022`, length `0x00800000` sectors, and a
  4 GiB byte extent;
- partitions 1 and 3 retain their recorded geometry;
- partition 2 either contains the exact expected ext2 identity or is explicitly
  classified as absent/mismatched;
- the Stage1 discovery rule selects only `/dev/mmcblk0p2` and rejects an
  ambiguous matching root.

Prefer a current, plan-bound install result when it already proves the expected
uncompressed root SHA-256. Otherwise run the existing read-only verifier. A
full 1 GiB physical hash is intentionally not placed under the short pre-boot
hardware watchdog: at the measured SDMA rate it takes minutes, while the
current watchdog clock permits only a short recovery window.

No write is allowed from this phase. Its result must distinguish “matching”,
“mismatched”, and “not measurable” rather than treating missing evidence as a
match.

### Phase D: Conditional Asterinas-only provisioning

Skip this phase when Phase C proves that partition 2 already contains the
frozen root image. Otherwise, and only with a matching pre-board permit:

1. Boot the existing compressed installer on Asterinas.
2. Require the exact root SHA-256 and
   `asterinas.mmc_write_partition2` boot arguments.
3. Revalidate the live partition geometry before opening it for write.
4. Stream the signed gzip payload to `/dev/mmcblk0p2`, compute the uncompressed
   hash during the write, require the expected byte count, call `sync`, and
   publish an install result bound to the signed-root identity.
5. Refuse automatic retries. A failed or interrupted attempt must return to a
   diagnosable state before a new permit can be issued.

The whole MMC disk remains read-only. The installer must not open the raw disk
for write and must not touch partitions 1 or 3.

The short hardware watchdog is not armed for this long write because a reset in
the middle would manufacture corruption. Recovery uses the existing bounded
software reboot timer after Asterinas component initialization, visible progress
markers, and one authorized attempt. Failure before the software timer is armed
is reported as an exceptional board-recovery condition; it is not hidden by an
unsafe repeated write loop.

### Phase E: Physical two-boot persistence gate

Use the frozen Megrez Sv48 kernel, board DTB, Stage1 archive, and installed root.
A thin board adapter may reuse the existing gate protocol and serial lifecycle;
it must not duplicate their marker parser or persistence state machine.

Boot 1 must:

1. reach the ordered Stage1 and Debian shell markers;
2. prove `uname -m` is `riscv64`;
3. prove the expected Debian point release, Bash version, five explicit package
   identities, and ext2 root filesystem;
4. prove the root is writable;
5. write the fresh nonce, call `sync`, and read the same value back;
6. reboot through the planned Asterinas recovery path.

Boot 2 must:

1. start from a fresh U-Boot and Asterinas boot, not an old serial buffer;
2. reach the same ordered shell markers;
3. repeat all identity checks;
4. read the exact boot-1 nonce;
5. create and sync the second probe;
6. publish the final result only after teardown and complete transcript
   classification.

The evidence records a SHA-256 of the nonce but redacts its plaintext from the
published logs. A post-boot filesystem hash is not compared with the immutable
source-image hash because the persistence writes intentionally change the
filesystem.

### Phase F: Operator shell handoff

After the two-boot gate passes, perform one normal boot with the same physical
artifacts and root identity. Do not arm the short diagnostic watchdog and do
not request an automatic reboot. Leave Debian Bash attached to the board
console so the operator can run ordinary commands.

This final interactive boot is a convenience handoff, not a substitute for the
machine-readable two-boot result. Losing the interactive session cannot erase
or change the preceding gate evidence.

## Recovery and Board-Safety Contract

- Short, read-only probes may use the proven hardware watchdog only when their
  worst-case duration fits its measured window.
- Full-root hashing and installation never run under a watchdog that would
  expire during expected work.
- Long operations emit bounded progress and use an Asterinas software recovery
  timer with an explicit margin; a silent deadline is a failure.
- Every attempt starts from a fresh U-Boot prompt and exactly one `booti`.
- The board controller releases the serial device on every completed recovery
  path and never relies on repeated physical-reset requests as its normal loop.
- A failed write attempt is never retried automatically. The next attempt
  requires a new read-only inventory and a new permit.
- No command persists U-Boot environment state.

The milestone does not claim that software can recover from loss of board
power, a broken early-boot path before all recovery facilities exist, or a
physically wedged controller. It minimizes those exceptional cases by proving
the same software path in QEMU, using read-only inventory before mutation, and
performing one high-information physical attempt.

## Evidence and Publication

The run directory contains, at minimum:

- immutable input manifest and hashes;
- QEMU boot-1 and boot-2 serial logs and result;
- read-only board inventory result;
- conditional install log and result, or an explicit current-identity skip;
- physical boot-1 and boot-2 serial logs;
- redacted persistence evidence;
- final physical result with a stable failure reason or `passed: true`;
- the commands and tool versions needed to reproduce the host-side checks.

Results are written through pinned output directories with stale-success
invalidation, bounded log sizes, atomic single-file replacement, and directory
sync. A pass is published last. A partial run cannot reuse an older pass.

## Testing Strategy

1. Host unit tests cover artifact identity, geometry decisions, stale-result
   invalidation, ordered/split markers, nonce redaction, timeout/fatal scanning,
   and every failure phase without network access.
2. Existing MMC kernel tests retain the partition-2-only write boundary,
   arithmetic overflow rejection, and whole-disk read-only behavior.
3. The generic Sv39/SMP=4 QEMU gate runs once on the final frozen bundle.
4. The physical gate begins only after host and QEMU gates pass and a current
   pre-board permit exists.
5. Physical experiments are read-only until the conditional install decision,
   and installation runs at most once per permit.
6. Remote CI is not monitored as a substitute for local evidence.

## Delivery Boundaries

The implementation plan should preserve four reviewable boundaries:

1. **Current-bundle audit and physical adapter:** converge existing contracts,
   add focused host tests, and generate the pre-board permit.
2. **Read-only inventory and conditional provisioning:** identify or install
   the exact root without changing the persistence protocol.
3. **Two-boot physical gate:** prove Debian identity and nonce persistence on
   the current Megrez Sv48 build.
4. **Operator handoff and documentation:** make the passed artifact set easy to
   boot into a normal interactive shell.

Each boundary is independently committed. A failure in a later boundary does
not justify broad changes to an earlier subsystem without a focused reproducer.

## Rejected Alternatives

- **Boot Linux to provision the root:** rejected because it bypasses the
  Asterinas storage and installation path that this milestone must prove.
- **Trust the current partition without identity evidence:** rejected because
  stale or partial contents can create a false persistence pass.
- **Always reinstall partition 2:** rejected because it adds unnecessary risk
  and destroys useful state when the exact root is already present.
- **Use the QEMU Sv39 kernel on Megrez:** rejected because the board and QEMU
  paging contracts are separate; the physical tier is Sv48.
- **Keep the hardware watchdog armed during a long hash or install:** rejected
  because expected work exceeds its safe window and a reset may interrupt a
  write.
- **Include systemd, network, or desktop work now:** rejected because it would
  obscure whether failures belong to persistent storage, process handoff, or
  higher-level services.

## Acceptance Criteria

The milestone is complete only when all of the following are true on one frozen
current-branch artifact set:

1. The host contract and generic Sv39/SMP=4 QEMU two-boot gate pass.
2. The Megrez run uses a separately recorded Sv48/SMP=4 kernel and board DTB.
3. Read-only evidence proves the exact partition-2 geometry and either a
   matching root identity or the need for one authorized install.
4. Any installation uses Asterinas, writes only `/dev/mmcblk0p2`, verifies the
   exact byte count and uncompressed SHA-256, and publishes a current result.
5. Physical boot 1 reaches Debian Bash, verifies identity, writes and syncs a
   fresh nonce, and reboots through the planned path.
6. Physical boot 2 reaches Debian Bash from a fresh boot and reads the exact
   nonce before publishing `passed: true`.
7. No panic, oops, fatal marker, timeout, stale evidence, or unauthorized disk
   write appears in either complete transcript.
8. A final normal boot leaves an interactive Debian Bash available on the
   board console without a short diagnostic watchdog forcing a reset.

Only after these criteria pass should systemd, networking, and desktop work be
resumed on this root.
