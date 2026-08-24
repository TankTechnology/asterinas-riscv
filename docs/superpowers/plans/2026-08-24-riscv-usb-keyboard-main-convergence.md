# RISC-V USB Keyboard Main Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge the pinned downstream main into the existing RISC-V USB keyboard topic history without rewriting it, while making main's IRQ, USB, PCI, DMA, input, TTY, and build contracts the exact M0 baseline.

**Architecture:** Perform a normal two-parent merge on an isolated branch created from the approved topic design. Resolve core conflicts by restoring the pinned main tree, remove the historical PCI xHCI adapter that would otherwise merge without conflict, and preserve historical tests and board tools only as separately audited artifacts. The only approved RISC-V reboot recovery exception to main authority is the `cfg(riscv64)` crate registration and bootstrap arming needed by the retained panic hook and `boot_reboot.rs`. M0 adds no controller behavior; it produces the verified base for the new PCI xHCI plan.

**Tech Stack:** Git worktrees and three-way merge, Rust nightly, Cargo/`cargo-osdk`, Python `unittest`, Docker, QEMU RISC-V, Asterinas OSTD/USB/PCI/input components.

---

## Pinned Refs and Historical Ledger

```text
TOPIC_DESIGN       4cc608b070923ac4a10b9c30ed937f0b9e35d188
INTEGRATION_MAIN   8cd69a7d521cfa05adb52c05979cceaa58b29ab8
MERGE_BASE         1ed8a46c54afa7731f8e95f745d1b120ac5d8cc6
DIRTY_BACKUP       refs/codex-backup/megrez-usb-keyboard-dirty-20260824
DIRTY_BACKUP_SHA   2be8052f3542400424c9d5267a3e542f97408497
```

| Topic change | Classification | M0 treatment |
|---|---|---|
| `bc625863b` USB/DMA/HID foundation | Superseded | Restore main USB, DMA, and input trees |
| `273407415`, `61f638693` event-ring IRQ | Superseded | Restore main USB and IRQ trees |
| `1e176746a` softirq lockfile | Superseded | Restore main manifests and lockfile |
| `47364d032` PCI BAR allocation | Superseded | Restore main PCI tree |
| `32979fab9` PCI xHCI adapter | Reimplement | Remove historical `pci.rs`; retain requirements |
| `ba139ca91` PCI interrupt-map matching | Re-evaluate | Retain as a requirement, not source |
| `37adeb80e` USB input to TTY | Reimplement if missing | Restore main input/TTY and test it |
| `220d770aa` QEMU input echo | Compatibility artifact | Retain the scenario, not its init code |
| `f6ba5c3c3` echo outside TTY lock | Reimplement if needed | Restore main TTY; retain the deadlock concern |
| `c4ae2212c` RISC-V software reboot recovery | Retain as an approved exception | Keep `boot_reboot.rs`, its panic hook, and the two `cfg(riscv64)` wiring sites |
| `767e27e64`, `3d45f6e73` Megrez tools | Freshly verify | Preserve only while their focused tests pass |

## Completed Isolation Record

- [x] The original dirty worktree remains at `/home/ubuntu/xaj/Program/asterinas`.
- [x] The backup ref contains exactly `Cargo.lock`, USB `mod.rs`, and USB `pci.rs`.
- [x] The convergence worktree is `/home/ubuntu/.config/superpowers/worktrees/asterinas/megrez-usb-keyboard-convergence` on `codex/megrez-usb-keyboard-main-convergence`.
- [x] USB HID baseline: 49 tests passed.
- [x] Board-session baseline: three tests passed with `PYTHONPATH=tools/riscv`.
- [x] QEMU/U-Boot contract baseline: 171 tests passed with one supported skip.

The board-session test does not import without `PYTHONPATH=tools/riscv`.
Commands below include that required path.

### Task 1: Commit the M0 plan

**Files:**
- Create: `docs/superpowers/plans/2026-08-24-riscv-usb-keyboard-main-convergence.md`
- Verify: `docs/superpowers/specs/2026-08-24-riscv-xhci-usb-keyboard-convergence-design.md`

- [ ] **Step 1: Verify the plan**

```bash
git diff --check
! rg -n 'T[O]DO|T[B]D|F[I]XME|current `origin/ma[i]n`' docs/superpowers/plans/2026-08-24-riscv-usb-keyboard-main-convergence.md
git rev-parse HEAD
```

Expected: all checks pass and `HEAD` is `4cc608b070923ac4a10b9c30ed937f0b9e35d188`.

- [ ] **Step 2: Commit only the plan**

