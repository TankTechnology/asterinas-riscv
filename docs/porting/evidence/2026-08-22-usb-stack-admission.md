# Megrez USB/xHCI Stack Admission Ledger

## Purpose

This ledger classifies the USB-related work retained on
`codex/megrez-usb-keyboard` before the downstream repository absorbs the
official Asterinas main history.
It prevents the mixed topic branch from being merged wholesale,
while preserving useful implementation and board-validation provenance.

The publication repository is
`TankTechnology/asterinas-riscv`.
The official `asterinas/asterinas` repository is not a publication target for
this ledger.

## Frozen Sources

| Item | Value |
|---|---|
| Current downstream baseline | `origin/main@dab7dacff` |
| Local topic source | `codex/megrez-usb-keyboard@ecdea5a39` |
| Remote topic provenance | `origin/codex/megrez-usb-keyboard@243edb99b` |
| Consolidation branch | `codex/usb-stack-cleanup` |
| Open tracking issue | `TankTechnology/asterinas-riscv#75` |

The source worktree remains dirty by design.
Its modified files,
`.local-workspace`,
and generated logs were not reset,
deleted,
staged,
or copied.

## Dispositions

| Value | Meaning |
|---|---|
| `MAIN` | Behavior is already accepted on `origin/main`. |
| `SUPERSEDED` | A safer current-main implementation replaces the source behavior. |
| `ADAPT` | Useful behavior is absent from main and eligible for a focused rewrite. |
| `DEFER` | Useful behavior is blocked by an unmet hardware or interface contract. |
| `RETIRE` | Debugging, duplicate, generated, stale, or unrelated material is excluded. |

## Commit Admission Matrix

| Source | Scope | Current-main equivalent or blocker | Evidence | Disposition |
|---|---|---|---|---|
| `bc625863b` | USB component, HID decoder, report queue, DMA adapter, DWC3 selection, oracle | Rebuilt as `eb8ef32cd` and hardened by later main commits | Historical QEMU key matrix; OSTD 239/239; 49 host oracle tests during main integration | `MAIN` |
| `273407415` | Interrupt-driven xHCI event ring | Rebuilt as `71d16eaef`, then replaced by task-context and level-safe ownership series | Focused RISC-V SMP=4 ownership regression | `SUPERSEDED` |
| `1e176746a` | `aster-softirq` lockfile edge | Taskless softirq dependency removed by `9fe300e64` | Current component manifest and lockfile | `RETIRE` |
| `47364d032` | RISC-V PCI BAR allocation | Rebuilt as `288848e7e` with fail-closed assignment preflight | Four focused PCI kernel tests under RISC-V QEMU SMP=4 | `MAIN` |
| `32979fab9` | PCI qemu-xhci discovery | Assumes identity DMA and legacy INTx | No admitted runtime gate on current main | `DEFER` |
| `ba139ca91` | PCI interrupt-map matching | Still feeds an exclusive mapping from a potentially shared INTx pin | No shared-INTx or validated MSI/MSI-X contract | `DEFER` |
| `61f638693` | IRQ enable, persistent decoder, device registration | Replaced by `342ae0454`, `9fe300e64`, `ec5c19766`, and `0955fc77d` | Ownership review: Critical 0, Important 0 | `SUPERSEDED` |
| `37adeb80e` | Direct serial-console TTY injection | Current USB device submits normal input events to the generic VT input handler | Current input and VT handler contracts | `RETIRE` |
| `220d770aa` | QEMU init keyboard echo | Test-harness behavior, not a USB kernel contract | Historical QEMU key matrix | `RETIRE` |
| `c6057145b` | Pin `dma-api` 0.9.5 | Current reviewed main deliberately resolves the compatible 0.9.3 API | Current `Cargo.lock` and integration record | `RETIRE` |
| `667a89067` | Megrez handoff checklist | Useful physical-board workflow, but refers to local-only evidence assets | Requires a self-contained rewrite before publication | `ADAPT` |
| `393bd55d4` | Up-plate readiness and DTB patch requirement | Historical boundary remains valid; artifact identities are stale | 2026-08-10 readiness record on source branch | `ADAPT` |
| `767e27e64` | DTB patcher, simulation wrapper, board commands | Tools are absent from main; patcher scans only immediate `/soc` children and board commands contain historical assumptions | Historical simulation PASS; no current-main tool audit | `ADAPT` |
| `e282453d9` | Pre-upload test report | Historical source evidence, not current-main acceptance | Three builds, QEMU key matrix, DWC3 fail-safe, OSTD/UART tests | `MAIN` as evidence only |
| `861a4fc6d` | Recovery-baseline addendum | Mixes USB evidence with downstream reboot policy | Historical recovery record | `RETIRE` from USB stack |
| `3d45f6e73` | Automated board-session driver and tests | Useful structure, but hardcodes boot addresses, DT path, `mmc 1`, and milestone policy | Three source unit tests | `ADAPT` |
| `f6ba5c3c3` | Echo outside line-discipline lock | Current main owns a newer TTY implementation; USB uses generic input delivery | Current VT handler path | `RETIRE` |
| `803786f99` | Formatting normalization | No standalone behavior | Current formatting baseline | `RETIRE` |
| `65e452bad` | Cross-architecture lint fixes | Relevant portability behavior is already represented in current main | Current main build history | `MAIN` |
| `243edb99b` | QEMU display-chain report | Graphics evidence, not USB stack content | Separate DRM/desktop workstream | `RETIRE` from USB stack |

