# DRM/main synchronization notes

**Audit date:** 2026-08-25

**DRM branch:** `track/drm` at `4899f8d5f`

**Main/NixOS baseline:** `origin/main` and `nixos/track/nixos` at `5eef48901`

## Purpose

This note records the initial branch audit performed after DRM-M21.
It identifies work that has landed on the main and NixOS tracks,
work that is still isolated on topic branches,
and a safe order for bringing relevant changes into the DRM track.

No branch integration was performed during this audit.
The remote references were fetched and compared read-only.

## Executive summary

The local DRM track contains the newest virtio-gpu work:
real Mesa virgl rendering,
per-file virgl contexts,
3D resource attachment,
fences,
atomic KMS,
page-flip events,
PRIME,
and the render node.

The main/NixOS track contains the newer general-purpose RISC-V platform work:
NixOS stage-1 and persistent-root boot,
systemd and Nix sandbox support,
PCI BAR allocation,
devtmpfs hardening,
USB/input work,
network and process namespaces,
and the pixel-verified Xfce desktop.

The two tracks have diverged for long enough that a direct merge is high risk.
The DRM track and main both contain versions of the early virtio-gpu and desktop work,
while only the DRM track contains the later virgl implementation.
A dedicated integration branch and subsystem-by-subsystem conflict resolution are required.

## Branch snapshot

| Ref | Commit | State |
|---|---:|---|
| `track/drm` | `4899f8d5f` | Local DRM-M21+, including the per-file virgl context fix |
| `origin/track/drm` | `e47ceb96c` | Remote DRM track, still at the recovered DRM-M15 Weston harness |
| `origin/main` | `5eef48901` | Current main line, including NixOS-N5 |
| `nixos/track/nixos` | `5eef48901` | Converged with `origin/main` |
| `origin/integration` | `ec73b53ca` | Old integration line; it is not the current convergence target |

Commit-graph counts for `track/drm...origin/main` are 72 commits on the DRM side
and 307 commits on the main side.
Patch-id comparison detects a number of equivalent commits on both sides,
so these numbers must not be interpreted as 379 independent changes.

The local and remote DRM refs also have non-linear history:
`track/drm...origin/track/drm` reports 180 commits on the local side
and 79 on the remote side.
The remote ref should therefore be treated as stale history,
not as a branch that can be fast-forwarded into the local DRM track.

## Local DRM progress that must be preserved

The following local commits are newer than the remote DRM branch
and do not have equivalents on main:

- `4899f8d5f` — create a legacy virgl context per open DRM file,
  propagate its context ID through 3D commands,
  and attach every 3D resource to it.
- `3bb2e5927` — return pollable virtio-gpu render fences.
- `931e944bd` — correct the virtio-gpu ioctl ABI,
  capset handling,
  and scanout backing behavior.
- `b0cb17f7a` — expose the DRM sysfs device tree expected by Mesa.
- `ba3d98c8a` and `1a35446c4` — atomic KMS and page-flip completion events.
- `7a919303e` and `ec38366b6` — GEM/render-node support
  and the virtio-gpu 3D wire protocol.

When resolving integration conflicts,
the current versions of `kernel/src/device/drm/`
and `kernel/comps/virtio/src/device/gpu/`
must be the starting point.
Main's early 2D virtio-gpu implementation must not replace them.

## Main/NixOS progress relevant to the DRM track

### Platform and device stability

- `288848e7e` — allocate RISC-V PCI BARs from device-tree PCIe memory ranges.
- `c2d0f449c` — harden RISC-V PCI BAR allocation.
- `7e6e0ee4e` — harden the PCI BAR assignment preflight.
- `d9274d629` — fix devtmpfs bootstrap-node creation.
- `fbbad9cad` — fix devtmpfs registration races.
- `80e2f4793` — harden the no-`/dev` regression harness.

These changes are directly relevant to reliable virtio-gpu PCI discovery
and desktop device-node creation.
They should be evaluated before importing desktop userspace assets.

### Input and interactive desktop

- `ac1d05134` — continue draining virtio-input events after `SYN_REPORT`.
  The DRM track contains a patch-equivalent fix at `0f1c1f2ca`.
- `815edc63e` — keyboard and terminal behavior,
  an `xkbcomp` compatibility stub,
  and IRQ logging reduction.
  The DRM track has related work at `2a0329a69`,
  but the patches are not fully equivalent and require a content comparison.
- `71d16eaef` and its prerequisite series — drive the USB boot keyboard
  from the xHCI event-ring interrupt.
