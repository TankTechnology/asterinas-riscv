# RISC-V USB Stack Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Classify every useful USB/xHCI change from the mixed Megrez topic branch,
retain only current-main-compatible work,
reuse valid test evidence,
and publish a clean review branch only to `TankTechnology/asterinas-riscv`.

**Architecture:** Treat current `origin/main` as the accepted implementation baseline.
Build a disposition ledger before changing kernel code.
Keep the integrated USB/HID/DMA/IRQ stack unchanged,
retire source-side TTY and IRQ variants that current main supersedes,
and defer PCI xHCI runtime admission until its DMA and interrupt contracts are validated.

**Tech Stack:** Git worktrees,
Rust nightly,
Asterinas OSTD,
CrabUSB/xHCI,
RISC-V PCI/PLIC,
QEMU RISC-V SMP=4,
and GitHub CLI.

---

## Repositories and Refs

- Publication repository: `https://github.com/TankTechnology/asterinas-riscv.git`
- Frozen source worktree: `/home/ubuntu/xaj/Program/asterinas`
- Frozen source branch: `codex/megrez-usb-keyboard` at `ecdea5a39`
- Remote provenance branch: `origin/codex/megrez-usb-keyboard` at `243edb99b`
- Consolidation worktree: `/home/ubuntu/.config/superpowers/worktrees/asterinas/usb-stack-cleanup`
- Consolidation branch: `codex/usb-stack-cleanup`
- Consolidation base: `origin/main` at `dab7dacff`
- Design commit: `80b030566`

The source worktree's modified files and untracked artifacts remain untouched.

### Task 1: Establish the disposition ledger

**Files:**
- Create: `docs/porting/evidence/2026-08-22-usb-stack-admission.md`

- [ ] **Step 1: Verify repository identity and the frozen source**

Run:

```bash
git remote get-url origin
git rev-parse HEAD origin/main codex/megrez-usb-keyboard origin/codex/megrez-usb-keyboard
git -C /home/ubuntu/xaj/Program/asterinas status --short --branch
git status --short --branch
```

Expected: `origin` is `TankTechnology/asterinas-riscv`;
the source still has its three modified files and six untracked paths;
the consolidation worktree has no production-code changes.

- [ ] **Step 2: Create fixed disposition categories**

Write the ledger with these exact categories:

```text
MAIN       behavior already accepted on origin/main
SUPERSEDED source behavior replaced by a safer current-main implementation
ADAPT      useful behavior absent from main and eligible for a focused rewrite
DEFER      useful behavior blocked by an unmet hardware or interface contract
RETIRE     debugging, duplicate, generated, stale, or unrelated material
```

For each USB source commit,
record paths,
current-main equivalent or blocker,
test evidence,
and disposition.

- [ ] **Step 3: Commit the initial ledger**

Run:

```bash
git add docs/porting/evidence/2026-08-22-usb-stack-admission.md
git commit -m "docs(riscv): inventory Megrez USB stack admission"
```

Expected: a documentation-only commit.

### Task 2: Admit unchanged generic USB and HID layers

**Files:**
- Modify: `docs/porting/evidence/2026-08-22-usb-stack-admission.md`
- Verify: `kernel/comps/usb/src/keyboard.rs`
- Verify: `kernel/comps/usb/src/keyboard_linux_vectors.rs`
- Verify: `ostd/src/bus/usb/report_queue.rs`
- Verify: `tools/usb-hid/boot_keyboard_oracle.py`
- Verify: `tools/usb-hid/tests/test_boot_keyboard_oracle.py`

- [ ] **Step 1: Prove unchanged blobs**

Compare these paths between `origin/main` and `codex/megrez-usb-keyboard` with
`git rev-parse <ref>:<path>`:

```text
kernel/comps/usb/src/arch/other.rs
kernel/comps/usb/src/arch/riscv/capability.rs
kernel/comps/usb/src/keyboard.rs
kernel/comps/usb/src/keyboard_linux_vectors.rs
ostd/src/bus/usb/report_queue.rs
tools/usb-hid/README.md
tools/usb-hid/boot_keyboard_oracle.py
tools/usb-hid/requirements.txt
tools/usb-hid/tests/test_boot_keyboard_oracle.py
```

Expected: every pair of blob IDs is equal.

- [ ] **Step 2: Reuse existing evidence**

