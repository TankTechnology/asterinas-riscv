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
Cleanup-only resources are hidden from normal ioctls.
Resource ids that outlive their GEM object remain in a device-level retry queue and are retried before later resource creation.
Each retry pass visits every resource that was queued when the pass began without allocating a snapshot or letting one persistent failure starve later resources.
This prevents another thread sharing the DRM file from observing and using a handle that the creating ioctl later tries to roll back,
or from interleaving a later allocation that would make rollback leak pool space.

Focused real-guest validation passed:

- deterministic kernel tests confirmed both pending-allocation serialization and the rollback-versus-quarantine policy;
- a deterministic kernel test confirmed that failed cleanup does not block later resources and that resources queued during a pass remain for the next pass;
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

## 2026-08-27 IRQ-driven virtio-gpu control completion

Runtime virtio-gpu control requests no longer busy-poll the used ring. The
control queue now keeps its virtqueue behind an IRQ-safe spinlock; the queue
callback removes the completed used entry, records it without allocating, and
wakes the submitting task through a `WaitQueue`. A sleeping mutex still
serializes commands and protects the shared small-command DMA buffer, so this
is a bounded one-request-at-a-time prerequisite rather than asynchronous fence
submission yet.

The IRQ path examines at most one device-written used entry and never logs
while holding the IRQ-disabled queue lock. A malformed token or response length
is handled only after releasing the lock. A valid token with an invalid length
returns its descriptor chain before waking the submitter with an error. An
invalid token cannot prove that the submitted chain is no longer device-owned,
so the request and its DMA buffers deliberately remain alive instead of risking
DMA into freed memory. The legacy virtqueue pop helper also bounds how many
malformed entries it skips in one call to the device-visible ring size.

Early display discovery and the boot test pattern continue to poll because the
task scheduler is not available throughout that initialization window. The
polling-to-IRQ transition takes the same operation mutex as submissions, which
prevents an IRQ from consuming the response of a request that already chose
the polling path. Device notification uses a separately cloned virtqueue
notifier, allowing its MMIO write to happen after the IRQ-disabled queue lock
is released. Control queues with fewer than the two descriptors required for a
request/response chain are rejected during initialization.

The RISC-V kernel build passed. The U-Boot mini-root harness repeatedly stalled
before the first Asterinas log after the host reboot, so the same mini virgl
initramfs was run through OSDK direct boot with `virtio-gpu-gl-device`. That
real guest passed PRIME lifetime checks and all raw virgl gates. In particular,
GET_CAPS versions 0, 1, and 2 all completed (the earlier wake-only experiment
hung before version 2), followed by resource creation, transfers, fenced and
unfenced execution, blocking `WAIT`, pixel round-trip, rendered-pixel, and
double-buffer checks. The final markers were `M20_PRIME_PASS`,
`M16_VIRGL_RAW_PASS`, and `MINI_RAW_RC=0`.

The next synchronization slice is to allow multiple in-flight control
requests, associate their descriptor tokens with persistent fence state, and
implement nonblocking `VIRTGPU_WAIT_NOWAIT`. This commit does not change the
current synchronous EXECBUFFER/fence ABI by itself.

## 2026-08-27 token-routed control completions

The runtime control queue no longer has a device-wide operation mutex or one
global completion slot. Each submitted descriptor head token is entered in a
fixed-size token map with its own completion state and wait queue. The IRQ
handler drains a bounded number of coalesced used entries, removes the matching
request from the token map, and wakes only that request. If the descriptor ring
is full, submitters sleep until one completion releases a chain instead of
panicking or spinning.

Each pending request owns references to every DMA buffer in its descriptor
chain. This keeps device-visible memory alive independently for concurrent
requests and preserves the earlier invalid-token safety rule. The IRQ path
does not allocate, perform notification I/O under the queue spinlock, or wake
an unbounded queue of descriptor waiters. Small fixed commands still share one
DMA page behind a separate sleeping mutex, while independently allocated
`SUBMIT_3D` and variable-size capset buffers can use the concurrent queue.
Early boot remains polling-compatible.

Validation on the resulting tree:

- the RISC-V kernel build passed with only the seven pre-existing unrelated
  warnings;
- token-map kernel tests cover out-of-order completion routing and safe token
  reuse; the component ktest image compiled, while the current host OSDK test
  launcher selected `qemu-system-x86_64 -machine virt` and therefore could not
  boot the RISC-V test image;
- a real OSDK RISC-V guest with `virtio-gpu-gl-device` passed the baseline raw
  virgl suite and then 12 simultaneous independent virgl clients:
  `M21_CONTROLQ_CONCURRENT_RESULT passes=12 failures=0` and
  `M21_CONTROLQ_CONCURRENT_PASS`.

