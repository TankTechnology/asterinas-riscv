# Megrez USB/xHCI Core Main Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the tested USB keyboard and xHCI core from `codex/megrez-usb-keyboard` onto the current local `main`, prove it with RISC-V host, kernel, and QEMU `smp=4` gates, and leave a clean fast-forward candidate without the topic branch's deferred desktop and board-management work.

**Architecture:** Replay the existing source commits in dependency order on `codex/megrez-usb-core-main-integration`, preserving current main-side interfaces when conflicts arise. The port is divided into USB/OSTD foundations, interrupt-driven xHCI, PCI discovery, TTY delivery, and portability cleanup; each batch is independently tested and reviewed before the next begins.

**Tech Stack:** Rust nightly, Asterinas OSTD and component graph, CrabUSB/xHCI, RISC-V PCI and PLIC, Python `unittest`, Docker, QEMU RISC-V, HID boot-keyboard reports.

---

## Source and Integration Refs

- Integration worktree: `/home/ubuntu/.config/superpowers/worktrees/asterinas/megrez-usb-core-main`
- Integration branch: `codex/megrez-usb-core-main-integration`
- Approved design commit: `92fc5506f`
- Source branch: `codex/megrez-usb-keyboard`
- Source head at planning time: `ecdea5a39`
- Integration base: local `main` at `cc0d19383`

This is a tested-code transplant, not a new implementation. The source commits already contain their behavioral tests, so the integration discipline is: prove the target feature is absent, transplant the exact tested commit with `-x`, run its focused tests immediately, and add a new regression test only when adapting to a changed main-side interface exposes a new failure.

### Task 1: Establish clean target and source baselines

**Files:**
- Verify: `docs/superpowers/specs/2026-08-21-megrez-usb-core-main-integration-design.md`
- Verify source: `tools/usb-hid/tests/test_boot_keyboard_oracle.py`
- Verify source: `docs/porting/evidence/2026-08-10-megrez-pre-upload-test-report.md`

- [ ] **Step 1: Verify the exact refs and clean integration worktree**

```bash
git rev-parse HEAD
git rev-parse main
git rev-parse codex/megrez-usb-keyboard
git status --short --branch
```

Expected: `HEAD` is the approved design descendant `92fc5506f`, `main` is
`cc0d19383`, the source is `ecdea5a39`, and the integration worktree is clean.

- [ ] **Step 2: Prove USB is absent from the current target**

```bash
test ! -e kernel/comps/usb/Cargo.toml
test ! -e ostd/src/bus/usb.rs
test ! -e tools/usb-hid/boot_keyboard_oracle.py
```

Expected: all three absence checks exit zero.

- [ ] **Step 3: Re-run the source branch's host oracle unit baseline**

```bash
git -C /mnt/shared/xaj/Program/asterinas status --short --branch
python3 -m unittest discover \
  -s /mnt/shared/xaj/Program/asterinas/tools/usb-hid/tests \
  -p 'test_*.py' -v
```

Expected: 49 tests pass. The source status may show only the known local
workspace symlink and generated log files; none are staged or copied.

- [ ] **Step 4: Record the physical-oracle environmental boundary**

```bash
if test -r /dev/uhid && test -w /dev/uhid; then
  python3 /mnt/shared/xaj/Program/asterinas/tools/usb-hid/boot_keyboard_oracle.py --check
else
  printf '%s\n' 'SKIP: /dev/uhid is not readable and writable'
fi
```

Expected: the check passes when UHID is available; otherwise the exact skip is
recorded as an environment limitation and the deterministic 49-test suite
remains the host baseline.

### Task 2: Port the USB, DMA, and HID foundation

