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

## 2026-08-26 current-main convergence

The integration branch was refreshed again against the active team main line,
`tank/main` at `ffb1bd062`. The merge keeps the modular DRM/GEM/KMS and virgl
implementation while porting main's hardware-cursor work into that newer
architecture. It also brings the current Debian, MMC, USB, PCI, and RISC-V
platform work into the same history instead of maintaining a parallel baseline.

The pre-merge DRM work was split into two reviewable commits:

- `41710d370` — reuse the active 2D scanout resource for repeated updates;
- `af8664810` — add the stable RISC-V software-display/Xfce acceptance path.

Conflict resolution and review added several hardening changes:

- serialize the complete virtio-gpu DMA request/queue transaction with sleeping
  mutexes and roll back partially created host resources;
- enumerate capsets by index and match their returned ids;
- validate framebuffer arithmetic, layouts, modifiers, offsets, pitches, and
  userspace enumeration capacities;
- restrict DRM mmap to GEM ranges owned by the open file;
- bound EXECBUFFER allocations and roll back PRIME/fence descriptors when
  userspace copyout fails;
- implement exclusive DRM-master ownership for global KMS/cursor operations and
  prevent render-node clients from enumerating legacy FLINK names.

Validation on the converged tree:

- RISC-V Sv39 `cargo osdk build`: pass;
- kernel ktests `framebuffer_extent`, `mmap_range`, and `cursor`: pass;
- virtio-gpu ktests `framebuffer_length` and `cursor`: pass;
- host cursor-gate unit tests: 10/10 pass;
- real U-Boot/QEMU cursor gate: pass, with two `UPDATE_CURSOR` traces and one
  `MOVE_CURSOR` trace. Evidence is generated under
  `target/qemu-uboot/drm-cursor/evidence/`;
- persistent-root software-display Xfce boot: pass. Xorg opened the real DRM
  card, selected the 640x480 `Virtual-1` mode, reached
  `XFCE_DRM_X11_CONNECT_OK`/`XFCE_DRM_XORG_READY`, and started `xfwm4`,
  `xfce4-panel`, `xfdesktop`, `xfsettingsd`, and `xfce4-session`. The visually
  confirmed framebuffer is
  `target/xfce-drm/guest-main-sync-final.png`.

The measured Xfce boot reached its systemd desktop target in 2 minutes 43.319
seconds: 10.533 seconds in the kernel and 2 minutes 32.786 seconds in userspace.
The full panel/dock render arrived later because software AIGLX/swrast under
RISC-V TCG remained CPU-bound. This result demonstrates a working, interactive
DRM desktop, but it is not yet a native-speed or consistently smooth experience.

Run the formal kernel build again after OSDK ktests and before packaging a QEMU
gate. The ktest workflow updates generic OSDK artifact links, so packaging
immediately after a ktest can accidentally select the test kernel rather than
the normal run kernel.

The final-reference GEM/host-resource cleanup and blocking virgl wait path were
completed on 2026-08-27; see the update below. Asynchronous `NOWAIT` support and
IRQ-driven fence completion remain separate follow-ups. The oversized
virtio-gpu and DRM dispatch modules should also be split in a separate
structural change.

## 2026-08-27 GEM lifetime and blocking wait completion

The DRM object lifetime now has one final-reference cleanup path. GEM handles,
framebuffers, PRIME dma-buf files, and resource-creation transactions each hold
an explicit reference. Dropping the last reference removes the FLINK name and
virtio-gpu resource mapping, then sends `RESOURCE_UNREF` after releasing DRM
spinlocks. Closing a DRI file also drains its framebuffer and handle ownership
and disables active scanout/cursor/context state before releasing DRM master.

PRIME export/import now keeps the same global GEM object alive across closing
the original handle. A dma-buf maps file offset zero to the object's actual
window within the shared VMO, including nonzero pool offsets, and honors the
Linux `DRM_CLOEXEC` and `DRM_RDWR` flag values. The shared VMO remains a bump
allocator intentionally: an mmap can outlive its DRM handle or dma-buf fd, so
reusing a freed span without a separate VMA lifetime reference could alias a
new object into an old mapping.