```bash
git add docs/superpowers/plans/2026-08-24-riscv-usb-keyboard-main-convergence.md
git diff --cached --check
git commit -m "docs(riscv): plan USB keyboard main convergence"
```

Expected: one Markdown file is committed and the worktree is clean.

### Task 2: Establish the merge RED state

**Files:**
- Verify only: Git graph

- [ ] **Step 1: Prove the pinned main is not yet an ancestor**

```bash
INTEGRATION_MAIN=8cd69a7d521cfa05adb52c05979cceaa58b29ab8
git merge-base --is-ancestor "$INTEGRATION_MAIN" HEAD
```

Expected: exit status 1.

- [ ] **Step 2: Verify the merge base and divergence**

```bash
git merge-base HEAD "$INTEGRATION_MAIN"
git rev-list --left-right --count HEAD..."$INTEGRATION_MAIN"
```

Expected: merge base `1ed8a46c54afa7731f8e95f745d1b120ac5d8cc6`, with 47 topic-side commits after the merge-gate corrections and 315 main-side commits.

### Task 3: Start the merge and confirm its conflict contract

**Files:**
- Conflict: `Cargo.lock`
- Conflict: `Cargo.toml`
- Conflict: `docs/superpowers/plans/2026-08-20-riscv-ltp-gate-baseline.md`
- Conflict: `docs/superpowers/plans/2026-08-20-wayland-global-size-fix.md`
- Conflict: `docs/superpowers/specs/2026-08-20-riscv-ltp-layered-integration-design.md`
- Conflict: `kernel/comps/pci/src/arch/riscv/mod.rs`
- Conflict: `kernel/comps/pci/src/cfg_space.rs`
- Conflict: `kernel/comps/usb/Cargo.toml`
- Conflict: `kernel/comps/usb/src/arch/riscv/mod.rs`
- Conflict: `kernel/comps/usb/src/lib.rs`
- Conflict: `kernel/src/device/registry/char.rs`
- Conflict: `kernel/src/device/tty/line_discipline.rs`
- Conflict: `kernel/src/init.rs`
- Conflict: `ostd/src/bus/usb.rs`
- Conflict: `ostd/src/io/io_mem/mod.rs`
- Conflict: `ostd/src/mm/dma/dma_coherent.rs`
- Conflict: `ostd/src/mm/dma/test.rs`
- Conflict: `ostd/src/mm/dma/usb_kernel_op.rs`

- [ ] **Step 1: Merge without committing**

```bash
INTEGRATION_MAIN=8cd69a7d521cfa05adb52c05979cceaa58b29ab8
git merge --no-ff --no-commit "$INTEGRATION_MAIN"
```

Expected: Git stops with conflicts and creates no commit.

- [ ] **Step 2: Assert all 18 paths and no others are unresolved**

```bash
git diff --name-only --diff-filter=U | sort
```

Expected: exactly the 18 paths listed above.
If the pinned merge differs, abort and revise the plan before resolving files.

### Task 4: Restore the main-authoritative implementation

**Files:**
- Restore: workspace manifests, lockfile, and `Makefile`
- Restore: `kernel/comps/input/`, `kernel/comps/pci/`, `kernel/comps/usb/`
- Restore: evdev, char registry, TTY, kernel init and crate root
- Restore: OSTD USB, DMA, MMIO, and all affected architecture IRQ trees
- Restore: the three conflicting 2026-08-20 plan/spec files
- Remove through restore: `kernel/comps/usb/src/arch/riscv/pci.rs`

- [ ] **Step 1: Restore all authority paths from the pinned main**

```bash
INTEGRATION_MAIN=8cd69a7d521cfa05adb52c05979cceaa58b29ab8
git restore --source="$INTEGRATION_MAIN" --staged --worktree -- \
  Cargo.toml Cargo.lock Makefile Components.toml kernel/Cargo.toml \
  kernel/comps/input kernel/comps/pci kernel/comps/usb \
  kernel/src/device/evdev kernel/src/device/registry/char.rs \
  kernel/src/device/tty kernel/src/init.rs kernel/src/lib.rs \
  ostd/Cargo.toml ostd/src/arch/loongarch/irq \
  ostd/src/arch/riscv/irq ostd/src/arch/x86/irq \
  ostd/src/bus/usb.rs ostd/src/bus/usb ostd/src/io/io_mem/mod.rs \
  ostd/src/irq ostd/src/mm/dma \
  docs/superpowers/plans/2026-08-20-riscv-ltp-gate-baseline.md \
  docs/superpowers/plans/2026-08-20-wayland-global-size-fix.md \
  docs/superpowers/specs/2026-08-20-riscv-ltp-layered-integration-design.md
```

Expected: the named paths match `INTEGRATION_MAIN`; historical USB `pci.rs` is removed.