## Already-Admitted Layers

### Report queue and HID decoder

The following files have identical Git blobs on the source topic and current
`origin/main`:

- `kernel/comps/usb/src/arch/other.rs`;
- `kernel/comps/usb/src/arch/riscv/capability.rs`;
- `kernel/comps/usb/src/keyboard.rs`;
- `kernel/comps/usb/src/keyboard_linux_vectors.rs`;
- `ostd/src/bus/usb/report_queue.rs`;
- `tools/usb-hid/README.md`;
- `tools/usb-hid/boot_keyboard_oracle.py`;
- `tools/usb-hid/requirements.txt`;
- `tools/usb-hid/tests/test_boot_keyboard_oracle.py`.

Their tests were not repeated during this documentation-only consolidation.
The byte-identical inputs preserve the recorded 49-test host result and the
historical key-matrix evidence.

The admission audit compared the following blob identities directly:

| Path group | Shared blob |
|---|---|
| Architecture fallback | `cbafdb2a20c9c17969f68ae3f85c47ce0b00c4f3` |
| xHCI capability probe | `6cfa1329035ec5fe78a5a91fcd416f9dc9dfc1e6` |
| HID decoder | `1380a929b176f9b98cd83961e3227e994c55593f` |
| Linux keyboard vectors | `447736675b932461067d95cc3a1c4e4dfbb64f45` |
| Bounded report queue | `8a01b917ebbe97c3ffe5706e7c705ad5edb7ef62` |
| Oracle README | `ff75b216faf975e5a043fdf9684629902037e807` |
| Oracle generator | `2e716a895311ea53afe326496314b3cbe277892d` |
| Oracle requirements | `4f3fbea5141da4c95f1bb425ea26314fdd3a5481` |
| Oracle tests | `a34ed82501a06f151f6844c78a5ea44dc42b30bf` |

### xHCI ownership and IRQ deferral

Current main contains the reviewed follow-up series:

- `342ae0454` enforces MMIO and DMA mapping ownership;
- `9fe300e64` moves USB IRQ work into task context;
- `ec5c19766` sequences controller and interrupt ownership;
- `0955fc77d` adds level-safe RISC-V PLIC masking,
  acknowledgement,
  rearm,
  and teardown.

This series supersedes both the original interrupt commit and the uncommitted
source-worktree experiment.
The documented RISC-V SMP=4 ownership regression and completed code review are
reused because none of these files changed in the consolidation branch.

The audit also recorded distinct source identities so that the dirty
experiment cannot be mistaken for admitted main code:

| RISC-V USB worker | Blob |
|---|---|
| Current `origin/main` | `8d61d00dd04ab5307a6db1fe05450a2f3e688d15` |
| Committed topic source | `d3b9f9e0bc7b7f815306c2f7949a704f54b5528b` |
| Dirty source worktree | `a1d29a8a4793b90a8c223df023381829c9ea1e84` |