The post-implementation review also hardened four adjacent failure paths:
state-changing commands now accept only `OK_NODATA`; resource IDs report
exhaustion instead of wrapping; descriptor lengths are validated before queue
mutation and cannot truncate or overflow; and a malformed cursor completion is
returned as an error instead of leaving the caller in a polling loop. The real
guest stress gate was rerun after these fixes and again passed all 12 clients.

Two broader items remain follow-up work rather than blockers for this slice.
Failed cleanup of a replaced scanout or cursor resource still needs a persistent
deferred-retry set, and spinlock-protected users outside the GPU driver still
need migration to the queue's non-logging pop helper and detached notifier.

The ioctl ABI is still synchronous: callers receive a response only after the
control ticket completes, and `VIRTGPU_WAIT_NOWAIT` remains unsupported. The
next slice is to keep fence state beyond the submitting syscall, expose a
nonblocking completion query, and connect it to resource/context timelines.

## 2026-08-27 asynchronous resource fences and NOWAIT

`VIRTGPU_EXECBUFFER` no longer waits for a fenced `SUBMIT_3D` response before
returning.
It creates a persistent fence around an owned control ticket, queues the
command, and lets the control-queue IRQ signal both blocking waiters and
pollers.
An output fence fd is pending until token completion, although a fast device may
signal it before the ioctl copies the descriptor to userspace.
It becomes readable through `Pollee` notification when the matching descriptor
token completes.
Response DMA synchronization and validation remain in task context.

Each GEM object named by an EXECBUFFER handle list tracks all currently
unfinished fences, rather than only the most recent fence. This preserves
cross-context safety when a shared object is used by overlapping submissions.
Because the virgl command stream is opaque and untrusted clients can omit a
referenced object from that list, the manager also keeps a conservative
device-wide set of all in-flight fences. Context teardown and final GEM release
wait on that set before destroying host-visible state.
EXECBUFFER capture is serialized with final GEM release; closing the last
reference waits for every retained fence before issuing `RESOURCE_UNREF`, so
the host cannot render through a resource that has already been destroyed.
Completed fences are pruned on later submission or explicitly consumed by
`WAIT`.

Every successful `RESOURCE_ATTACH_BACKING` also retains an owning reference to
the DMA pool until `RESOURCE_UNREF` succeeds. If unref fails, the owner remains
live rather than exposing freed guest memory to a host device that may still
retain the backing table.

`VIRTGPU_WAIT_NOWAIT` is now implemented.
It returns `EBUSY` while any tracked resource fence is pending and succeeds
without sleeping once all are complete.
Blocking `WAIT` consumes the same fence set instead of submitting an additional
NOP barrier. Fence and resource ID allocators use checked atomic updates and
report exhaustion rather than wrapping into old IDs.

Validation on the final tree:

- the RISC-V kernel compiled with only the seven pre-existing unrelated
  warnings;
- a real RISC-V OSDK guest with `virtio-gpu-gl-device` passed asynchronous
  fence-fd polling, the pre/post-completion NOWAIT probe, rendered-pixel and
  double-buffer checks, and the complete raw virgl suite;
- the 12-client control-queue stress gate again reported
  `M21_CONTROLQ_CONCURRENT_RESULT passes=12 failures=0` and
  `M21_CONTROLQ_CONCURRENT_PASS`, with no invalid completion or panic.

The RISC-V ktest build currently stops in the unrelated syscall path because
`kernel/src/syscall/riscv_flush_icache.rs` calls an `ostd::arch::flush_icache`
symbol that this branch does not export. The normal RISC-V build exercises the
DRM changes successfully; fixing that branch-level ktest mismatch is tracked
separately from this fence slice.

The next merge-preparation work is to add checked, format-aware validation for
userspace-supplied 3D resource geometry and transfer ranges, resolve deferred
cleanup for directly presented scanout/cursor resources, and finish generic
virtqueue I/O migration away from logging/error paths under spinlocks. After
that, the DRM stack can be rebased onto the latest main branch and split into
upstream-sized commits.

## 2026-08-27 Xfce DRI3 direct rendering

The Xfce acceptance path now distinguishes Xorg's server-side glamor renderer
from the renderer selected by an ordinary GLX application. Before this slice,
Xorg used virgl but the application reported `llvmpipe`, so the desktop had
only partial acceleration.

Two missing Linux interfaces blocked DRI3 clients. The DRM sysfs minor nodes
did not provide the top-level `uevent`/`DEVNAME` consumed by libdrm's
`drmGetDeviceNameFromFd2`, and the primary node did not implement the
`GET_MAGIC`/`AUTH_MAGIC` authentication handshake used when Xorg reopens and
authorizes a client fd. Both interfaces are now present. Authentication state
also gates `GEM_FLINK` and `GEM_OPEN`, and master reuse is restricted to the
owning process or a caller with `CAP_SYS_ADMIN`.