**Files:**
- Create: `kernel/comps/usb/Cargo.toml`
- Create: `kernel/comps/usb/src/lib.rs`
- Create: `kernel/comps/usb/src/arch/other.rs`
- Create: `kernel/comps/usb/src/arch/riscv/capability.rs`
- Create: `kernel/comps/usb/src/arch/riscv/mod.rs`
- Create: `kernel/comps/usb/src/keyboard.rs`
- Create: `kernel/comps/usb/src/keyboard_linux_vectors.rs`
- Create: `ostd/src/bus/usb.rs`
- Create: `ostd/src/bus/usb/report_queue.rs`
- Create: `ostd/src/mm/dma/dma_window.rs`
- Create: `ostd/src/mm/dma/usb_kernel_op.rs`
- Create: `tools/usb-hid/README.md`
- Create: `tools/usb-hid/boot_keyboard_oracle.py`
- Create: `tools/usb-hid/requirements.txt`
- Create: `tools/usb-hid/tests/test_boot_keyboard_oracle.py`
- Modify: `Cargo.toml`
- Modify: `Cargo.lock`
- Modify: `Components.toml`
- Modify: `kernel/Cargo.toml`
- Modify: `kernel/comps/input/src/event_type_codes.rs`
- Modify: `kernel/src/device/registry/char.rs`
- Modify: `kernel/src/device/tty/line_discipline.rs`
- Modify: `kernel/src/device/tty/termio.rs`
- Modify: `kernel/src/init.rs`
- Modify: `kernel/src/lib.rs`
- Modify: `ostd/Cargo.toml`
- Modify: `ostd/src/arch/riscv/mm/eic7700_cache.rs`
- Modify: `ostd/src/arch/riscv/mm/mod.rs`
- Modify: `ostd/src/bus.rs`
- Modify: `ostd/src/io/io_mem/mod.rs`
- Modify: `ostd/src/mm/dma/dma_coherent.rs`
- Modify: `ostd/src/mm/dma/mod.rs`
- Modify: `ostd/src/mm/dma/test.rs`

- [ ] **Step 1: Transplant the tested foundation commit**

```bash
git cherry-pick -x bc625863b58e4ec74a651511a94b97f6a9c1da47
```

Expected: the commit applies as one provenance-preserving commit. If current
main produces a conflict, keep all newly added USB/oracle files, preserve the
current main-side API in existing files, and add only the component, DMA,
input-code, init, and registry wiring shown by the source commit. Do not copy
any framebuffer, reboot, NixOS, or board-session file.

- [ ] **Step 2: Verify component and workspace wiring**

```bash
cargo metadata --no-deps --format-version 1 > /tmp/megrez-usb-core-metadata.json
jq -e '[.packages[] | select(.name == "aster-usb")] | length == 1' \
  /tmp/megrez-usb-core-metadata.json
rg -n 'aster-usb|kernel/comps/usb' Cargo.toml Components.toml kernel/Cargo.toml
```

Expected: exactly one `aster-usb` package exists and all three wiring searches
return the intended component entries.

- [ ] **Step 3: Run the transplanted host tests**

```bash
python3 -m unittest discover -s tools/usb-hid/tests -p 'test_*.py' -v
```

Expected: 49 tests pass from the integration worktree.

- [ ] **Step 4: Check the foundation commit structurally**

```bash
cargo fmt --all -- --check
git diff --check HEAD^
git status --short --branch
```

Expected: formatting and whitespace checks pass and the worktree is clean.

### Task 3: Port interrupt-driven event-ring handling

**Files:**
- Modify: `kernel/comps/usb/Cargo.toml`
- Modify: `kernel/comps/usb/src/arch/riscv/mod.rs`
- Modify: `ostd/src/bus/usb.rs`
- Modify: `Cargo.lock`

- [ ] **Step 1: Port the event-ring and lockfile commits in order**

```bash
git cherry-pick -x 2734074154f8558f0088ee7f31b516644900690c
git cherry-pick -x 1e176746acd322c2cf5f1cc9d2ae9d2da3518ce6
```

Expected: `aster-softirq` is wired into `aster-usb`; the interrupt handler
schedules deferred work instead of running an idle polling loop.

- [ ] **Step 2: Prove the intended interrupt architecture is present**

```bash
rg -n 'handle_event_irq|Taskless|IrqLine|IRQ_CHIP|aster-softirq' \
  kernel/comps/usb/Cargo.toml \
  kernel/comps/usb/src/arch/riscv/mod.rs \
  ostd/src/bus/usb.rs
! rg -n 'loop[[:space:]]*\{' kernel/comps/usb/src/arch/riscv/mod.rs
```

Expected: interrupt and deferred-work symbols are present and no unconditional
idle polling loop is introduced.

- [ ] **Step 3: Run focused structural tests**

```bash
python3 -m unittest discover -s tools/usb-hid/tests -p 'test_*.py' -v
cargo fmt --all -- --check
git diff --check HEAD~2
```

Expected: 49 tests pass and the two-commit batch is formatting-clean.

### Task 4: Port RISC-V PCI BAR allocation and PCI xHCI discovery

**Files:**
- Modify: `kernel/comps/pci/Cargo.toml`
- Modify: `kernel/comps/pci/src/arch/riscv/mod.rs`
- Modify: `kernel/comps/pci/src/cfg_space.rs`
- Modify: `kernel/comps/usb/Cargo.toml`
- Modify: `kernel/comps/usb/src/arch/riscv/mod.rs`
- Create: `kernel/comps/usb/src/arch/riscv/pci.rs`
- Modify: `Cargo.lock`

