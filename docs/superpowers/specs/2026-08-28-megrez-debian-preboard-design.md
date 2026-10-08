# Megrez Debian Desktop Preboard Gate Design

## Goal

Provide one fail-closed path from an exact Debian desktop artifact set to a
single controlled Megrez boot. The path must prove everything that QEMU can
prove before touching the board, bind every result to the exact bytes being
booted or installed, and require an automatic-recovery contract before the
first physical `booti`.

The guest under test is always Asterinas. Linux is not an accepted runtime or
installer kernel.

## Current Evidence and Gap

The existing `tcp-probe` debug plan already proves generic Sv39, SMP=4,
cache-aware artifact transfer, one `booti`, ordered guest markers, and
`asterinas.reboot_after` recovery. The 2026-08-28 physical run also proved the
Megrez RJ45 path and returned to a fresh U-Boot prompt automatically.

The current signed desktop root, however, has SHA-256
`605827646ed9b770c44ef9b72544236eaa48764c73c33cad8ccba11b46435f89`,
while the existing passing desktop QEMU results name older root and manifest
hashes. Schema 1 intentionally caps every artifact at 64 MiB and therefore
cannot bind the 1 GiB root image. Those results must not authorize a physical
desktop boot.

## Chosen Architecture: Extend the Thin Debug Workflow

The chosen approach is the previously approved thin orchestration layer. It
extends the existing immutable plan and reuses the existing rootfs contract,
M6 QEMU gate, software-reboot QEMU gate, board transport, and physical GMAC
gate. It does not add another QEMU command builder or serial state machine.

### Schema 2 Plan

`DebugPlan` schema 2 uses profile `debian-browser` and carries two explicit
artifact groups:

- boot artifacts: kernel, Stage1 initramfs, QEMU DTB, and Megrez DTB;
- Debian evidence artifacts: QEMU U-Boot, root image, root manifest, package
  lock, package checksums, and signed `InRelease`.

The canonical JSON keeps schema 1 byte-compatible. Artifact validation uses a
per-name size policy: ordinary boot and metadata inputs remain bounded, while
`root_image` must be exactly 1 GiB. Reading an artifact opens it once and
computes size, SHA-256, and CRC32 from the held descriptor. Board transfer
continues to use only kernel, Stage1, and Megrez DTB; the root image is never
sent through XMODEM.

Schema 2 also freezes the exact physical boot arguments, SMP=4, Sv39, ordered
desktop/browser milestones, and a bounded `reboot_after` value. The rootfs
contract must validate profile `desktop-m5-network`, Debian `13.6`, RISC-V,
and the manifest/lock/image relationship before a plan can be written.

### Desktop Simulation Result

`megrez_debug.py simulate PLAN --tier desktop` invokes the existing M6 gate.
The adapter passes only paths from the held schema 2 plan, then checks the M6
result for:

- `passed: true`, profile `desktop-m5-network`, and reason `pass`;
- generic-Sv39 CPU (`sv48=false`), exactly four harts, and 2 GiB RAM;
- VirtIO network, keyboard, tablet, two VirtIO block devices, and no KVM;
- exact hashes for kernel, U-Boot, QEMU DTB, Stage1, root image, manifest,
  package lock, and package checksums;
- DNS/HTTPS evidence, M5 READY, M4 READY, a remote Baidu image, one explicit
  JavaScript capability status, M6 READY, and two nontrivial screenshots.

It publishes the shared `StageResult(stage="desktop")` bound to the plan hash.
A stale or mismatched native M6 result is removed before launch and can never
authorize the board.

### Recovery Result

The preboard path also requires a fresh QEMU software-reboot result bound to
the same kernel hash. The timer case must observe a second firmware epoch and
a fresh U-Boot prompt after `asterinas.reboot_after`; the panic case remains a
separate kernel regression but is not required for every desktop milestone.
The result is translated to `StageResult(stage="recovery")`.

### Preboard Permit

`megrez_debug.py preboard` validates the plan and requires matching passed
`desktop` and `recovery` results. It reopens every artifact, reruns
the rootfs contract, checks both DTBs, checks the current Git commit, and
atomically publishes `preboard.json` with:

- schema and plan hash;
- exact hashes of the two prerequisite results;
- current commit;
- board transfer names and expected CRC32 values;
- recovery timeout and the sole allowed physical boot arguments.

The permit is invalid after any artifact, result, bootarg, DTB, or commit
change. It contains no mutable serial state and does not open the board.

### Asterinas-Only Network Installation

The physical install step is explicit because it overwrites only
`/dev/mmcblk0p2`. It requires the matching preboard permit, builds the existing
network installer initramfs from the exact Stage1/root image, serves the root
from a literal private-LAN HTTP URL, and boots the installer under Asterinas
with both `asterinas.mmc_write_partition2` and the exact root SHA-256 armed.

The host accepts installation only after the ordered
`DEBIAN_INSTALL_FETCH_OK` and `DEBIAN_INSTALL_PASS` markers name the exact
1 GiB size and SHA-256, followed by a fresh U-Boot prompt from automatic
recovery. The installer result is `StageResult(stage="install")` bound to the
same plan. The host never invokes Linux on the board and never writes U-Boot
persistent environment.

### Controlled Desktop Boot

The final physical gate requires matching preboard and install results. It
reuses the existing TFTP/board-session path, boots at most once, and requires
ordered GMAC, DNS, HTTPS, desktop, input, browser, and M6 markers. A bounded
validation boot uses automatic recovery and must return to U-Boot before the
result can pass. Only after this bounded gate succeeds may the operator use a
separate long-running desktop boot without the debug timeout.

## Failure and Recovery Rules

- No physical command runs without a current preboard permit.
- No install or desktop command performs `saveenv`, reset, or Linux boot.
- One monotonic deadline covers prompt detection, transfer/TFTP, boot,
  markers, and automatic recovery; substeps do not reset the budget.
- The first termination signal closes the serial path and publishes failure;
  the second exits immediately.
- If recovery does not return to U-Boot, publish `recovery-timeout`, release
  the device, and continue QEMU/host work. Manual reset is not normal flow.
- Failure output never contains `passed: true`, and stale passing evidence is
  invalidated before validation or launch.

## Verification Layers

1. Host tests freeze schema compatibility, one-open hashing, rootfs binding,
   result identity, stale evidence, signal behavior, and fail-closed permits.
2. PTY tests prove one `booti`, no persistent commands, split markers, one
   deadline, automatic U-Boot recovery, and serial cleanup.
3. One current-artifact M6 QEMU run proves the desktop, network, browser,
   framebuffer, keyboard, and tablet path.
4. One current-kernel QEMU reboot run proves recovery.
5. One controlled install and one bounded desktop boot prove only the Megrez
   hardware differences that QEMU cannot model.

The gate does not claim modern JavaScript compatibility. NetSurf's accepted
contract remains basic browsing, images, forms, and an explicit limited
JavaScript status; Firefox is a later milestone.