The completed guest reports `Using DRI3 for screen 0`,
`XFCE_GL_DIRECT yes`, `XFCE_GL_RENDERER virgl`, a checked output pixel,
`XFCE_GL_BENCH_PASS`, and `XFCE_DRM_PASS`. The 30-frame shader sample improved
from 0.609 FPS on llvmpipe to 5.369 FPS on virgl in one TCG comparison. The
full diagnosis, control-queue regression bisect, and test method are recorded in
[`../xfce/XFCE-DRM-M2-report.md`](../xfce/XFCE-DRM-M2-report.md).

The remaining DRI3 `Illegal resource`/`CREATE_OBJECT` warning was traced to a
shared GEM resource that was attached only to Xorg's virgl context, not to the
importing GLX client's context. Per-file resource membership is now maintained
across handle import/close and rechecked from each `EXECBUFFER` BO list. The
Xfce harness reports `command-stream: OK` and rejects all three former
virglrenderer error markers.

## 2026-08-27 atomic UAPI and KMS object correction

The first architecture-and-validation pass found that the M17 kernel and its
raw test shared the same non-Linux interpretation of `drm_mode_atomic`.
They used the second field as a flattened property count, ignored the
per-object count array, and extended the structure to 72 bytes.
That produced a private ioctl number, so the earlier self-consistent gate did
not prove compatibility with an unmodified libdrm atomic request.

The wire type is now the Linux 56-byte structure and produces ioctl command
`0xc03864bc`.
The parser consumes unique object ids and per-object property counts, validates
the complete bounded transaction, and publishes property state only after the
fallible framebuffer presentation succeeds.
CRTC, connector, encoder, and primary-plane ids are now distinct.
This removes the earlier ambiguity that allowed a property to be accepted when
it applied to any object type sharing id 1.

The rebuilt Sv39/SMP=4 M17 guest passed 44/44 checks, including negative tests
for an invalid property/object pairing and a nonzero reserved field.
The complete Debian Mesa/Xorg/Xfce virgl path then passed DRI3 direct rendering,
pixel validation, the command-stream error gate, and `XFCE_DRM_PASS`.
The shader sample reported 10.210 FPS; total boot time was unusually high under
the concurrent host load and is not used as a performance result.

The same pass hardened query-array capacities and property blobs.
Blob allocation is bounded and fallible, creation rolls back if its result
cannot be copied to userspace, ownership belongs to the creating DRM file, and
committed KMS state retains its reference after userspace destroys the blob.
Unsupported asynchronous atomic commits now return `EOPNOTSUPP` instead of
being silently executed synchronously.

After the capability and lifetime hardening, the complete Xfce gate passed
again with DRI3 direct rendering, the virgl renderer, the expected output
pixel, 30 submitted frames, a clean command stream, and `XFCE_DRM_PASS`.
The sample reported 9.214 FPS while other QEMU guests were active on the host,
so this run is retained as a semantic regression result rather than a
performance comparison.

The low-cost validation tiers and the planned separation of DRM core state from
the virtio-gpu backend are recorded in
[`../drm/VALIDATION.md`](../drm/VALIDATION.md).

## 2026-08-27 transactional KMS state follow-up

Atomic commits now build a complete proposed KMS state from the last committed
state plus the request, validate the resulting topology, mode, framebuffer,
and plane geometry, and only then touch virtio-gpu hardware. Property values
are published under one lock after the fallible hardware operation succeeds.
TEST_ONLY follows the same validation path without publishing state or issuing
commands.

The legacy SETCRTC and PAGE_FLIP paths now mirror their successful state into
the same property model. Closing the scanout owner or removing its active
framebuffer resets that model to a coherent disabled state. Framebuffer IDs are
allocated device-wide, preventing a stale property value from aliasing a new
per-file framebuffer.

Pipeline shutdown is no longer a validation stub. `ACTIVE=0` and a complete
disconnect issue a real virtio-gpu scanout disable, publish the disabled state,
and can subsequently restore the original mode and primary plane. Modes are
decoded as exact 68-byte `drm_mode_modeinfo` values and checked for valid timing
relationships. Because the current backend presents a complete framebuffer,
the plane validator accepts only an origin-zero, unscaled, uncropped rectangle
whose dimensions agree with both the framebuffer and mode; unsupported crop or
scale requests fail explicitly.

The M17 Sv39/SMP=4 guest passes 55/55 checks, including invalid timing,
invalid geometry, active-without-plane, TEST_ONLY disable, real disable,
restore, and full-disconnect cases. The complete NixOS Xorg/Xfce regression
also passes after the stricter validation: DRI3 direct rendering, virgl,
expected pixel read-back, 30 rendered frames, clean command stream, and
`XFCE_DRM_PASS`. The sample measured 7.149 FPS and is retained as a semantic
regression run rather than a cross-run performance conclusion.

Property blobs now have both per-blob and device-wide accounting (64 KiB per
blob, 4 MiB and 256 live blobs device-wide). Virtio-gpu scanout replacement
prepares and flushes the new resource before `SET_SCANOUT`; failed unrefs are
retained for retry, and fixed-size control commands reject truncated device
responses instead of consuming stale DMA-buffer contents.