- [ ] **Step 1: Port PCI allocation, xHCI discovery, and address-aware IRQ mapping**

```bash
git cherry-pick -x 47364d032546f7a10f516e7c4829007ccf3382ad
git cherry-pick -x 32979fab9091d57b8b5f09040b9946e8dcac5ffc
git cherry-pick -x ba139ca91dbf19677a077cc1899862d54a1fc135
```

Expected: zero-valued RISC-V BARs can be allocated from firmware PCIe memory
ranges; the xHCI class driver stores BAR0, DMA, and IRQ resources; interrupt
map matching includes both masked PCI address and interrupt pin.

- [ ] **Step 2: Verify the PCI/xHCI contracts in source**

```bash
rg -n 'MmioAllocator|alloc_mmio|ranges' kernel/comps/pci/src/arch/riscv/mod.rs
rg -n '0x0[Cc]|0x03|0x30|BAR0|interrupt-map-mask|pci_address|interrupt_pin' \
  kernel/comps/usb/src/arch/riscv/pci.rs
```

Expected: both searches expose the allocator, xHCI class match, BAR0, and
address-plus-pin interrupt routing.

- [ ] **Step 3: Run formatting and host regressions**

```bash
cargo fmt --all -- --check
python3 -m unittest discover -s tools/usb-hid/tests -p 'test_*.py' -v
git diff --check HEAD~3
```

Expected: formatting passes, 49 host tests pass, and the batch has no
whitespace error.

### Task 5: Stabilize keyboard registration and deliver input through TTY

**Files:**
- Modify: `kernel/comps/usb/src/arch/riscv/mod.rs`
- Modify: `ostd/src/bus/usb.rs`
- Modify: `kernel/src/device/tty/vt/keyboard/handler.rs`
- Modify: `kernel/src/device/tty/line_discipline.rs`
- Modify: `kernel/src/device/tty/mod.rs`

- [ ] **Step 1: Port interrupt stabilization and serial-console delivery**

```bash
git cherry-pick -x 61f6386934ecbfd497e9f00d0f1dae7178d51474
git cherry-pick -x 37adeb80e9e4831bc326286eaed85f124043fbd1
```

Expected: xHCI global interrupts are enabled, the HID decoder and input device
persist across interrupt batches, and sleep-capable event delivery runs in task
context rather than softirq context.

- [ ] **Step 2: Run the existing TTY test before applying the echo fix**

```bash
docker run --rm --privileged --network=host -v /dev:/dev \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702 \
  bash -lc 'cd kernel && OSDK_TARGET_ARCH=riscv64 cargo osdk test \
    tty_echo_runs_without_the_line_discipline_lock --scheme riscv'
```

Expected: this focused test fails when the current integration still echoes
under the line-discipline lock. If current main has independently fixed it and
the test passes, record the evidence and skip the next source commit as already
implemented.

- [ ] **Step 3: Port the minimal echo-outside-lock fix when the red test requires it**

```bash
git cherry-pick -x f6ba5c3c32c0bd59ba427036648735cb7e1ecbe6
```

Expected: `push_char` returns at most two echo bytes and `push_input` writes
them only after releasing the line-discipline lock.

- [ ] **Step 4: Re-run the focused TTY test**

```bash
docker run --rm --privileged --network=host -v /dev:/dev \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702 \
  bash -lc 'cd kernel && OSDK_TARGET_ARCH=riscv64 cargo osdk test \
    tty_echo_runs_without_the_line_discipline_lock --scheme riscv'
```

Expected: the focused test passes.

### Task 6: Reconcile dependency and cross-architecture portability fixes

**Files:**
- Modify: `Cargo.toml`
- Modify: `Cargo.lock`
- Modify: `ostd/src/mm/dma/test.rs`
- Modify: `ostd/src/mm/dma/usb_kernel_op.rs`
- Modify: `.gitignore`
- Modify: `kernel/comps/usb/src/lib.rs`
- Modify: `kernel/src/device/registry/char.rs`
- Modify: `kernel/src/device/tty/mod.rs`
- Modify: `ostd/src/io/io_mem/mod.rs`
- Modify: `ostd/src/mm/dma/dma_coherent.rs`

- [ ] **Step 1: Port the deterministic DMA API dependency fix**

```bash
git cherry-pick -x c6057145bb2b50f7e9f282bea1521210868af7f0
```

Expected: the workspace pins `dma-api` to `=0.9.5`, and both production and
test deallocation handle its `Result` return value.

- [ ] **Step 2: Apply the source formatting normalization**

