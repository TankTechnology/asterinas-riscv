# Megrez USB Keyboard Main Synchronization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge current `origin/main` into the published Megrez USB keyboard topic history, resolve the three textual conflicts without losing either workstream, and produce a locally verified fast-forward candidate for pull request #1.

**Architecture:** Create `codex/megrez-usb-keyboard-sync-20260819` from the approved topic head in the repository's established global worktree area. Perform a normal two-parent merge, resolve only the three actual textual conflicts, then validate the combined PCI/xHCI, USB keyboard, DMA, TTY, RISC-V boot, and main-branch functionality in increasing-cost stages. Keep publication separate from implementation: the final candidate is reported but not pushed.

**Tech Stack:** Git worktrees, Rust nightly, Cargo/`cargo-osdk`, Python `unittest`, QEMU RISC-V, U-Boot, DTB tooling, Bash.

---

### Task 1: Create the isolated synchronization worktree and establish the baseline

**Files:**
- Verify: `docs/superpowers/specs/2026-08-19-megrez-usb-main-sync-design.md`
- Verify: `tools/usb-hid/tests/test_boot_keyboard_oracle.py`
- Verify: `tools/riscv/tests/test_megrez_board_session.py`

- [ ] **Step 1: Confirm refs and repository identity**

Run from the existing `asterinas-riscv` checkout:

```bash
git remote get-url origin
git rev-parse HEAD
git rev-parse codex/megrez-usb-keyboard
git rev-parse origin/main
git merge-base HEAD origin/main
git status --short --branch
```

Expected:

```text
origin: https://github.com/TankTechnology/asterinas-riscv.git
HEAD equals codex/megrez-usb-keyboard and contains design commit db429cbe8
origin/main: 1ed8a46c54afa7731f8e95f745d1b120ac5d8cc6
merge base: 09dcf1e63b18f892489ec7d65cf9f20b4e4585bf
```

The status may list the nine pre-existing untracked log/cache/worktree paths;
none may be staged, deleted, or copied into the new worktree.

- [ ] **Step 2: Create the synchronization worktree**

Use the global worktree convention already used by this repository:

```bash
git worktree add \
  /home/ubuntu/.config/superpowers/worktrees/asterinas/megrez-usb-main-sync \
  -b codex/megrez-usb-keyboard-sync-20260819 \
  codex/megrez-usb-keyboard
```

Expected: Git creates the new branch at the approved topic head, including the
design and implementation-plan commits. No project-local ignore change is
required because the worktree is outside the repository.

- [ ] **Step 3: Verify the isolated baseline is clean**

Run in the new worktree:

```bash
git status --short --branch
git rev-parse HEAD
```

Expected: a clean `codex/megrez-usb-keyboard-sync-20260819` worktree at the
same commit as `codex/megrez-usb-keyboard`.

- [ ] **Step 4: Run cheap pre-merge regression tests**

```bash
python3 -m unittest discover -s tools/usb-hid/tests -p 'test_*.py' -v
python3 -m unittest tools.riscv.tests.test_megrez_board_session -v
python3 tools/usb-hid/boot_keyboard_oracle.py --check
```

Expected: all Python unit tests pass and the checked-in Linux keyboard vectors
match the oracle generator. If a command fails before the merge, record it as a
topic-branch baseline failure and stop for review rather than attributing it to
the integration.

### Task 2: Merge `origin/main` and resolve the three textual conflicts

**Files:**
- Modify: `Makefile`
- Modify: `kernel/comps/uart/src/console.rs`
- Modify: `tools/riscv/eic7700_isolation.sh`
- Verify auto-merge: `.gitignore`
- Verify auto-merge: `kernel/comps/input/src/event_type_codes.rs`
- Verify auto-merge: `ostd/src/arch/riscv/mm/eic7700_cache.rs`
- Verify auto-merge: `tools/riscv/prepare_qemu_uboot_booti.sh`

- [ ] **Step 1: Start a non-rewriting merge without committing**

```bash
git merge --no-ff --no-commit origin/main
git diff --name-only --diff-filter=U
```

Expected: the merge stops with exactly these three unresolved files:

```text
Makefile
kernel/comps/uart/src/console.rs
tools/riscv/eic7700_isolation.sh
```

- [ ] **Step 2: Resolve the trailing-whitespace check in `Makefile`**

Keep both protections: exclude tracked DTB blobs and ask `grep` to ignore any
other binary input. The final recipe line must be:

```make
	@if git --git-dir=$$PWD/.git ls-files | grep -v '[.]patch$$' | grep -v '[.]dtb$$' | xargs grep -I -d skip ' $$' ; then \
```

Also retain the topic branch's `.dtb` exclusion in the `format` recipe.

- [ ] **Step 3: Resolve `DiagnosticSendError` in the UART console**