Mark report queue,
HID decoder,
Linux vectors,
and oracle as `MAIN`.
Reference the 2026-08-10 source evidence and the 49-test integration result in
`docs/superpowers/plans/2026-08-21-megrez-usb-core-main-integration.md`.
Do not rerun tests whose inputs are byte-identical.

- [ ] **Step 3: Commit the generic-layer disposition**

```bash
git add docs/porting/evidence/2026-08-22-usb-stack-admission.md
git commit -m "docs(riscv): admit existing USB and HID layers"
```

### Task 3: Retire superseded IRQ experiments

**Files:**
- Modify: `docs/porting/evidence/2026-08-22-usb-stack-admission.md`
- Verify: `kernel/comps/usb/src/arch/riscv/mod.rs`
- Verify: `ostd/src/bus/usb.rs`
- Verify: `ostd/src/irq/top_half.rs`
- Verify: `ostd/src/arch/riscv/irq/chip/mod.rs`

- [ ] **Step 1: Map the reviewed current-main series**

Run:

```bash
git show --stat --oneline 342ae0454 9fe300e64 ec5c19766 0955fc77d
git diff --no-index <(git show origin/main:kernel/comps/usb/src/arch/riscv/mod.rs) /home/ubuntu/xaj/Program/asterinas/kernel/comps/usb/src/arch/riscv/mod.rs
```

Expected: current main contains mapping ownership,
task-context deferral,
interrupt sequencing,
and level-safe PLIC teardown beyond the dirty experiment.

- [ ] **Step 2: Record supersession and evidence reuse**

Mark `273407415` and the dirty IRQ changes as `SUPERSEDED` by
`342ae0454`,
`9fe300e64`,
`ec5c19766`,
and `0955fc77d`.
Reference the recorded SMP=4 ownership regression and the
Critical 0 / Important 0 review.
Do not rerun IRQ tests because IRQ code is unchanged.

- [ ] **Step 3: Commit the IRQ disposition**

```bash
git add docs/porting/evidence/2026-08-22-usb-stack-admission.md
git commit -m "docs(riscv): retire superseded USB IRQ variants"
```

### Task 4: Isolate the PCI xHCI blocker

**Files:**
- Modify: `docs/porting/evidence/2026-08-22-usb-stack-admission.md`
- Verify source: `kernel/comps/usb/src/arch/riscv/pci.rs`
- Verify: `kernel/comps/pci/src/arch/riscv/mod.rs`
- Verify: `kernel/comps/pci/src/capability/msix.rs`
- Verify: `ostd/src/bus/usb.rs`

- [ ] **Step 1: Confirm source assumptions**

Run:

```bash
git show codex/megrez-usb-keyboard:kernel/comps/usb/src/arch/riscv/pci.rs | rg -n 'DmaWindow::new|interrupt_line|resolve_pci_interrupt'
rg -n 'rejects_all_msi_capabilities|MSI capability' ostd/src/bus/usb.rs
rg -n 'acquire_msix_capability|MSIX_DEFAULT_MSG_ADDR|construct_remappable' kernel/comps/pci/src
```

Expected: the source assumes identity DMA and legacy INTx;
the USB dependency rejects MSI;
generic PCI MSI-X support does not establish a tested qemu-xhci contract.

- [ ] **Step 2: Record the exact blockers**

Mark `32979fab9` and `ba139ca91` as `DEFER` until both exist:

1. a PCI-host-derived DMA window that rejects untranslated or IOMMU layouts;
2. validated RISC-V MSI/MSI-X delivery for qemu-xhci or shared INTx dispatch.

Mark `47364d032` as `MAIN` because the fail-closed BAR allocator and its four
SMP=4 tests are already integrated.

- [ ] **Step 3: Commit the PCI disposition**

```bash
git add docs/porting/evidence/2026-08-22-usb-stack-admission.md
git commit -m "docs(riscv): isolate PCI xHCI admission blockers"
```

### Task 5: Retire direct TTY injection and generated state

**Files:**
- Modify: `docs/porting/evidence/2026-08-22-usb-stack-admission.md`
- Verify: `kernel/src/device/tty/vt/keyboard/handler.rs`
- Verify: `Cargo.lock`

- [ ] **Step 1: Confirm current generic input delivery**

Run:

```bash
rg -n 'register\(|submit_events' kernel/comps/usb/src/arch/riscv/mod.rs kernel/comps/usb/src/keyboard.rs
rg -n 'InputHandlerClass|look_like_keyboard|VT_MANAGER|push_input' kernel/src/device/tty/vt/keyboard/handler.rs
```

Expected: USB submits normal input events and the VT handler consumes
keyboard-like input devices.

- [ ] **Step 2: Record retired content**

Mark `37adeb80e`,
`220d770aa`,
and `f6ba5c3c3` as `RETIRE` for USB admission.
Mark the dirty `Cargo.lock`,
`.local-workspace`,
and generated logs as `RETIRE` without deleting them.

- [ ] **Step 3: Commit the retirement record**

```bash
git add docs/porting/evidence/2026-08-22-usb-stack-admission.md
git commit -m "docs(riscv): retire USB debug and generated state"
```

### Task 6: Close the hardware and testing boundary

**Files:**
- Modify: `docs/porting/evidence/2026-08-22-usb-stack-admission.md`

- [ ] **Step 1: Record Megrez disposition**

Mark DWC3 selector parsing,
capability probing,
non-coherent DMA validation,
and fail-safe startup as `MAIN`.
Mark physical clocks,
reset,
PHY,
and keyboard interaction as `DEFER` until board access returns.

- [ ] **Step 2: Prove documentation-only scope**

Run:

```bash
git diff --name-only origin/main...HEAD
git diff --check origin/main...HEAD
git status --short --branch
```

Expected: only the design,
plan,
and ledger differ from `origin/main`.

- [ ] **Step 3: Commit the final ledger**

```bash
git add docs/porting/evidence/2026-08-22-usb-stack-admission.md
git commit -m "docs(riscv): finalize USB stack admission ledger"
```

### Task 7: Verify and publish only to asterinas-riscv

**Files:**
- Verify: `docs/superpowers/specs/2026-08-22-usb-stack-consolidation-design.md`
- Verify: `docs/superpowers/plans/2026-08-22-usb-stack-consolidation.md`
- Verify: `docs/porting/evidence/2026-08-22-usb-stack-admission.md`

- [ ] **Step 1: Run documentation-scope gates only**

Run:

```bash
git diff --check origin/main...HEAD
rg -n 'T[B]D|T[O]DO|implement[ ]later|fill[ ]in[ ]details' docs/superpowers/specs/2026-08-22-usb-stack-consolidation-design.md docs/superpowers/plans/2026-08-22-usb-stack-consolidation.md docs/porting/evidence/2026-08-22-usb-stack-admission.md
git status --short --branch
```

Expected: diff check passes,
placeholder search has no match,
and the worktree is clean.
Do not rerun Cargo,
KTest,
or QEMU because production code is unchanged.

- [ ] **Step 2: Push the review branch**

Run:

```bash
test "$(git remote get-url origin)" = 'https://github.com/TankTechnology/asterinas-riscv.git'
git push -u origin codex/usb-stack-cleanup
```

Expected: only `origin/codex/usb-stack-cleanup` is created.

- [ ] **Step 3: Update issue 75**

Run:

```bash
gh issue comment 75 --repo TankTechnology/asterinas-riscv --body-file docs/porting/evidence/2026-08-22-usb-stack-admission.md
```

Expected: issue 75 receives the ledger and stays open for PCI xHCI and physical
Megrez work.

### Task 8: Hand off to official-main integration

**Files:**
- Verify: no USB production files changed.

- [ ] **Step 1: Prove the mixed branch was not merged**

Run:

```bash
git merge-base --is-ancestor codex/megrez-usb-keyboard HEAD && exit 1 || true
git diff --quiet origin/main...HEAD -- kernel/comps/usb ostd/src/bus/usb
```

Expected: the source branch is not an ancestor and production USB trees equal
`origin/main`.

- [ ] **Step 2: Record the publication boundary**

Report:

```text
origin/codex/usb-stack-cleanup: published disposition record
origin/main: unchanged
origin/codex/megrez-usb-keyboard: retained as provenance
upstream/*: unchanged
issue #75: open for PCI xHCI and physical-board validation
```

- [ ] **Step 3: Resume the later upstream-main migration**

Use `origin/main` plus this ledger as the downstream baseline.
Do not merge,
rebase,
or copy the frozen USB topic worktree.