```bash
git cherry-pick -x 803786f9977173265c0b7d2eb1084dfbc7f444ac
```

Expected: no semantic change; only the files touched by the source formatting
commit change.

- [ ] **Step 3: Run the full formatting and non-RISC-V portability gate**

```bash
cargo fmt --all -- --check
docker run --rm --privileged --network=host -v /dev:/dev \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702 \
  bash -lc 'make check'
```

Expected: both pass. If `make check` reports only the exact architecture-gated
dead-code/import issues addressed by source commit `65e452bad`, apply that
commit with `git cherry-pick -x 65e452bad66f1fd9cbc113f56a6e39dd64aad731`,
preserving any newer main-side UART and registry implementation, then rerun
both commands. No unrelated lint change is authorized.

### Task 7: Build and run kernel tests serially

**Files:**
- Verify: `kernel/comps/usb/`
- Verify: `kernel/comps/pci/src/arch/riscv/mod.rs`
- Verify: `ostd/src/bus/usb.rs`
- Verify: `ostd/src/bus/usb/report_queue.rs`
- Verify: `ostd/src/mm/dma/`
- Verify: `kernel/src/device/tty/`

- [ ] **Step 1: Check the affected RISC-V crates**

```bash
docker run --rm --privileged --network=host -v /dev:/dev \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702 bash -lc '
    cargo check -p ostd --target riscv64imac-unknown-none-elf &&
    cargo check -p aster-pci -p aster-usb --target riscv64imac-unknown-none-elf
  '
```

Expected: both checks exit zero.

- [ ] **Step 2: Build the complete RISC-V kernel**

```bash
docker run --rm --privileged --network=host -v /dev:/dev \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702 \
  bash -lc 'make kernel TARGET_ARCH=riscv64'
```

Expected: a non-empty QEMU ELF and Linux Image are produced under
`target/osdk/aster-kernel/`.

- [ ] **Step 3: Run the RISC-V kernel tests after the normal build**

```bash
docker run --rm --privileged --network=host -v /dev:/dev \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702 \
  bash -lc 'make ktest TARGET_ARCH=riscv64'
```

Expected: OSTD DMA/USB, report queue, HID translation, PCI, TTY, UART, and the
remaining RISC-V kernel tests pass. Do not run this concurrently with a normal
kernel build.

- [ ] **Step 4: Restore the normal kernel artifact**

```bash
docker run --rm --privileged --network=host -v /dev:/dev \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702 \
  bash -lc 'make kernel TARGET_ARCH=riscv64'
```

Expected: the normal QEMU ELF is rebuilt after `cargo osdk test`.

### Task 8: Run the QEMU `smp=4` xHCI acceptance gates

**Files:**
- Verify: `tools/riscv/make_qemu_uboot_initramfs.py`
- Verify: `target/osdk/aster-kernel/aster-kernel-osdk-bin.qemu_elf`
- Create generated output: `target/qemu-uboot/usb-keyboard-runtime/`

- [ ] **Step 1: Build the deterministic keyboard initramfs**

```bash
python3 tools/riscv/make_qemu_uboot_initramfs.py \
  target/qemu-uboot/usb-keyboard-initramfs.cpio.gz
mkdir -p target/qemu-uboot/usb-keyboard-runtime
```

Expected: the initramfs exists and is non-empty.

- [ ] **Step 2: Start QEMU with four harts and a PCI xHCI keyboard**

```bash
qemu-system-riscv64 \
  -machine virt -cpu rv64,svpbmt=true,zkr=true -m 2G -smp 4 \
  -no-reboot -display none \
  -kernel target/osdk/aster-kernel/aster-kernel-osdk-bin.qemu_elf \
  -initrd target/qemu-uboot/usb-keyboard-initramfs.cpio.gz \
  -append 'earlycon console=ttyS0 init=/init loglevel=info' \
  -device qemu-xhci,id=xhci -device usb-kbd,bus=xhci.0 \
  -serial file:target/qemu-uboot/usb-keyboard-runtime/serial.log \
  -monitor unix:target/qemu-uboot/usb-keyboard-runtime/monitor.sock,server=on,wait=off \
  -daemonize
```

Expected: the monitor socket appears and serial reaches the RISC-V userspace
marker with exactly one USB keyboard registration.

- [ ] **Step 3: Inject the acceptance sequence and stop QEMU**

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

Expected: QEMU exits after accepting the complete input sequence.

- [ ] **Step 4: Assert registration, input, userspace, and panic contracts**