Retain the explanatory topic-branch comment and the attribute present on both
lines of development:

```rust
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum DiagnosticSendError {
    // Only MMIO UART backends (RISC-V dw-apb/SiFive) can fail this way.
    #[cfg_attr(not(target_arch = "riscv64"), expect(dead_code))]
    Io,
}
```

- [ ] **Step 4: Resolve the EIC7700 script header**

Use the repository's current shell-header convention without duplicate blank
lines or license markers:

```bash
#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# EIC7700 isolation verification: pure QEMU, no physical board required.
```

- [ ] **Step 5: Stage conflict resolutions and prove the index is resolved**

```bash
git add Makefile kernel/comps/uart/src/console.rs tools/riscv/eic7700_isolation.sh
git diff --name-only --diff-filter=U
git diff --cached --check
```

Expected: no unmerged paths and no whitespace errors.

- [ ] **Step 6: Audit the four auto-merged paths**

Run:

```bash
rg -n 'target-ubuntu|__pycache__|[*][.]pyc' .gitignore
rg -n 'pub enum AbsCode|Key102nd|SysRq|Compose' kernel/comps/input/src/event_type_codes.rs
rg -n 'has_uncached_dram_alias|is_eic7700_compatible' ostd/src/arch/riscv/mm/eic7700_cache.rs
sed -n '1,12p' tools/riscv/prepare_qemu_uboot_booti.sh
```

Expected: `.gitignore` contains both workstreams' entries; the input enum
contains main's absolute-axis support and the USB keyboard key codes; the
EIC7700 helper and compatibility tests remain; the boot script has one SPDX
header and retains the current main-side implementation below it.

- [ ] **Step 7: Commit the two-parent merge**

```bash
git commit -m "Merge origin/main into Megrez USB keyboard branch"
git show -s --format='%H%n%P%n%s' HEAD
```

Expected: the commit has two parents, first equal to the synchronization
branch's pre-merge topic head and second equal to `1ed8a46c5`.

### Task 3: Validate repository structure and host-side tooling

**Files:**
- Verify: `Cargo.toml`
- Verify: `Cargo.lock`
- Verify: `Components.toml`
- Verify: `kernel/comps/usb/Cargo.toml`
- Test: `tools/usb-hid/tests/test_boot_keyboard_oracle.py`
- Test: `tools/riscv/tests/test_megrez_board_session.py`
- Test: `tools/riscv/tests/test_qemu_uboot_contracts.py`
- Test: `tools/riscv/tests/test_qemu_uboot_booti.py`

- [ ] **Step 1: Confirm the USB component remains in the resolved workspace**

```bash
cargo metadata --no-deps --format-version 1 > /tmp/megrez-usb-cargo-metadata.json
jq -e '.packages[] | select(.name == "aster-usb")' /tmp/megrez-usb-cargo-metadata.json
rg -n 'aster-usb|kernel/comps/usb' Cargo.toml Components.toml kernel/Cargo.toml
```

Expected: `aster-usb` appears exactly once as a workspace package and remains
wired into the kernel component graph.

- [ ] **Step 2: Run formatting and structural checks**

```bash
cargo fmt --all -- --check
git diff --check codex/megrez-usb-keyboard..HEAD
git status --short
```

Expected: formatting and diff checks pass; the worktree has no unstaged or
untracked integration artifacts.

- [ ] **Step 3: Re-run USB and board tooling tests after the merge**

```bash
python3 -m unittest discover -s tools/usb-hid/tests -p 'test_*.py' -v
python3 -m unittest tools.riscv.tests.test_megrez_board_session -v
python3 tools/usb-hid/boot_keyboard_oracle.py --check
```

Expected: the same test set that passed before the merge still passes.

- [ ] **Step 4: Run the RISC-V boot-contract unit suite**

```bash
python3 -m unittest \
  tools.riscv.tests.test_qemu_uboot_contracts \
  tools.riscv.tests.test_qemu_uboot_booti -v
```

Expected: all tests pass. Cross-toolchain-dependent tests may skip only when
their own test contract reports a supported environmental skip.

### Task 4: Build and test the merged Rust kernel serially

**Files:**
- Verify: `kernel/comps/usb/src/arch/riscv/mod.rs`
- Verify: `kernel/comps/usb/src/keyboard.rs`
- Verify: `ostd/src/bus/usb.rs`
- Verify: `ostd/src/bus/usb/report_queue.rs`
- Verify: `ostd/src/mm/dma/`
- Verify: `kernel/src/device/tty/`

- [ ] **Step 1: Check the affected RISC-V crates**

Run inside the pinned Asterinas development container:

```bash
cargo check -p ostd --target riscv64imac-unknown-none-elf
cargo check -p aster-pci -p aster-usb --target riscv64imac-unknown-none-elf
```