## 2026-08-28 device-wide lifetime accounting and M22

DRM fdinfo now exposes explicitly device-scoped GEM, host-resource, virgl
context/attachment, fence, backend ownership, scanout/cursor, and deferred
cleanup diagnostics. The implementation derives structural counts from the
owning containers and keeps O(1) aggregates for GEM owners, context
attachments, and fence associations. Diagnostics and context tracking were
split out of the main DRM coordinator, and the first 64 MiB contiguous pool
allocation no longer runs under a spinlock.

M22 repeatedly creates a mapped GEM buffer, creates and attaches a host virgl
resource, exports/imports it with PRIME, submits and verifies a successful
fenced command, closes the worker while retaining the dma-buf, and finally
closes the dma-buf. All 32 rounds restored every reclaimable counter to the
baseline. The DUMB-pool watermark advanced by exactly 524,288 bytes, exposing
the current non-reusing pool as a separate long-run capacity limitation rather
than misclassifying it as a live-object leak.

On the corrected tree, the RISC-V Sv39 build and `cargo osdk test` pass, M17
passes 55/55 checks, and the complete Xorg/Xfce gate reports DRI3 direct
rendering, virgl, a clean command stream, and `XFCE_DRM_PASS`. The one-run
30-frame sample was 17.765 FPS and is retained only as semantic evidence. See
[`DRM-M22-report.md`](DRM-M22-report.md) for the counter contract, staged-close
test, evidence, review corrections, and remaining pool-lifetime work.

## 2026-08-28 mapping-safe DUMB-pool reuse

The 64 MiB pool is no longer a monotonic bump allocator.
Page-aligned spans are allocated first-fit, coalesced on final release, and
cleared before reuse.
A span is owned jointly by the GEM object, surviving DRM or PRIME VMAs, and
any virtio-gpu resource whose host backing lifetime is not yet confirmed
closed.
The generic VMA lifetime token follows fork, split, and remap, and prevents
adjacent mappings with different object owners from merging.

M22 now proves both DRM and PRIME mappings survive handle/fd close without
aliasing, then observes reuse of the original offset after `munmap`. Its 4,200
fast allocation cycles exceed 64 MiB cumulatively, and all 32 full
GEM/PRIME/virgl/fence rounds reuse offset zero.
Final live pool usage is zero; the 32 KiB high-water reflects two
simultaneously live buffers in the mapping lifetime checks.

RISC-V Sv39 build and ktest pass, M22 reports 42 checks and 32/32 rounds, and
M17 remains 55/55. The Xfce DRI3/virgl functional gate also passes. Its current
0.824 FPS sample ran while another RISC-V QEMU saturated roughly 3.3 host CPU
cores, so it is recorded as load-contaminated functional evidence rather than
a performance regression measurement.

## 2026-08-28 virgl resource contract and partial atomic flips

The converged DRM branch now validates virgl resource creation and transfer
requests against guest-owned metadata before submitting them to QEMU. The
validated state contains the Gallium target, format, dimensions, array size,
mip/sample parameters, flags, and GEM backing size. Texture and buffer
transfers reject nonexistent mip levels, boxes outside target geometry,
backing-offset overflow, and—for the common linear 32-bit formats—final-byte
overflow at every mip level. Other formats retain virglrenderer's format-aware
IOV validation. M22 exercises both legal TO/FROM transfers and malformed
requests, then requires all lifetime counters to remain at baseline. Its
four-hart Sv39 run passed 50 checks, zero failures, and 32/32
resource-lifetime rounds.

The Mesa acceptance client was corrected to discover the globally unique CRTC,
connector, and plane ids, use Linux's 56-byte `drm_mode_atomic` layout, and
group properties per KMS object. This exposed an Asterinas event-validation
bug: a plane-only `FB_ID` update was rejected unless the request also listed a
CRTC object. Atomic events now derive their target from the plane's inherited
CRTC routing. The real virgl renderer subsequently completed four frames: the
first used a complete modeset and the next three used `FB_ID`-only commits;
all commits and flip events succeeded, sequences advanced from 0 through 3,
and all four read-back checksums differed.

The compact Mesa test root now lives on a second 4 KiB-block ext2 disk instead
of a 165 MiB initramfs. A small bootstrap initramfs mounts and chroots into that
disk. This removes large initramfs decompression from each iteration while
retaining the Debian Mesa/LLVM closure. The complete PRIME, raw virgl, and
Mesa/GBM/KMS gate ended with `MINI_VIRGL_PASS`.

## 2026-08-28 public DRM clients and nonblocking atomic commits

The compact RISC-V validation image now includes Debian's `modetest` and
`kmscube`, including their discovered ELF, Mesa, GBM, and DRI dependencies.
The public-client gate enumerates KMS resources, performs both legacy and
atomic modesets with `modetest`, then renders four virgl frames through both
the legacy and atomic `kmscube` paths. It rejects software rendering, failed
commits, missing atomic support, and incomplete output rather than relying on
the exit status alone. The existing PRIME, raw virgl, and EGL checks remain in
the complete gate.