- [ ] **Step 2: Prove conflict resolution and PCI-adapter removal**

```bash
test -z "$(git diff --name-only --diff-filter=U)"
test ! -e kernel/comps/usb/src/arch/riscv/pci.rs
git diff --cached "$INTEGRATION_MAIN" --check
```

Expected: all commands exit zero.

- [ ] **Step 3: Prove the authority trees are byte-identical to main**

```bash
git diff --exit-code "$INTEGRATION_MAIN" -- \
  Cargo.toml Cargo.lock Makefile Components.toml kernel/Cargo.toml \
  kernel/comps/input kernel/comps/pci kernel/comps/usb \
  kernel/src/device/evdev kernel/src/device/registry/char.rs \
  kernel/src/device/tty \
  ostd/Cargo.toml ostd/src/arch/loongarch/irq \
  ostd/src/arch/riscv/irq ostd/src/arch/x86/irq \
  ostd/src/bus/usb.rs ostd/src/bus/usb ostd/src/io/io_mem/mod.rs \
  ostd/src/irq ostd/src/mm/dma
```

Expected: no output and exit status 0. `kernel/src/init.rs` and
`kernel/src/lib.rs` are excluded only for the approved RISC-V reboot recovery
exception: `crate::boot_reboot::arm_if_requested()` and `mod boot_reboot;`,
both guarded by `cfg(riscv64)`.

### Task 5: Verify preserved compatibility artifacts

**Files:**
- Verify: `tools/usb-hid/`
- Verify: `tools/riscv/megrez_board_session.py`
- Verify: `tools/riscv/megrez_patch_dtb.py`
- Verify: `tools/riscv/verify_megrez_sim.sh`
- Verify: Megrez handoff and board-session documents

- [ ] **Step 1: Prove the audited artifacts remain**

```bash
test -f tools/usb-hid/boot_keyboard_oracle.py
test -f tools/riscv/megrez_board_session.py
test -f tools/riscv/megrez_patch_dtb.py
test -x tools/riscv/verify_megrez_sim.sh
test -f docs/porting/megrez-board-handoff-checklist.md
test -f docs/porting/megrez-board-session-commands.md
```

Expected: all checks exit zero.

- [ ] **Step 2: Run resolved-tree host tests**

```bash
python3 -m unittest discover -s tools/usb-hid/tests -p 'test_*.py' -v
timeout 10s env PYTHONPATH=tools/riscv python3 -m unittest \
  tools.riscv.tests.test_megrez_board_session \
  tools.riscv.tests.test_megrez_patch_dtb -v
python3 -m unittest tools.riscv.tests.test_qemu_uboot_contracts tools.riscv.tests.test_qemu_uboot_booti -v
```

Expected: 49 USB HID tests, 18 board-session tests, four DTB patch tests,
and 229 U-Boot contract tests pass; only the supported cross-compiler test
skips. The increase from the 171-test topic baseline is the pinned main's
added contract coverage. The board-tool command must finish before its
10-second outer deadline.

- [ ] **Step 3: Verify metadata and formatting**

```bash
cargo metadata --no-deps --format-version 1 > /tmp/riscv-usb-main-convergence-metadata.json
if cargo fmt --all -- --check > /tmp/riscv-usb-main-convergence-fmt.log 2>&1; then
  exit 1
fi
python3 - <<'PY'
from pathlib import Path
import re
import subprocess

integration_main = "8cd69a7d521cfa05adb52c05979cceaa58b29ab8"
root = Path.cwd().resolve()
output = Path("/tmp/riscv-usb-main-convergence-fmt.log").read_text()
fmt_paths = {
    str(Path(match).resolve().relative_to(root))
    for match in re.findall(r"^Diff in (.+?):[0-9]+:$", output, re.MULTILINE)
}
assert len(fmt_paths) == 31, sorted(fmt_paths)
for path in sorted(fmt_paths):
    subprocess.run(
        ["git", "diff", "--quiet", integration_main, "--", path],
        check=True,
    )
PY
git diff --cached "$INTEGRATION_MAIN" --check
python3 -c 'import json; d=json.load(open("/tmp/riscv-usb-main-convergence-metadata.json")); assert sum(p["name"] == "aster-usb" for p in d["packages"]) == 1'
```

Expected: metadata and scoped whitespace checks pass; the workspace has exactly
one `aster-usb` package; the formatter reports exactly 31 paths and every one
is byte-identical to the pinned main. Any topic-preserved or merge-resolution
format drift fails the per-path `git diff --quiet` check.

### Task 6: Commit and prove graph convergence

**Files:**
- Commit: the resolved merge index