Expected: both commands finish successfully with no new warnings promoted to
errors.

- [ ] **Step 2: Build the complete RISC-V kernel**

```bash
make kernel TARGET_ARCH=riscv64
```

Expected: the kernel build exits zero and produces non-empty QEMU ELF and Linux
Image artifacts under `target/osdk/aster-kernel/`.

- [ ] **Step 3: Run kernel tests after the normal build completes**

```bash
make ktest TARGET_ARCH=riscv64
```

Expected: OSTD DMA/USB, USB report queue and keyboard, TTY, UART, and the rest
of the RISC-V kernel-test set pass. Do not run this concurrently with the
normal kernel build because `cargo osdk test` replaces the QEMU ELF artifact.

- [ ] **Step 4: Rebuild the normal kernel after KTest**

```bash
make kernel TARGET_ARCH=riscv64
```

Expected: the normal QEMU ELF and Linux Image artifacts are restored for the
runtime gates.

### Task 5: Run the Megrez and USB runtime acceptance gates

**Files:**
- Verify: `tools/riscv/verify_megrez_sim.sh`
- Verify: `tools/riscv/make_qemu_uboot_initramfs.py`
- Verify: `tools/riscv/qemu_uboot_init.S`
- Verify: `tools/riscv/megrez_patch_dtb.py`
- Verify: `docs/porting/evidence/2026-08-10-megrez-pre-upload-test-report.md`

- [ ] **Step 1: Run the Megrez Sv48/Svade contract simulation**

Use the existing U-Boot simulation image and mount the synchronization
worktree as the repository:

```bash
docker run --rm \
  -v /home/ubuntu/.config/superpowers/worktrees/asterinas/megrez-usb-main-sync:/root/asterinas \
  -w /root/asterinas \
  asterinas-env:uboot-sim \
  bash -lc 'export PATH=/usr/local/qemu/bin:$PATH; tools/riscv/verify_megrez_sim.sh'
```

Expected: `classification: PASS`, `marker_seen=yes`, the userspace marker is
present, and the script prints SHA-256 identities for the kernel, initramfs,
and DTB.

- [ ] **Step 2: Prepare a direct-QEMU USB keyboard boot**

```bash
python3 tools/riscv/make_qemu_uboot_initramfs.py \
  target/qemu-uboot/usb-keyboard-initramfs.cpio.gz
mkdir -p target/qemu-uboot/usb-keyboard-runtime
qemu-system-riscv64 \
  -machine virt \
  -cpu rv64,svpbmt=true,zkr=true \
  -m 2G -smp 1 -no-reboot -display none \
  -kernel target/osdk/aster-kernel/aster-kernel-osdk-bin.qemu_elf \
  -initrd target/qemu-uboot/usb-keyboard-initramfs.cpio.gz \
  -append 'earlycon console=ttyS0 init=/init loglevel=info' \
  -device qemu-xhci,id=xhci \
  -device usb-kbd,bus=xhci.0 \
  -serial file:target/qemu-uboot/usb-keyboard-runtime/serial.log \
  -monitor unix:target/qemu-uboot/usb-keyboard-runtime/monitor.sock,server=on,wait=off \
  -daemonize
```

Expected: QEMU starts, the monitor socket appears, and the serial log reaches
the RISC-V userspace marker with one USB keyboard registration.

- [ ] **Step 3: Inject the keyboard acceptance sequence**

```bash
printf '%s\n' \
  'sendkey a' 'sendkey b' 'sendkey 1' 'sendkey z' \
  'sendkey shift-a' 'sendkey shift-1' \
  'sendkey caps_lock' 'sendkey a' \
  'sendkey ret' 'sendkey spc' 'sendkey tab' 'sendkey esc' \
  'sendkey backspace' 'sendkey ctrl-c' \
  'sendkey a' 'sendkey a' 'sendkey a' 'sendkey a' 'sendkey a' \
  'sendkey a' 'sendkey b' 'sendkey a' 'sendkey b' 'sendkey a' \
  'sendkey b' 'sendkey a' 'sendkey b' 'sendkey a' 'sendkey b' \
  | socat - UNIX-CONNECT:target/qemu-uboot/usb-keyboard-runtime/monitor.sock
sleep 5
printf 'quit\n' | socat - UNIX-CONNECT:target/qemu-uboot/usb-keyboard-runtime/monitor.sock
```

Inspect the result:

```bash
rg -n 'USB|keyboard|panic|Hello from RISC-V userspace' \
  target/qemu-uboot/usb-keyboard-runtime/serial.log
python3 - <<'PY'
from pathlib import Path

log = Path("target/qemu-uboot/usb-keyboard-runtime/serial.log").read_bytes()
assert log.count(b"USB boot keyboard registered:") == 1
assert b"panic" not in log.lower()
for expected in (b"ab1z", b"A!", b"aaaaaababababab"):
    assert expected in log, expected
print("USB keyboard runtime acceptance: PASS")
PY
```