`kmscube` exposed that merely accepting `DRM_MODE_ATOMIC_NONBLOCK` while
waiting synchronously—or allowing only one in-flight request—does not satisfy
the public UAPI. Each DRM file now has a bounded FIFO for nonblocking atomic
commits. Validation and the logical KMS/property state exchange finish before
the ioctl returns; pinned framebuffer backing keeps queued scanouts alive;
the worker applies hardware changes in submission order and publishes the
reserved page-flip event only after a successful hardware update. File close
waits for the queue, and dropping DRM master is rejected while work remains,
so an old master's worker cannot overwrite a new master's scanout. Legacy KMS
operations fail with `EBUSY` while queued atomic hardware work is
pending so that they cannot overtake it. Event capacity is reserved before
logical state publication, preventing accepted commits from overbooking the
per-file event queue. Invalid `TEST_ONLY | PAGE_FLIP_EVENT` requests are now
rejected. `DIRTYFB` refreshes only the currently active framebuffer and no
longer performs an unintended logical page flip. The work and event queues
live in a focused module rather than adding more coordination machinery to the
DRM root module.

The final Sv39 run passed public resource enumeration, legacy and atomic
`modetest`, legacy and atomic virgl `kmscube`, PRIME sharing, raw virgl, and
Mesa EGL/GBM/KMS rendering, ending in `MINI_VIRGL_PASS`. M22 separately passed
50 checks, zero failures, and all 32 resource-lifetime rounds. Frame rates from
these TCG functional runs are not treated as performance measurements.

Known compatibility gaps remain explicit: the virtual CRTC advertises no
gamma table. At this point in the history, completion events still followed
virtio-gpu command completion rather than a display clock; the later display
clock section below closes that pacing gap.

## 2026-08-28 atomic explicit synchronization

The atomic KMS pipeline now exposes Linux-compatible plane `IN_FENCE_FD` and
CRTC `OUT_FENCE_PTR` properties. They remain one-shot values (`-1` and `0`
when queried) rather than becoming persistent KMS state. Test-only and failed
commits initialize valid output pointers to `-1`; successful commits return a
close-on-exec sync-file-like fd. Nonblocking workers wait for input
dependencies, perform the virtio-gpu scanout update in FIFO order, and signal
the output fence at the same completion stage as the page-flip event.

Mesa's explicit KMS path also required
`VIRTGPU_EXECBUF_FENCE_FD_IN`. The execbuffer ioctl now accepts an input and
output fence in the shared `fence_fd` field, waits without holding global GPU
resource-transaction locks, and preserves the existing asynchronous render
out-fence behavior. This was discovered by public atomic `kmscube`, not by a
driver-specific ioctl test.

The Sv39 public-client gate enumerated both properties and passed legacy and
atomic `modetest`, then rendered four real virgl frames through both legacy
and atomic `kmscube`. The dedicated Mesa client performed a test-only commit,
four nonblocking commits with output-fence polling, and chained each completed
output fence into the next plane update; all four frames and checksums passed.

## 2026-08-28 software vblank clock and paced completions

The virtio-gpu transport has command-completion fences
but no physical vblank interrupt.
The DRM implementation now isolates that limitation
behind a device-wide `VblankClock`,
following the same timer-backed model that Linux DRM offers
to drivers without a hardware vblank source.
The clock advances from monotonic time while scanout is active
and freezes without resetting its sequence when the CRTC is disabled.

Legacy page-flip events are scheduled asynchronously for the next refresh
boundary. Synchronous and nonblocking atomic paths now release
`OUT_FENCE_PTR` and publish `DRM_EVENT_FLIP_COMPLETE` at that same boundary,
using one sequence and timestamp source rather than incrementing a counter per
submitted commit.
Slow rendering can therefore skip elapsed sequence numbers,
as a real display clock does.
The synthesized mode's pixel clock also includes blanking totals,
so libdrm derives the same 60 Hz rate used by the KMS clock.

The post-change Sv39 gate again passed public legacy and atomic `modetest` and
`kmscube`, PRIME sharing, raw virgl rendering, and the Mesa explicit-sync
client, ending in `MINI_VIRGL_PASS`.
TCG render throughput remains a functional measurement only;
the important result is that public clients continued to make progress
while events and fences were paced by a display clock.

## 2026-08-28 vblank and CRTC sequence UAPI