Only the first blob is admission authority.

## Deferred PCI xHCI Contract

The old PCI driver is not admitted.
It creates `DmaWindow::new(0, 0, usize::MAX)` without deriving the device view
from the PCI host's `dma-ranges` or rejecting an IOMMU-backed topology.
It also maps the legacy interrupt pin as an exclusive deferred PLIC source even
though PCI INTx may be shared.

PCI qemu-xhci admission requires both:

1. a PCI-host-derived DMA contract that accepts only a validated translatable
   window and fails closed for unsupported `dma-ranges` or IOMMU layouts;
2. either demonstrated RISC-V MSI/MSI-X delivery for qemu-xhci or a shared INTx
   dispatcher with per-device interrupt-status probing.

The current CrabUSB/xHCI adapter deliberately rejects MSI capabilities because
the dependency's MSI accessor does not safely model every legal capability
layout.
Generic Asterinas PCI MSI-X support therefore does not, by itself, satisfy the
USB runtime contract.

The committed PCI driver blob is
`19f2d0454c29cba2b35139cd184a01dda93f6f94`.
The dirty follow-up blob is
`43786f621d84124bdeb0136f81f9a64987917c14`.
Neither exists on `origin/main`,
and neither is admission-ready.
They remain recoverable from the frozen topic worktree.

## Input and Generated-State Cleanup

The admitted USB worker registers a standard input device and submits decoded
events.
The existing VT input-handler class connects keyboard-like devices and performs
the TTY delivery.
Direct serial-console injection from the old source branch is unnecessary and
would couple USB to a specific console backend.

The following local state is excluded and remains untouched in the frozen
worktree:

- dirty `Cargo.lock` dependency downgrades and removed component edges;
- `.local-workspace`;
- `kernel/comps/uart/target-uart-final2.log`;
- `kernel/comps/uart/target-uart-ktest-final.log`;
- `kernel/comps/uart/target-uart-ktest.log`;
- `kernel/target-echo-ktest.log`;
- `kernel/target-kernel-ktest.log`.

## Megrez Hardware Boundary

Current main admits the explicitly selected DWC3 path,
capability probing,
non-coherent DMA validation,
host-role selection,
failure isolation,
and interrupt-driven report processing.

QEMU evidence verifies the software boot contract,
the historical keyboard path,
and invalid-selector failure safety.
It does not verify physical EIC7700 DWC3 clocks,
reset,
PHY,
board routing,
or a real keyboard.
Those claims remain deferred until hardware access returns.

The old board tools remain provenance rather than admitted code.
They should be rewritten as a separate board-tooling milestone with recursive
DWC3 discovery,
parameterized storage and addresses,
artifact identity gates,
and mock-session tests before publication.

## Test-Evidence Ledger

| Evidence | Scope | Reuse decision |
|---|---|---|
| Source report `e282453d9` / `861a4fc6d` | Three RISC-V builds, Sv48/Svade simulation, complete key matrix, invalid selector, OSTD 239/239, UART 80/80 | Historical source evidence; not rerun |
| USB main-integration plan | 49 host oracle tests | Reused because all oracle and vector blobs are identical |
| USB main-integration plan | RISC-V OSTD/kernel ktest compile | Reused because consolidation changes no Rust source |
| USB main-integration plan | SMP=4 IRQ ownership regression | Reused because IRQ source is unchanged |
| USB main-integration plan | Four SMP=4 PCI BAR tests | Reused because PCI source is unchanged |
| USB main-integration review | Critical 0, Important 0 | Reused for the accepted current-main implementation |

No Cargo,
KTest,
or QEMU command is required for this documentation-only branch.
Any later `ADAPT` implementation invalidates only its affected evidence and
must run focused tests plus a RISC-V SMP=4 cross-layer gate.

## Publication Boundary

- `origin/codex/usb-stack-cleanup` may publish this disposition record.
- `origin/main` remains unchanged by this milestone.
- `origin/codex/megrez-usb-keyboard` remains provenance and is not force-pushed.
- `upstream/*` remains unchanged.
- Issue #75 remains open for PCI xHCI runtime and physical-board validation.