Expected: base keys, modifiers, Caps Lock, special/control keys, and the rapid
sequence are delivered; registration occurs once; no panic is present.

- [ ] **Step 4: Run the invalid-DWC3-selector fail-safe case**

```bash
qemu-system-riscv64 -machine virt,dumpdtb=target/qemu-uboot/usb-keyboard-runtime/invalid-dwc3.dtb
fdtput -p -t s \
  target/qemu-uboot/usb-keyboard-runtime/invalid-dwc3.dtb \
  /chosen asterinas,usb-host /soc/usb@deadbeef
```

Start the fail-safe guest with the invalid DTB and independent output paths:

```bash
qemu-system-riscv64 \
  -machine virt \
  -cpu rv64,svpbmt=true,zkr=true \
  -m 2G -smp 1 -no-reboot -display none \
  -kernel target/osdk/aster-kernel/aster-kernel-osdk-bin.qemu_elf \
  -initrd target/qemu-uboot/usb-keyboard-initramfs.cpio.gz \
  -dtb target/qemu-uboot/usb-keyboard-runtime/invalid-dwc3.dtb \
  -append 'earlycon console=ttyS0 init=/init loglevel=info' \
  -device qemu-xhci,id=xhci \
  -device usb-kbd,bus=xhci.0 \
  -serial file:target/qemu-uboot/usb-keyboard-runtime/invalid-dwc3-serial.log \
  -monitor unix:target/qemu-uboot/usb-keyboard-runtime/invalid-dwc3-monitor.sock,server=on,wait=off \
  -daemonize
```

Then inject `sendkey a`, wait for its echo, and quit through the second monitor
socket. Verify:

```bash
printf 'sendkey a\n' \
  | socat - UNIX-CONNECT:target/qemu-uboot/usb-keyboard-runtime/invalid-dwc3-monitor.sock
sleep 5
printf 'quit\n' \
  | socat - UNIX-CONNECT:target/qemu-uboot/usb-keyboard-runtime/invalid-dwc3-monitor.sock
rg -n 'failed to resolve USB host|Hello from RISC-V userspace' \
  target/qemu-uboot/usb-keyboard-runtime/invalid-dwc3-serial.log
! rg -ni 'panic' target/qemu-uboot/usb-keyboard-runtime/invalid-dwc3-serial.log
```

Expected: the invalid selected DWC3 node produces the documented warning, PCI
xHCI fallback continues to deliver the key, userspace is reached, and no panic
occurs.

### Task 6: Run final repository checks and prepare the review handoff

**Files:**
- Verify all files in the merge commit.

- [ ] **Step 1: Run the full lint/check gate**

```bash
make check TARGET_ARCH=riscv64
```

Expected: formatting, Clippy, typo, license, API-documentation, and whitespace
checks pass. If a failure also reproduces at `origin/main`, capture both command
outputs and classify it as a main-branch baseline issue before considering any
code change.

- [ ] **Step 2: Prove ancestry and merge shape**

```bash
git merge-base --is-ancestor codex/megrez-usb-keyboard HEAD
git merge-base --is-ancestor 1ed8a46c54afa7731f8e95f745d1b120ac5d8cc6 HEAD
git rev-list --left-right --count origin/main...HEAD
git log --first-parent --oneline codex/megrez-usb-keyboard..HEAD
git status --short --branch
```

Expected: both ancestry checks exit zero, the first-parent log contains the
normal merge commit, and the worktree is clean.

- [ ] **Step 3: Review the integrated diff**

```bash
git diff --stat origin/main...HEAD
git diff --name-status origin/main...HEAD
git diff --check origin/main...HEAD
git range-diff \
  09dcf1e63b18f892489ec7d65cf9f20b4e4585bf..243edb99b64c9fc23277d193874042d3f64da9a7 \
  1ed8a46c54afa7731f8e95f745d1b120ac5d8cc6..HEAD
```

Expected: the topic functionality remains represented after the new main
baseline; no conflict marker, accidental generated artifact, or unrelated
track-branch content appears.

- [ ] **Step 4: Prepare the no-push handoff**

Report:

```text
- synchronization branch and worktree path
- merge commit hash and both parents
- exact conflict resolutions
- passing validation commands and test counts
- any baseline/environmental limitations
- resulting ahead/behind count against origin/main
- proposed fast-forward push command
```

The proposed publication command is:

```bash
git push origin HEAD:codex/megrez-usb-keyboard
```

Do not run it until the reviewed local result is explicitly approved. Never use
`--force` or `--force-with-lease` for this branch.