The DRM primary node now implements Linux's refresh-sequence interfaces:
blocking and event forms of `DRM_IOCTL_WAIT_VBLANK`,
`DRM_IOCTL_CRTC_GET_SEQUENCE`, and `DRM_IOCTL_CRTC_QUEUE_SEQUENCE`.
Relative, absolute, and next-on-miss targets use the shared monotonic
`VblankClock`; queued events carry the Linux 64-bit sequence/timestamp layout.
Render nodes reject these KMS-only ioctls, unknown CRTC ids and unsupported
flags return errors, and wait/queue requests reject an inactive CRTC.
The matching timestamp, high-CRTC-index, and CRTC-in-event capabilities are
reported through `DRM_IOCTL_GET_CAP`.

The completion scheduler is ordered by target sequence and submission order.
It sleeps directly until its earliest absolute target instead of waking on
every refresh; a queue generation wakes and retargets the worker when an
earlier event is submitted or a file closes. Legacy page flips have a separate
RAII reservation, so a second flip cannot replace the first before its target
refresh. Blocking atomic commits now reserve event capacity before waiting on
an input fence or changing hardware state, matching the nonblocking path and
preventing a post-commit `EBUSY` result.

The dedicated RISC-V Mesa client verified the 64-bit ABI sizes, all three
capabilities, inactive behavior, a blocking relative wait, an asynchronous
legacy vblank event, monotonic sequence queries, out-of-submission-order CRTC
sequence events, invalid ids and flags, and render-node rejection. The active
run reported `initial=503`, blocking wait `504`, vblank event `505`, earlier
queued event `510`, and later event `517`. The complete Sv39 gate also passed
public legacy and atomic `modetest`/`kmscube`, PRIME, raw virgl, explicit sync,
and four distinct Mesa frames with a real virgl renderer, ending in
`MINI_VIRGL_PASS`.

For this gate, `build_mini_disk.sh` is the authoritative rebuild step: it
regenerates both the ext2 Mesa root disk and the small bootstrap cpio consumed
by `boot_mini_debug.py`. Running only `build_mini_rootfs.py` updates a staging
tree but leaves the QEMU disk unchanged.

## 2026-08-28 DRM syncobj and timeline synchronization

The DRM file state now owns Linux-compatible syncobj handles backed by
shareable synchronization objects. Binary reset/signal/wait operations and
timeline points are supported together with whole-syncobj fd sharing,
`sync_file` import/export, transfer, query, wait-for-submit, wait-available,
wait-all, and one-shot eventfd notification. `DRM_CAP_SYNCOBJ` and
`DRM_CAP_SYNCOBJ_TIMELINE` are reported on DRM clients. `WAIT_DEADLINE` is
accepted as a compatibility hint but is not yet propagated into GPU scheduling.

`VIRTGPU_EXECBUFFER` accepts Linux's input and output syncobj arrays, including
the older eight-byte descriptor stride and optional timeline points. Input
fences are consumed before GPU resource locks are acquired. Output points own
the actual asynchronous virtio-gpu completion fence, and their publication is
serialized in the same global order as control-queue submission. Binary input
syncobjs can request reset after a successful submission.

The fence layer separates passive state inspection from active device polling,
so spinlock-protected resource and timeline bookkeeping never drives virtio
callbacks. Timeline chains are flattened to unique unsignaled leaves; this
keeps polling linear, prevents recursive completion from exhausting the kernel
stack, and releases dependency graphs after signaling. Per-file handles,
timeline points, ioctl arrays, and retained eventfd watchers have explicit
bounds. Eventfd kernel signaling uses an IRQ-safe counter and preserves Linux's
overflow state.

The new static RISC-V client validates binary and timeline behavior, absolute
timeouts, future submission wakeups, transfer, shared object fds, `sync_file`
round trips, eventfd notification, concurrency, descriptor validation, input
reset, and a real virtio-gpu EXECBUFFER output point. The full Sv39 QEMU gate
reported `M19_SYNCOBJ_PASS binary timeline transfer share sync_file eventfd
concurrency execbuffer` and `MINI_SYNCOBJ_RC=0`, then also passed PRIME, raw
virgl, public legacy/atomic KMS, explicit synchronization, vblank UAPI, and four
distinct Mesa virgl frames, ending in `MINI_VIRGL_PASS`.

## 2026-08-28 submission-safe syncobj publication and lifetime stress

`VIRTGPU_EXECBUFFER` now reserves every recoverable output-syncobj timeline
slot before submitting the command to virtio-gpu. The reservation is an RAII
publication token for both binary and timeline outputs: any validation,
allocation, resource-lookup, or transport error before submission releases all
unpublished capacity, while successful submission consumes every token without
returning a partial `ENOSPC` or `ENOMEM` result. Concurrent timeline producers
must preserve capacity promised to outstanding tokens.

Long fence chains no longer flatten and re-register callbacks on every
historical leaf. Each append adds at most two dependency edges, and completed
chain nodes propagate through an iterative queue. This changes construction
from quadratic callback growth to constant work per append while retaining
non-recursive interrupt-stack behavior. Queue capacity is reserved before a
chain is published and is capped at 16,384 outstanding chain nodes system-wide.
Fence callbacks have cancellable RAII registrations, so replacing or resetting
a syncobj removes callbacks attached to obsolete pending fences. Eventfd
notification similarly takes
one syncobj readiness snapshot and checks all watchers in one linear pass,
rather than scanning all timeline points once per watcher. A system-wide RAII
quota of 16,384 retained eventfd watchers complements the existing 4,096 limit
per syncobj, bounding memory across files and objects.

