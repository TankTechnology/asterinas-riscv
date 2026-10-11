# Debian runtime integration audit, 2026-10-11

## Scope and source identity

The integration checkout is `asterinas-pr179-merge`, on
`codex/debian-runtime-netlink` at `3c225dd16`. Its parent is main's PR #179
merge, `294c225ce`. The local branch contains the #181 netlink port and the
#182 Debian runtime gate; neither has been merged into `origin/main`. No board
boot was performed during this audit.

## Verified results

- RISC-V release kernel build completed after the #181 port.
- The `AUTO_TEST=netlink_route_netns` QEMU regression passed, including route
  namespace, uevent namespace, and libnl route/link/address checks. This is
  not proof of full generic-netlink family support or physical networking.
- Current main's Megrez DRM simulation passed at 1280x1024 and 1920x1080
  (`target/drm-evidence-20261011-main-rerun`). This is simulation evidence;
  it does not certify physical scanout or PowerVR acceleration.
- The Debian ext4 rootfs builder completed after supplying build dependencies
  and suppressing service starts during package installation. The temporary
  service-start policy is removed before publication.
- The corrected `systemd-ext4-m3` image contains `curl`, `iproute2`,
  `python3`, `login`, and `passwd` in its signed package lock.
- The full two-boot QEMU ext4/systemd gate passed with the current kernel and
  Stage1. Its receipt is `target/qemu-systemd-ext4-m3-current-gate-final`,
  with `DEBIAN_EXT4_READY` for both boots and
  `DEBIAN_EXT4_PASS boot=2 persist=1`.

## POSIX PR triage

The three PR heads were fetched as `origin/pr134`, `origin/pr103`, and
`origin/pr135` before review.

- **#134 (`c6d837a03`)** changes `SigAction` so `SA_RESETHAND` resets only
  the handler while retaining flags, restorer, and mask metadata. It also
  updates `/proc/<pid>/task/<tid>/stat` signal classification and adds a
  focused regression. This is a bounded semantic fix and is the first POSIX
  candidate after the Debian runtime gate; it still needs a current-main
  QEMU regression before merge.
- **#103 (`7f6952ebf`)** adds current-thread CPU-affinity migration and
  scheduler `Migrate` handling, with an SMP4 regression. It affects global
  scheduling and therefore must be tested on `SMP=4` before it can be used to
  explain Firefox or systemd behavior. It is a second, independent candidate.
- **#135 (`facd07ac5`)** is stacked: its history includes POSIX timers,
  namespaced clocks, RCU idle progress, socket-buffer prefaulting, range-lock
  handling, large-file `fcntl`, and the #103 affinity fix. It is not a safe
  single merge unit for Debian support. We will extract only a narrowly
  justified commit after the corresponding QEMU gate identifies a failure.

The evidence rule is therefore: merge #134 or #103 only when a current-main
QEMU test proves the named behavior and a physical counterpart is available;
do not merge #135 wholesale. PR #184 remains measurement infrastructure and
is deferred until these runtime gates are stable.

## Failed integration and corrected diagnosis

Four initial richer ext4/systemd QEMU runs failed before the interactive-login
gate at `DEBIAN_EXT4_FAIL reason=network-request`. The serial logs show
systemd service probes and apt update/install/remove/reinstall of `hello`
progressing before that failure. These are retained as failed diagnostics,
not acceptance evidence.

The generated `packages.lock` did not contain curl, iproute2, or Python. The
local dependency additions had accidentally changed `systemd-m2` rather
than `systemd-ext4-m3`. The curl failures therefore do not establish a DNS,
HTTP, TLS, or kernel-network defect. Earlier resolver/endpoint hypotheses
were unsupported; those speculative script changes have been reverted.

The ext4 profile now declares its runtime dependencies, while the M2 profile
retains its original package contract. A regression test first failed on the
missing ext4 dependencies and passed after this correction. The corrected
rootfs was rebuilt and its QEMU two-boot gate passed. Previous generated
images remain failed candidates and must not be used for board deployment.

## Authentication review

PR #182's custom login helper replaced Debian login authentication with a
fixed test credential and started a shell via setpriv. It did not prove a
normal PAM/login session. The local builder no longer installs or selects
that helper; its getty uses Debian's normal login path. A regression test
first failed with the override and passed after its removal. The ext4 gate
now records the account transition from its real `id`, passwd, permissions,
and `su - debian` checks; serial tty login remains a separate gate because
the current getty handoff is not stable enough to combine with filesystem
acceptance.

## Remaining work and ordering

1. Separate package lifecycle, network recovery, and graphical gates so a
   failure identifies one contract. Keep the complete integration pass as
   a separate acceptance requirement.
2. Revalidate the board management chain before a single watchdog-protected
   runtime run. This audit has no fresh UID, boot-id, or recovery receipt.
3. Complete fb0/Xorg/session/application/reboot acceptance on QEMU and then
   the board. Inspect #141/#140/#139 for missing increments instead of
   merging historical DRM snapshots over current main.
4. Apply the POSIX PR triage above only when a Debian application gate
   identifies the matching semantic gap; then repeat QEMU and board tests.
5. Use #184 for bounded performance baselines only after the runtime passes;
   benchmark infrastructure alone does not establish a speedup.

Space at the end of the audit was approximately 2.4 GiB on `/mnt/shared`.
Do not start another image build while space remains insufficient. Preserve
serial logs, JSON results, source changes, and recovery backups when pruning
failed generated images. Full `make check` remains red on existing kernel
Clippy diagnostics; targeted checks must not be presented as a full lint pass.

The later read-only management attempt `target/megrez-management-audit-20261011-v3`
also stopped at the bounded U-Boot wait with only `^C` observed
(`timed out waiting for U-Boot prompt`). It sent no menu choice, artifact,
probe, or reset command. A three-second serial read-only observation produced
no bytes afterward, so the board state remains unknown; this is not evidence
that RockOS or Asterinas is running.