- `a57d4e351` — record an end-to-end interactive GTK desktop result.
- `d30ef32bc` — run the Xfce desktop in the RISC-V guest
  with pixel-verified evidence.

The Xfce and Xorg assets are useful acceptance workloads,
but they should be imported only after their kernel prerequisites are reconciled.

### NixOS and general kernel progress

Main now contains:

- NixOS-N5 stage-1 initramfs to persistent ext2 root and systemd boot;
- `sandbox = true` coverage and a guest C++ toolchain gate;
- user, PID, network, mount, IPC, and UTS namespace work;
- seccomp filtering and TSYNC;
- networking and ARP-resolution fixes;
- Nix daemon and package-management compatibility work.

This work reduces the need for the DRM track to maintain a separate full Debian image.
The preferred long-term route is to run the DRM acceptance workload
on top of the main/NixOS userspace and boot pipeline.

## Topic branches not yet on main

The following NixOS topic branches each carry one commit not present on main:

| Branch | Tip | Content |
|---|---:|---|
| `nixos/feat-clone3-cgroup` | `ad181e7b7` | `clone3(CLONE_INTO_CGROUP)` |
| `nixos/feat-sched-reset-on-fork` | `887d811a4` | `SCHED_RESET_ON_FORK` and policy inheritance |
| `nixos/feat-sync-file-range2-rseq` | `f89f3ce53` | `sync_file_range2` and `rseq` |
| `nixos/fix-iovec-null-base` | `6a5ba3838` | Return `EFAULT` for a non-empty NULL iovec |
| `nixos/fix-wexited-wait` | `c1ca82b55` | Recognize the `WEXITED` wait option |
| `nixos/name-to-handle-at` | `a455235c5` | `name_to_handle_at` and `open_by_handle_at` |

These branches are based far behind current main.
They should be rebased and integrated by their owning track
rather than cherry-picked directly into DRM.

The following remote topic refs have no commits ahead of current main
and can be considered already absorbed or obsolete as integration sources:

- `nixos/alsa-pcm-abi`
- `nixos/fix/clock-getres`
- `nixos/fix/force-fault-signal-delivery`
- `nixos/fix/tmpfs-root-mode`
- `nixos/seccomp-bpf-filter`
- `nixos/virtio-sound-driver`

## Recommended synchronization sequence

1. Record `4899f8d5f` as the known-good DRM baseline.
   Keep the RISC-V OSDK build,
   raw virgl test,
   and Mesa/EGL multi-frame test as mandatory gates.
2. Create a dedicated integration branch from `track/drm`,
   for example `codex/drm-main-sync`.
   Do not rewrite or merge directly into the working DRM branch.
3. Reconcile PCI BAR and devtmpfs changes first.
   Verify virtio-gpu discovery over both virtio-mmio and virtio-pci.
4. Reconcile input and USB changes.
   Retain the DRM track's equivalent virtio-input fix
   and review the keyboard changes by content rather than commit subject.
5. Integrate the main/NixOS kernel baseline while resolving DRM conflicts
   in favor of the newer local DRM implementation.
6. Import the Xorg/Xfce acceptance workloads after the kernel converges.
7. Re-run the DRM tests without `ioctltrace` or info-level syscall logging
   before drawing performance conclusions.

## Conflict policy

Use the following ownership rule during integration:

| Area | Preferred source |
|---|---|
| `kernel/src/device/drm/` | Local `track/drm` first, then manually apply main fixes |
| `kernel/comps/virtio/src/device/gpu/` | Local `track/drm` first |
| PCI, namespace, networking, generic syscall code | `origin/main` first |
| virtio-input | Compare patch IDs and retain the newest behavior from both tracks |
| Xorg/Xfce/NixOS tooling | `origin/main` first, adapted to the converged kernel |
| DRM test harnesses | Local `track/drm`, with debug-only tracing disabled for performance runs |

## Deferred work

A low-noise M19 performance harness was started during the audit
but deliberately deferred when the focus moved away from the full Debian rootfs.
The experiment confirmed that unpacking the approximately 200 MiB Debian initramfs
under RISC-V TCG dominates startup time before the desktop benchmark begins.

Future performance work should use the main/NixOS persistent-root pipeline
or a smaller runtime closure.
It should not use the full Debian initramfs as the default interactive path.

## Refresh commands

The following read-only commands reproduce the high-level audit:

```sh
git fetch --all --prune
git rev-list --left-right --count track/drm...origin/main
git rev-list --left-right --count track/drm...origin/track/drm
git log --oneline track/drm..origin/main
git cherry -v origin/main track/drm
```

Always record the compared commit IDs when updating this note,
because ahead/behind counts change as main advances.