Kernel tests cover reservation publication, cancellation, concurrent appends,
retention-limit accounting, in-flight waiter lifetime, and a 4,096-node fence
chain. The static RISC-V ioctl client now also verifies that an exported
syncobj survives destruction of its original handle and imports through a
second DRM fd, performs 256 same-object timeline transfers, and reaches the
exact eventfd-watcher boundary without hanging on a blocking eventfd read.
The final Sv39 gate reported `M19_SYNCOBJ_PASS binary timeline transfer share
sync_file eventfd concurrency execbuffer lifetime stress bounds`,
`MINI_SYNCOBJ_RC=0`, a real Mesa virgl renderer, four distinct frames, and
`MINI_VIRGL_PASS`.

## 2026-08-28 allocation-safe publication and fence teardown

The syncobj commit path now preallocates every callback, two-edge chain node,
completion-queue slot, watcher notification, and multi-output publication
before a virtio command is submitted. Timeline signal batches similarly
reserve all target updates and one shared signaled fence before publishing any
point, so allocation failure cannot leave a partially signaled array. Null
timeline-point pointers now return `EFAULT`, and eventfd watchers retain a
typed `EventFile` rather than an erased file object that must be downcast at
notification time.

The device-completion handshake is safe when completion races ticket
attachment, including the synchronous callback path. Fence callbacks run
outside their IRQ-disabled list lock. Long pending chains are torn down through
the same pre-reserved iterative work queue used for completion, avoiding a
recursive 4,096-node destructor. Active chain polling is O(1): it caches a
cloneable virtio control-queue poll handle whose lifetime is independent of an
individual consumed command ticket, so a completed leaf cannot strand another
dependency.

Resource use is bounded at the system level as well as per ioctl. Fence
callbacks are capped at 16,384; in-flight execbuffer command streams at 64 MiB;
and retained GEM-object/fence associations at 262,144. The execbuffer quota is
owned by the fence until its ticket-owned DMA stream is released. Resource
association vectors, syncobj waiter lists, handle decoding, and output
publication storage use fallible pre-submit reservations, so a userspace-sized
request returns `ENOMEM` or `ENOSPC` rather than reaching the kernel allocation
failure handler after submission.

The ioctl regression client submits one real command to two timeline outputs
and waits for points 13 and 17, exercises binary/timeline transfer and sharing,
eventfd and concurrency, null-pointer rejection, lifetime, stress, and quota
boundaries. The final Sv39 run in
`/tmp/drm-syncobj-post-review-gate.log` reported
`M19_SYNCOBJ_PASS binary timeline transfer share sync_file eventfd concurrency
execbuffer multi_output lifetime stress bounds`, `MINI_SYNCOBJ_RC=0`, successful
legacy and atomic `modetest`/`kmscube`, PRIME, raw virgl, and EGL, ending in
`MINI_VIRGL_PASS`. These remain functional correctness gates; TCG frame timing
is not used as a GPU-performance benchmark.

The final persona review found and drove fixes for stale leaf polling, early
execbuffer-quota release, and three user-controlled infallible allocation
sites. Remaining review items are architectural cleanup rather than confirmed
runtime defects: split the large DRM root/syncobj modules and decompose the
resource-create and execbuffer ioctl transactions into narrower typed stages.

## 2026-08-28 typed execbuffer transaction and DRM module boundaries

The first architectural cleanup split Linux core/KMS wire definitions from
the DRM composition root into `device/drm/uapi.rs`, and moved virtio-gpu
execbuffer decoding and submission into `device/drm/virtio_gpu/execbuffer.rs`.
The execbuffer path now has explicit decoded, prepared, submitted, and
published states. Userspace arrays, command memory, output syncobj capacity,
the completion fence, and the optional out-fence fd are all prepared before
the irreversible virtio submission. Dropping a prepared response signals its
unpublished fence as failed and rolls back an installed fd; after submission,
publication performs no fallible allocation.

The follow-up persona review closed four concurrency and transaction defects.
Syncobj waits propagate allocation failure, retain each fence generation once
observed, and keep a direct completion callback even if the container is
reset. Eventfd notification now snapshots readiness and its watcher-generation
cutoff atomically with each state transition, so a rapid signal/reset pair
cannot lose the signal. Execbuffer consumes RESET inputs before publishing
outputs, preserving a new fence when one syncobj appears in both arrays.