Blocking `VIRTGPU_WAIT` is no longer a no-op. It submits a fenced virgl NOP on
the same context timeline and waits for the fenced response, which makes all
earlier context commands complete before the ioctl returns. `NOWAIT` is
explicitly rejected with `EOPNOTSUPP` until the control queue can expose an
asynchronous completion query. `EXECBUFFER` likewise rejects syncobj/ring
dependency fields that the current driver cannot honor instead of silently
ignoring them.

Focused mini-root validation passed with the final kernel:

- PRIME export/mmap/close-original/import: `M20_PRIME_PASS` and
  `MINI_PRIME_RC=0`;
- raw virgl creation, transfer, fenced wait, and double buffering:
  `M16_VIRGL_RAW_PASS` and `MINI_RAW_RC=0`;
- Mesa virgl renderer: OpenGL ES 3.2, four changing frame checksums,
  `M19_EGL_DONE`, and `MINI_EGL_RC=0`.

The remaining synchronization work is performance and API completeness:
IRQ-driven used-ring draining, per-resource fence state, and nonblocking
`NOWAIT`. Device-global KMS state serialization and bounded page-flip event
queues are also still required before treating this as production-ready DRM.

## Integration outcome

The audit was acted on later on 2026-08-25. The dedicated
`codex/drm-main-sync` branch merged `origin/main` while retaining the mature DRM
and virtio-gpu implementations. The persistent-root systemd/Xfce pipeline now
boots on that converged kernel and reaches an accelerated virgl/glamor desktop.

The end-to-end result, commands, kernel defects found by Xorg, and current TCG
performance limit are recorded in
[`../xfce/XFCE-DRM-M1-report.md`](../xfce/XFCE-DRM-M1-report.md).

## 2026-08-27 KMS and unpublished-handle hardening

DRM-master ownership, scanout ownership, active mode dimensions, and KMS
updates now share one device-wide sleeping mutex. This closes the old gap where
two primary-node files serialized only their own state while both changed the
same hardware scanout. Closing or removing a framebuffer distinguishes the
owning file as well as its per-file framebuffer id, and `GETCRTC` no longer
publishes an id from another file's private namespace.

GEM handles returned by `GEM_OPEN`, `CREATE_DUMB`, PRIME import, and implicit `VIRTGPU_RESOURCE_CREATE` allocation are now staged as `PendingGemHandle` transactions.
A handle number and object reference are reserved without adding the handle to the per-file lookup table.
Successful userspace copyout publishes the handle.
Failure drops the pending transaction, releases the object, and rolls back the serialized bump allocation after successful host-resource cleanup.
If the host cannot confirm resource destruction, the allocation is quarantined so its pages cannot be reused while the host may still access them.
This prevents another thread sharing the DRM file from observing and using a handle that the creating ioctl later tries to roll back,
or from interleaving a later allocation that would make rollback leak pool space.

Focused real-guest validation passed:

- a deterministic kernel test confirmed that a pending allocation excludes another reservation until it is published or rolled back;
- read-only ioctl response pages forced concurrent `CREATE_DUMB` copyouts to return `EFAULT`;
  every reserved handle remained unqueryable and the next valid allocation still mapped at offset zero
  (`M20_PRIME_UNPUBLISHED_HANDLE_OK`);
- PRIME export, close-original, import, and mmap lifetime remained operational
  (`M20_PRIME_PASS`);
- raw virgl resource creation, transfer, fenced render, wait, and double-buffer
  checks remained operational (`M16_VIRGL_RAW_PASS`);
- the persistent-root software-display desktop reached every graphical, Xorg,
  X11, and framebuffer gate (`XFCE_DRM_PASS`) in 2 minutes 19.602 seconds.

The controlled DRM-versus-fbdev startup measurements are recorded in
[`../xfce/XFCE-DISPLAY-BENCH.md`](../xfce/XFCE-DISPLAY-BENCH.md). They do not
show a statistically meaningful TCG startup-time advantage for either path;
DRM's current value is Linux KMS/PRIME/virgl compatibility, while an eventual
smoothness result needs interaction/frame-latency measurements on the virgl
path rather than boot time.

Debian riscv64 Xorg, Mesa, GBM, EGL, and virgl userspace are already exercised
by the M19 runtime and work with this DRM implementation. The official Debian
desktop m3/m4 boot configuration still selects `xserver-xorg-video-fbdev` with
`bochs-display`, so Debian userspace is supported but that distribution's
default desktop integration has not yet switched to DRM.
