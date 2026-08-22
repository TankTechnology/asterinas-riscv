# RISC-V USB Stack Consolidation Design

## Objective

Turn the mixed `codex/megrez-usb-keyboard` history into a small set of
reviewable USB layers that can be replayed onto the current downstream main.
Preserve the validated QEMU and Megrez work,
but do not publish experimental IRQ lifetime changes,
generated logs,
stale lockfile changes,
or unrelated desktop and LTP work as part of the USB stack.

The integration target is the `TankTechnology/asterinas-riscv` repository.
The official `asterinas/asterinas` repository is an input for later upstream
synchronization,
not a publication target for this work.

## Repository and Branch Policy

The source worktree remains frozen on `codex/megrez-usb-keyboard`.
Its modified `Cargo.lock`,
modified RISC-V USB files,
untracked `.local-workspace`,
and test logs are not reset,
deleted,
staged,
or moved.

Consolidation is performed in the global worktree
`/home/ubuntu/.config/superpowers/worktrees/asterinas/usb-stack-cleanup`
on `codex/usb-stack-cleanup`,
starting from `origin/main` at `dab7dacff`.
The branch may be pushed only to `origin`,
whose URL is `https://github.com/TankTechnology/asterinas-riscv.git`.
No pull request or push to `upstream` is part of this milestone.

## Source Classification

The source branch contains four kinds of material:

1. USB implementation commits for HID decoding,
   xHCI event handling,
   RISC-V PCI,
   DWC3 selection,
   and input delivery.
2. Board tooling and evidence for Megrez handoff and fail-safe behavior.
3. Unrelated NixOS,
   XFCE,
   browser,
   Wayland,
   and LTP work introduced by historical main merges.
4. Uncommitted IRQ-lifetime experiments,
   a stale regenerated lockfile,
   and local build logs.

Only the first two classes are USB consolidation inputs.
The third class remains represented by `origin/main` or its own topic history.
The fourth class is quarantined until independently reviewed and tested.

## Layered Patch Structure

### Layer 1: Generic USB report transport

This layer contains the bounded USB report queue and its unit tests under
`ostd/src/bus/usb/`.
It must not depend on PCI,
RISC-V device-tree parsing,
TTY internals,
or Megrez-specific addresses.

The queue contract covers ordered delivery,
empty and full states,
wraparound,
and overflow behavior.

### Layer 2: HID boot-keyboard decoding

This layer contains the architecture-independent keyboard decoder,
Linux-derived key vectors,
the host-side oracle,
and oracle tests.
It converts eight-byte HID boot reports into Asterinas input events.

Generated vectors remain checked in only when the oracle proves that they are
reproducible.
The large vector file and Python tests stay in this layer so that reviewers can
distinguish executable kernel code from compatibility data.

### Layer 3: Generic xHCI keyboard transport

This layer contains xHCI capability discovery,
controller setup,
event-ring handling,
interrupt enable and disable operations,
and HID report transfer.
It exposes a narrow host-resource interface consisting of MMIO,
DMA window,
and interrupt source.

The layer must not choose a Megrez DWC3 node or allocate a RISC-V PCI BAR.
Those policies belong to Layer 4.

### Layer 4: RISC-V host discovery

This layer contains RISC-V PCI xHCI discovery,
BAR allocation,
device-tree interrupt-map matching,
and opt-in DWC3 host selection through `/chosen/asterinas,usb-host`.

PCI xHCI remains the QEMU verification path.
DWC3 remains the Megrez physical-board path.
An invalid DWC3 selector must log a warning and leave the system operational.

### Layer 5: IRQ lifetime and recovery

The uncommitted conversion from process-global `Once` values to owned
`SpinLock<Option<_>>` resources,
masked PLIC mapping,
explicit rearm,
and an IRQ enable guard is treated as a separate correctness change.

This layer is accepted only after proving:

- controller interrupts are disabled before a mapping is dropped;
- the PLIC source is masked while reports are drained;
- every successful wakeup path rearms the source;
- startup and polling failures release owned resources without a second host
  observing partially initialized state;
- a second xHCI controller is rejected deterministically;
- no interrupt callback performs device registration or TTY work directly.

The existing dirty implementation is evidence and a starting point,
not an automatically accepted patch.

### Layer 6: Input and console integration

This layer registers the decoded keyboard as an input device and delivers
events through the existing evdev and VT keyboard contracts.
Any serial-console injection required for the historical Megrez demo is kept
separate from generic input registration.