The public RISC-V ioctl client now verifies out-fence fd rollback and fd reuse,
pollable execbuffer out-fences, multiple output syncobjs, and the same-syncobj
RESET-input/timeline-output case. A targeted QEMU ktest reported
`syncobj_regression ... ok` and `1 passed; 0 failed`. The final ordinary Sv39
gate in `/tmp/drm-typed-transaction-final-gate.log` reported the exact marker
`M19_SYNCOBJ_PASS binary timeline transfer share sync_file eventfd concurrency
execbuffer out_fence rollback multi_output alias_reset lifetime stress bounds`,
all legacy and atomic modetest/kmscube, PRIME, raw virgl, and EGL return codes
as zero, four distinct Mesa virgl frames, and `MINI_VIRGL_PASS`.

The next architecture slice should decompose `GpuManager`, the root ioctl
dispatcher, and `virtgpu_resource_create`; these are maintainability debts
recorded by the review, not confirmed runtime failures in this transaction.

## 2026-08-28 transactional resource creation and backing validation

`DRM_IOCTL_VIRTGPU_RESOURCE_CREATE` now lives in
`device/drm/virtio_gpu/resource_create.rs` and is expressed as validated,
prepared, host-created, context-attached, copied-out, and published phases.
The transaction owns its GEM pin, pending handle, backing allocation, context
membership, host resource, and creation lock. Returning before publication
unwinds these owners in a defined order; uncertain host unref is retained as a
cleanup-only resource instead of prematurely freeing guest backing.

Known tightly packed formats calculate a checked minimum backing size over the
complete mip chain, array layers, cube faces, or 3D depth. Explicitly undersized
new or existing GEM backing is rejected. Direct transfers similarly require a
provable linear layout and reject unsupported or multisample layouts rather
than sending an unbounded DMA range to the device. Automatic allocation grows
to the calculated minimum when the wire `size` is zero. The raw M16 client now
declares `last_level = 0` for its level-zero-only backing.

The same review added capset-version validation and changed `VIRTGPU_WAIT` to
an interruptible 15-second device deadline. Completed and failed fences are
removed from resource associations before their result is returned, preventing
permanent failed-fence retention. Pool capacities and allocation sizes now
carry explicit byte units, repeated GEM-handle resolution uses one helper, and
the Linux compatibility notes record Asterinas's existing-handle extension to
resource creation.

Targeted RISC-V ktests passed for backing-size calculation, unsupported layout
rejection, zero-deadline pending-fence waiting, and `syncobj_regression`. The
M22 fault/lifetime gate reported `55` passes, `0` failures, and `32/32` rounds,
including exact rollback after a read-only response copyout. The final Sv39
Mesa gate in `/tmp/drm-resource-final-gate-2.log` reported successful legacy
and atomic kmscube, PRIME, syncobj, raw virgl, four explicit-sync frames, four
distinct EGL frames, and `MINI_VIRGL_PASS`.

An unfiltered ktest attempt is recorded separately from these green targeted
gates: three existing vblank-queue tests failed and the run later stalled at a
wait-for-submit test. This transaction does not claim that unrelated full-suite
state as resolved.

## 2026-08-28 Debian and Megrez main-line integration

The DRM branch merged the public GitHub `main` tip
`bb55702cff8826514b4e44609d0d3e9e7be57634`, absorbing 157 commits since the
previous merge base. The imported work includes the Debian desktop M5 network,
M6 browser-evidence, and M7 Baidu-page contracts; the Megrez Debian installer,
pre-board, debug, and GMAC workflows; the new DWMAC component and bounded RX/TX
models; and the supporting Unix-socket credentials, user-namespace, seccomp,
and process-identity changes. Main did not modify the DRM implementation
directly.

Six textual conflicts occurred in `user_ns.rs`, `seccomp.rs`, and the
`setfsuid`, `setfsgid`, `setuid`, and `setgid` syscalls. They were resolved to
the later main-line implementations, which contain the complete privilege,
unmapped-ID, immutable filter-chain, and atomic TSYNC semantics rather than the
older branch variants. The merged RISC-V Sv39 kernel compiled successfully,
including the new `aster-dwmac` component.

The imported contract suites passed: 191 Debian rootfs/systemd/desktop/network/
browser tests, 33 Megrez GMAC tests, 11 DWMAC liveness-model tests, 68 Megrez
debug/pre-board tests, and 52 Megrez installer/board-session tests. These are
host-side and QEMU contract gates; this integration checkpoint does not claim a
new physical-Megrez board run or a complete interactive Debian desktop boot.

The post-merge DRM M22 QEMU gate reported 55 passes, 0 failures, and 32/32
resource-reclamation rounds. The final Mesa gate in
`/tmp/drm-main-debian-integration-gate.log` reported zero return codes for
legacy and atomic kmscube, PRIME, syncobj, raw virgl, and EGL, including four
explicit-sync frames, and ended with `MINI_VIRGL_PASS`. Thus the imported
Debian/network/board work does not regress the exercised
Mesa-to-DRM-to-virtio-gpu-to-virglrenderer path.