- [ ] **Step 1: Create the merge commit**

```bash
git commit -m "Merge origin/main into RISC-V USB keyboard branch"
```

Expected: one commit with two parents.

- [ ] **Step 2: Run the ancestry GREEN check**

```bash
INTEGRATION_MAIN=8cd69a7d521cfa05adb52c05979cceaa58b29ab8
git merge-base --is-ancestor "$INTEGRATION_MAIN" HEAD
git show -s --format='%P' HEAD
```

Expected: ancestry exits zero and the second parent is exactly `INTEGRATION_MAIN`.

- [ ] **Step 3: Prove historical PCI xHCI code is absent**

```bash
test ! -e kernel/comps/usb/src/arch/riscv/pci.rs
! rg -n 'qemu-xhci|PCI xHCI|PciXhci' kernel/comps/usb ostd/src/bus/usb.rs
```

Expected: both checks exit zero; PCI xHCI remains an M1 requirement.

### Recorded M0 quality correction: restore RISC-V reboot wiring

Commit `d99ba8dc007109aee71827d20113eeced7fbeef5` restores the two approved
`cfg(riscv64)` wiring sites after the merge had retained `boot_reboot.rs` and
the panic hook but left the module unreachable and unarmed.

The pre-fix pinned-container command

```bash
cargo osdk check --ktests -p ostd -p aster-kernel \
  --target riscv64imac-unknown-none-elf
```

failed with `E0433` at `kernel/src/thread/oops.rs:95`. The identical command
passed after `d99ba8dc0`; it compiled the seven existing `boot_reboot` ktests.
No runtime reboot or QEMU ktest result is claimed by this correction: a
focused runtime attempt stopped before launch because the generated test
initramfs was absent.

### Task 7: Run local RISC-V build and QEMU foundation gates

**Files:**
- Verify: main-authoritative USB, PCI, DMA, IRQ, input, and TTY trees

- [ ] **Step 1: Compile RISC-V kernel tests in the pinned container**

```bash
docker run --rm --privileged --network=host -v /dev:/dev \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702 \
  cargo osdk check --ktests -p ostd -p aster-kernel --target riscv64imac-unknown-none-elf
```

Expected: exit status 0.

- [ ] **Step 2: Lint the USB and PCI crates**

```bash
docker run --rm --privileged --network=host -v /dev:/dev \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702 \
  sh -lc 'RUSTFLAGS=-Dwarnings cargo clippy -p aster-usb -p aster-pci --target riscv64imac-unknown-none-elf --no-deps'
```

Expected: exit status 0 and no warning promoted to an error.

- [ ] **Step 3: Run OSTD ktests in four-hart QEMU**

```bash
docker run --rm --privileged --network=host -v /dev:/dev \
  -v "$PWD:/root/asterinas" -w /root/asterinas/ostd \
  asterinas/asterinas:0.18.0-20260702 \
  sh -lc 'OSDK_TARGET_ARCH=riscv64 cargo osdk test --scheme riscv --qemu-args="-smp 4"'
```

Expected: all OSTD tests pass and QEMU exits zero. M0 does not attach `qemu-xhci`.

- [ ] **Step 4: Rebuild the normal four-hart kernel after ktests**

```bash
docker run --rm --privileged --network=host -v /dev:/dev \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702 \
  make kernel TARGET_ARCH=riscv64 SMP=4
```

Expected: exit status 0 and non-empty normal artifacts under `target/osdk/aster-kernel/`.

### Task 8: Close M0 before planning PCI xHCI

**Files:**
- Verify: merge graph and worktree
- Reference: `docs/superpowers/specs/2026-08-24-riscv-xhci-usb-keyboard-convergence-design.md`

- [ ] **Step 1: Run final cheap checks without repeating QEMU**

```bash
git diff --check HEAD^
git status --short --branch
git log --oneline --decorate -5
```

Expected: clean worktree, visible two-parent merge, and no tracked generated artifact.

- [ ] **Step 2: Report the independent M0 result**

Report the plan and merge SHAs, both merge parents, host-test counts, compile/lint/QEMU/build results, historical PCI-adapter absence, backup ref, and unchanged original dirty worktree.

Expected: M0 is reviewable on its own. Write the M1 PCI xHCI plan from the merged APIs before adding controller code.

## Stop Conditions

- Abort and revise the plan if the pinned conflict set is not exactly 18 paths.
- Do not commit if any authority path differs from `INTEGRATION_MAIN`.
- Do not copy a topic implementation to hide a main-side contract change.
- On compile failure, identify the branch-only obsolete dependency and restore or remove it in a focused reviewed change.
- Do not push a remote ref or monitor remote CI during M0.