Debug echo behavior,
QEMU-only init changes,
and workarounds that bypass the line discipline are not part of the generic USB
stack unless a regression test demonstrates that they are still necessary.

### Layer 7: Megrez tooling and evidence

Board-session scripts,
DTB patching,
handoff checklists,
simulation contracts,
and historical evidence form a documentation and tooling layer.
They must state that QEMU verifies the boot and software contracts,
but cannot verify physical DWC3 clocks,
reset,
PHY,
or board wiring.

## Dependency and Lockfile Policy

Manifests are reconstructed from the minimal dependencies required by each
accepted layer.
`Cargo.lock` is regenerated from those manifests on the current
`origin/main` baseline.

The dirty source lockfile is not copied because it removes `aster-pci` and
`aster-softirq` from `aster-usb` while downgrading unrelated crates,
including `dma-api`.
No dependency version is downgraded merely to reproduce an old branch state.

## Test-Evidence Reuse Policy

Previously completed tests are not repeated when all of their relevant inputs
remain byte-for-byte unchanged and their environment contract still applies.
The consolidation record cites the existing command,
commit,
result,
and evidence file instead.

The following historical results are reusable as source-branch evidence:

- three successful RISC-V kernel builds at `767e27e64`;
- Megrez Sv48/Svade four-hart simulation with a userspace marker;
- the complete QEMU USB keyboard key matrix,
  rapid-input case,
  single registration,
  and zero panic;
- invalid DWC3 selector fail-safe behavior;
- OSTD ktests at 239/239;
- UART ktests at 80/80;
- the later recovery baseline and its documented known TTY test mismatch.

Evidence is invalidated only for the layer whose relevant code changes.
For example,
editing IRQ ownership invalidates the interrupt-driven runtime gate,
but does not invalidate unchanged HID decoder vectors.

New testing is limited to:

1. focused unit or host tests for a changed layer;
2. compilation of the dependency closure affected by that layer;
3. RISC-V QEMU SMP=4 runtime coverage for changed kernel behavior;
4. cross-layer acceptance after the curated layers are combined;
5. tests required because the later official-main layout migration changes
   file paths,
   interfaces,
   or dependencies.

Normal kernel builds and `cargo osdk test` run serially because kernel tests
replace the normal QEMU ELF artifact.
Physical Megrez USB behavior remains unverified until board access returns.

## Consolidation Workflow

For each layer:

1. identify the smallest source commit set and its dependencies;
2. compare the source implementation with current `origin/main` equivalents;
3. write or retain the narrow regression test before adapting behavior;
4. transplant only the required files and hunks;
5. regenerate metadata rather than copying stale generated state;
6. run only the invalidated tests;
7. record provenance,
   reused evidence,
   new evidence,
   and remaining physical-hardware limits;
8. commit the layer independently.

Unrelated source commits are not cherry-picked merely to preserve their hashes.
The frozen source branch and archived refs retain historical provenance.

## Publication and Later Upstream Integration

The curated commits are first reviewed locally as
`codex/usb-stack-cleanup`.
After the stack is coherent and tested,
it may be pushed to
`TankTechnology/asterinas-riscv` as a review branch.
It is not merged into `origin/main` until the resulting commit list,
diff,
and evidence ledger are reviewed.

The later full `upstream/main` integration must replay these curated layers
onto the official `kernel/core` layout.
It must not merge the mixed source branch or copy its dirty worktree state.

## Success Criteria

The milestone is complete when:

- every accepted USB behavior belongs to one documented layer;
- each layer has a focused commit and an explicit dependency boundary;
- unrelated NixOS,
  desktop,
  Wayland,
  browser,
  and LTP history is absent from the USB diff;
- generated logs,
  `.local-workspace`,
  build outputs,
  and the stale dirty lockfile are absent;
- reused and newly run tests are distinguishable in the evidence ledger;
- changed kernel behavior passes its focused tests and RISC-V QEMU SMP=4
  acceptance gate;
- the original dirty worktree remains recoverable and untouched;
- any published branch goes only to
  `TankTechnology/asterinas-riscv`.

## Non-goals

- Publishing to the official `asterinas/asterinas` repository.
- Claiming physical Megrez DWC3 verification without board access.
- Re-running unchanged historical tests solely to reproduce old logs.
- Folding NixOS,
  DRM,
  browser,
  Wayland,
  or LTP milestones into the USB patch stack.
- Force-pushing or rewriting the existing USB source branch.
