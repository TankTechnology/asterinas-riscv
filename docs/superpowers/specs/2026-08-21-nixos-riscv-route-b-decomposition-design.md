# Asterinas RISC-V Full NixOS Decomposition Design

## Purpose

Issue #62 defines the end state:
a reproducible, persistent, graphical NixOS system on Asterinas RISC-V.
This design decomposes that epic into independently testable modules
and defines how useful work from `track/nixos` is admitted to `main`
without merging the divergent branch wholesale.

The first acceptance platform is QEMU `virt` with four guest harts.
Milk-V Megrez, an installer ISO, Nix sandbox hardening,
and accelerated 3D graphics remain follow-up work
after the software-rendered graphical system passes.

## Current baseline

At `main` commit `b54aad2f89ce529691dd9944dac53bf33c8dcb93`:

- systemd 257.5, D-Bus, Xorg, virtio-gpu, virtio keyboard/tablet, PCManFM,
  xterm, and NetSurf have each run on Asterinas RISC-V;
- a local Nix cross-build produced systemd-minimal, Xorg, fbdev, and evdev
  RISC-V outputs;
- the visible browser proof used Alpine GTK3 userspace and therefore proves the
  kernel/device/UI path, not a complete NixOS system;
- the repository's synthetic Nix-style desktop depends on ignored build output,
  a sibling checkout, and placeholder store paths; and
- `tools/nixos/run.sh` rejects `TARGET_ARCH=riscv64` even though the build-side
  platform mapper recognizes `riscv64-linux`.

`track/nixos` is 109 commits ahead of and 159 commits behind `main` from their
merge base. A patch-id comparison of its 108 non-merge commits reports 20
patch-equivalent commits already in `main` and 88 nominally unique commits. The
nominally unique set also
contains changes already merged through rewritten PRs, open topic PRs, obsolete
test harnesses, and genuinely reusable work. The branch is a candidate patch
source, not a merge target.

## Architecture and module boundaries

The work is divided into eight child issues under #62.
Each child must deliver a working artifact or an auditable decision
and may be reviewed independently.

### Module 1: R0 track reconciliation and admission matrix

Produce a machine-readable and human-readable inventory of every
`track/nixos`-only commit. Classify each item as:

1. **already-main** — patch-equivalent or merged through a rewritten PR;
2. **existing-pr** — represented by an open topic PR and not duplicated;
3. **portable** — isolated tooling, test, documentation, or kernel change that
   can be rebased and locally verified;
4. **rewrite** — useful behavior whose old patch conflicts with current
   systemd, desktop, NixOS, or LTP structure; or
5. **retire** — stale report, superseded test harness, or abandoned workaround.

The matrix records source commit, changed subsystem, current-main equivalent or
PR, destination issue, verification command, and disposition reason. No kernel
commit enters `main` merely because it was exercised on `track/nixos`.

The first portable batch is the Nix M2-M9 user-space evidence and reproducible
smoke tooling. Reports are updated when their claims are stale. The current
LTP gate in `main` remains authoritative; older Nix-track LTP orchestration is
retired unless it contains an uncovered test case.

### Module 2: R1-A real RISC-V NixOS closure

Build a real `riscv64-linux` `config.system.build.toplevel` using a pinned
nixpkgs revision. The output uses genuine Nix store hashes and a real system
profile. It does not copy Alpine APKs, placeholder store directories, ignored
`target/riscv-cross` files, or sibling-worktree artifacts.

The module exposes one build command and a preflight mode. Preflight reports all
missing host capabilities and inputs before starting the expensive build. A
successful build emits a manifest containing nixpkgs revision, output path,
closure size, and recursive closure references.

### Module 3: R1-B persistent disk, stage 1, and QEMU runner

Convert the system closure into an ext2 root disk and boot it through the
existing U-Boot `booti` chain. Stage 1 mounts the real root, moves `/dev`, and
uses the NixOS-selected `switch_root` path to enter the generated stage 2.

The runner adds an architecture-specific RISC-V path instead of passing RISC-V
arguments through the x86-oriented `tools/qemu_args.sh`. It uses virtio-mmio,
an SMP-matched DTB, `-smp 4`, and snapshot overlays for smoke tests so the base
disk stays clean. Build, disk assembly, and boot orchestration remain separate
commands with explicit artifact contracts.

### Module 4: R2 real stage 2 and service foundation

Run NixOS-generated activation and systemd as PID 1. Reach
`multi-user.target` with D-Bus, getty/login, journald or equivalent persistent
log capture, and deterministic device discovery.

This module selects one documented device model: either mountable devtmpfs with
udev/coldplug, or udev disabled with equivalent deterministic kernel-provided
device nodes. Existing hand-written milestone units may serve as diagnostics,
but successful acceptance uses units from the NixOS closure.

Old Route B kernel gaps are re-tested on current `main`. `pivot_root` is not
treated as a blocker when verified `switch_root` semantics suffice. Remaining
mount propagation, mountinfo, cgroup, keyring, fanotify, seccomp, or shutdown
gaps receive focused reproducers and separate kernel changes.

### Module 5: R3 Nix daemon, persistence, and network

Run `nix-daemon` from the integrated closure, build a real derivation in
multi-user mode, and preserve the selected system generation across a clean
reboot. Initial acceptance explicitly permits `sandbox = false`.

Bring up virtio-net with deterministic addressing, DNS, CA certificates, and
certificate-verified HTTPS. The test distinguishes transport, DNS, trust-store,
and HTTP failures rather than reporting a single generic network failure.

### Module 6: R4 graphical NixOS closure