```bash
python3 - <<'PY'
from pathlib import Path

log = Path("target/qemu-uboot/usb-keyboard-runtime/serial.log").read_bytes()
assert log.count(b"USB boot keyboard registered:") == 1
assert b"Hello from RISC-V userspace" in log
assert b"panic" not in log.lower()
for expected in (b"ab1z", b"A!", b"aaaaaababababab"):
    assert expected in log, expected
print("USB keyboard runtime acceptance: PASS")
PY
```

Expected: `USB keyboard runtime acceptance: PASS`.

- [ ] **Step 5: Run the invalid-DWC3-selector fallback case**

```bash
qemu-system-riscv64 \
  -machine virt,dumpdtb=target/qemu-uboot/usb-keyboard-runtime/invalid-dwc3.dtb
fdtput -p -t s \
  target/qemu-uboot/usb-keyboard-runtime/invalid-dwc3.dtb \
  /chosen asterinas,usb-host /soc/usb@deadbeef
qemu-system-riscv64 \
  -machine virt -cpu rv64,svpbmt=true,zkr=true -m 2G -smp 4 \
  -no-reboot -display none \
  -kernel target/osdk/aster-kernel/aster-kernel-osdk-bin.qemu_elf \
  -initrd target/qemu-uboot/usb-keyboard-initramfs.cpio.gz \
  -dtb target/qemu-uboot/usb-keyboard-runtime/invalid-dwc3.dtb \
  -append 'earlycon console=ttyS0 init=/init loglevel=info' \
  -device qemu-xhci,id=xhci -device usb-kbd,bus=xhci.0 \
  -serial file:target/qemu-uboot/usb-keyboard-runtime/invalid-dwc3-serial.log \
  -monitor unix:target/qemu-uboot/usb-keyboard-runtime/invalid-dwc3-monitor.sock,server=on,wait=off \
  -daemonize
printf 'sendkey a\n' | socat - \
  UNIX-CONNECT:target/qemu-uboot/usb-keyboard-runtime/invalid-dwc3-monitor.sock
sleep 5
printf 'quit\n' | socat - \
  UNIX-CONNECT:target/qemu-uboot/usb-keyboard-runtime/invalid-dwc3-monitor.sock
rg -n 'failed to resolve USB host|Hello from RISC-V userspace' \
  target/qemu-uboot/usb-keyboard-runtime/invalid-dwc3-serial.log
! rg -ni 'panic' \
  target/qemu-uboot/usb-keyboard-runtime/invalid-dwc3-serial.log
```

Expected: the warning and userspace marker are present, PCI keyboard fallback
delivers the key, and no panic is present.

### Task 9: Run final gates, review, and fast-forward local main

**Files:**
- Verify all files changed since `cc0d19383`.

- [ ] **Step 1: Run the complete local host-tool suite**

```bash
PYTHONPATH=tools/riscv python3 -m unittest discover \
  -s tools/riscv/tests -p 'test_*.py' -v
python3 -m unittest discover -s tools/usb-hid/tests -p 'test_*.py' -v
```

Expected: the existing RISC-V discovery suite and all 49 USB oracle tests pass.

- [ ] **Step 2: Run final repository checks**

```bash
git diff --check main..HEAD
cargo fmt --all -- --check
docker run --rm --privileged --network=host -v /dev:/dev \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702 \
  bash -lc 'make check TARGET_ARCH=riscv64'
```

Expected: all checks exit zero. Any failure must be reproduced against local
`main` before it may be classified as inherited.

- [ ] **Step 3: Prove scope and ancestry**

```bash
git merge-base --is-ancestor cc0d19383 HEAD
git diff --name-status cc0d19383..HEAD
git diff --check cc0d19383..HEAD
git status --short --branch
```

Expected: ancestry succeeds, the diff contains only the approved USB/xHCI,
PCI, DMA, input/TTY, test, and integration-document scope, and the worktree is
clean apart from ignored build artifacts.

- [ ] **Step 4: Complete spec and code-quality reviews**

Dispatch a spec-compliance review against
`docs/superpowers/specs/2026-08-21-megrez-usb-core-main-integration-design.md`,
then an Asterinas persona-keyed code review over `cc0d19383..HEAD`. Resolve all
Critical and Important findings and rerun affected tests before proceeding.

- [ ] **Step 5: Fast-forward local main without touching remotes**

```bash
old_main=$(git rev-parse main)
test "$old_main" = cc0d19383468c87eeb2a706b7e32ebd8faacdb22
git merge-base --is-ancestor "$old_main" HEAD
git update-ref refs/heads/main HEAD "$old_main"
git rev-list --left-right --count origin/main...main
```

Expected: local `main` fast-forwards to the reviewed candidate. No remote push
or remote branch deletion occurs.