Provide Xorg, modesetting/fbdev fallback, evdev, XKB/fonts, GTK runtime, a
terminal, file manager, and browser from the Nix closure. Reach
`graphical.target` through NixOS-generated services. Verify a local browser
page and an HTTPS page, plus actual keyboard, pointer, click, focus, and typed
input behavior.

System V SHM permission semantics are fixed before enabling MIT-SHM. Until that
kernel fix lands, the closure disables the extension explicitly and links #18;
an implicit crash/restart fallback is not accepted.

### Module 7: R5 SMP, graphical, and evidence hardening

The normal gate performs one `-smp 4` boot. A separate repeated-boot test
measures the multi-thread TCG cold-boot/timer flake so a functional test is not
confused with a concurrency reliability test.

The result bundle records kernel, nixpkgs, closure, disk, DTB, serial, and
screenshot hashes. Gates fail on missing inputs, panic/fatal services, browser
restart, blank or uniform framebuffer, missing interaction markers, or dirty
base filesystem images.

### Module 8: loop device subsystem admission

The roughly 600-line loop subsystem on `track/nixos` is reviewed and merged as
an independent kernel feature. It receives focused unit/QEMU tests for
`/dev/loop-control`, allocation/removal, backing-file attach/detach, block I/O,
zero-sized image handling, ioctl dispatch, cleanup, and LTP-relevant behavior.

NixOS Route B may consume loop devices after this module passes, but the first
persistent QEMU disk path does not depend on loop support. This prevents a
large kernel subsystem from blocking closure and boot work.

## Track admission strategy

Whole-branch merge and bulk cherry-pick are prohibited. Admission proceeds in
small batches:

1. compute patch-id and PR equivalence against current `origin/main`;
2. inspect the source commit and its dependencies;
3. reproduce the behavior or failure on current `main`;
4. add or port a focused test before the implementation change;
5. rebase or rewrite the smallest useful patch;
6. run subsystem tests plus the relevant RISC-V QEMU gate;
7. review and commit the batch independently; and
8. update the admission matrix and child Issue with evidence.

The preliminary groups are:

- **Do not merge again:** patch-equivalent commits and work delivered through
  merged PRs #28, #29, #31-#41, #56, and the current LTP integration.
- **Do not duplicate:** open PRs #43-#47, #49-#53, and #55; review or refresh
  those branches.
- **Port first:** Nix M2-M9 build/smoke assets and current, still-valid reports.
- **Rewrite on current main:** systemd getty/journald/socket work on top of the
  D-Bus desktop and any old LTP integration hooks.
- **Independent kernel batches:** cgroup `memory.max`, socket compatibility,
  pwrite flags, capability/error semantics, network fixes, and loop devices.
- **Retire:** stale failure counts, superseded LTP drivers, and workaround-only
  reports whose assumptions have already changed.

## Issue publication and dependency flow

Create eight child issues using the module titles above. Each issue contains:

- scope and explicit non-goals;
- inputs and outputs;
- dependencies on #62, existing Issues, and open PRs;
- ordered implementation checklist;
- local verification commands and expected markers; and
- a definition of done that produces a reviewable artifact.

After creation, update #62 with a child-issue checklist in module order. The R0
issue owns the admission matrix and links every portable/rewrite kernel batch to
either an existing PR or the child issue that consumes it. Closing a child
issue requires evidence in its body or a linked PR; an uncited branch report is
not sufficient.

The dependency flow is:

```text
R0 reconciliation ─┬─> R1-A closure ─┐
                   ├─> loop admission │
                   └─> focused fixes  ├─> R1-B disk/runner -> R2 stage 2
                                      │                         |
                                      └─────────────────────────┘
                                                                v
                                                   R3 Nix/network/persistence
                                                                |
                                                                v
                                                    R4 graphical NixOS
                                                                |
                                                                v
                                                     R5 evidence hardening
```

R0 and loop admission may proceed independently. R1-B can prototype its disk
contract with a minimal closure while R1-A completes, but R2 acceptance requires
the real system closure.

## Error handling and observability

Every build and boot tool uses nonzero exit status for failure and distinguishes
missing prerequisites from build failure, guest timeout, kernel panic, service
failure, and acceptance failure. Serial logs are streamed while QEMU runs.
Long-running Nix builds expose derivation progress and preserve the failing log.
QEMU runners use bounded phase timeouts and inspect serial/process state instead
of sleeping blindly.

Boot evidence uses explicit markers for stage 1, stage 2, PID 1, D-Bus,
network, Nix daemon, graphical target, and browser interaction. A process being
present is not evidence that its window rendered. A screenshot being nonblank
is not evidence that the browser accepted input; both pixel and interaction
checks are required.

## Testing strategy

Each implementation batch follows test-first development:

- shell/Python contract tests for preflight, artifact selection, command-line
  construction, missing inputs, and timeout classification;
- `nix-instantiate`/evaluation checks before full closure builds;
- filesystem checks before and after disk assembly, with QEMU snapshot writes;
- RISC-V QEMU serial gates at `-smp 4` for stage transitions and services;
- focused kernel unit/QEMU reproducers for admitted kernel changes;
- browser framebuffer pixel validation and automated input probes; and
- a separate repeated multi-thread TCG boot test for SMP reliability.

Remote CI monitoring is not part of the workflow. Relevant checks run locally
before a branch or PR is handed off.

## Completion criteria for this design

This decomposition is complete when the eight child issues exist, #62 links to
them in dependency order, the R0 issue contains the initial five-way admission
inventory, and the first implementation plan covers R0 plus the RISC-V runner
preflight slice without attempting a whole-branch merge.
