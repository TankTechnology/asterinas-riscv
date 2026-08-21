# RISC-V D3 IRQ-Safe DW APB UART Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:executing-plans` to implement this plan task-by-task. Do not
> commit, rebase, push, or create a PR before the user reviews the resulting
> diff and test evidence.

**Goal:** Provide firmware-preserving DW APB UART transmit from task, IRQ, and
other atomic contexts without changing the generic logger architecture or
silently discarding console output.

**Architecture:** Remove the sleeping console prerequisite from the final D3
shape and serialize DW APB transmit with an IRQ-disabling spinlock, matching the
existing NS16550A path. Use a three-state atomic failure protocol to release
the transmitter before SBI stack-trace reporting. Resolve the selected
`stdout-path` to an absolute path and fail closed unless every ancestor bus is
identity mapped by an empty `ranges` property.

**Tech Stack:** Rust 2024, Asterinas `aster-uart`, `fdt` 0.1.5, OSTD
`SpinLock`/`IoMem`, RISC-V MMIO fences, OSDK kernel tests, QEMU, Megrez.

---

### Task 1: Replace the atomic-output expectation with the required behavior

**Files:**

- Modify tests: `kernel/comps/uart/src/arch/riscv/dw_apb.rs`

- [ ] **Step 1: Replace the silent-drop tests**

Remove these tests:

```rust
dw_apb_console_does_not_touch_mmio_when_transmit_is_owned
dw_apb_console_recognizes_preemption_disabled_as_atomic_mode
dw_apb_console_does_not_wait_for_an_owner_in_atomic_mode
dw_apb_console_does_not_block_abort_fallback_after_failure
dw_apb_console_transmits_only_from_task_context
```

Add:

```rust
#[ktest]
fn dw_apb_console_transmits_with_preemption_disabled() {
    let access = ScriptedAccess::new([Ok(LSR_THRE)]);
    let observation = access.clone();
    let uart = DwApbUart::new(access);
    let _preempt_guard = ostd::task::disable_preempt();

    assert_eq!(uart.try_send(b"A"), Ok(()));
    assert_eq!(
        observation.writes(),
        vec![(
            TRANSMIT_HOLDING_OFFSET_BYTES,
            u32::from(b'A'),
        )]
    );
}
```

- [ ] **Step 2: Run the focused UART suite and observe RED**

Run:

```bash
docker exec codex-riscv-d3 bash -lc '
  cd /root/asterinas/kernel/comps/uart &&
  cargo osdk test --target-arch riscv64 --scheme riscv
'
```

Expected: `dw_apb_console_transmits_with_preemption_disabled` fails with
`Err(TxError::AtomicContext)` and performs no write.

### Task 2: Replace only sleeping ownership with IRQ-safe spin ownership

**Files:**

- Modify: `kernel/comps/uart/src/arch/riscv/dw_apb.rs`

- [ ] **Step 1: Change the synchronization imports and lock type**

Use:

```rust
use core::{
    hint,
    ops::Range,
    sync::atomic::{AtomicBool, Ordering},
    time::Duration,
};

use ostd::{
    arch::{self, device::io_mem},
    io::IoMem,
    irq,
    mm::VmIoOnce,
    sync::{LocalIrqDisabled, SpinLock, SpinLockGuard, WaitQueue},
};
```

Remove `InterruptLevel`, `Mutex`, `MutexGuard`, and `task::atomic_mode`.
Temporarily retain `is_failed`, `failure_reported`, and `failure_waiters`; Task
3 replaces them after first proving the atomic transmit path.

Remove only `TxError::AtomicContext` and change ownership to:

```rust
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum TxError {
    Disabled,
    Mmio,
    TimedOut,
}

struct DwApbUart<A> {
    access: A,
    tx_lock: SpinLock<(), LocalIrqDisabled>,
    is_failed: AtomicBool,
    failure_reported: AtomicBool,
    failure_waiters: WaitQueue,
}

struct TxOwnerGuard<'a, A: DwApbAccess> {
    uart: &'a DwApbUart<A>,
    _lock: SpinLockGuard<'a, (), LocalIrqDisabled>,
}
```

Initialize the lock with:

```rust
tx_lock: SpinLock::new(()),
```

- [ ] **Step 2: Remove the atomic-context rejection**

The beginning of `try_send_with_timeout` becomes:

```rust
fn try_send_with_timeout(&self, buf: &[u8], timeout: Duration) -> Result<(), TxError> {
    if self.is_failed.load(Ordering::Acquire) {
        return Err(TxError::Disabled);
    }

    let _owner = self.claim_tx()?;
    // Keep the existing byte loop unchanged in this task.
```

Change `claim_tx` and `finish_claim` to carry `SpinLockGuard` rather than
`MutexGuard`. Keep the existing acquire-side I/O fence and the second
`is_failed` check after locking. Keep `send_byte`'s existing failure update;
Task 3 replaces the Boolean/waitqueue protocol only after the spin-owned
transmit path is green.

- [ ] **Step 3: Keep release-side I/O ordering**

Retain:

```rust
impl<A: DwApbAccess> Drop for TxOwnerGuard<'_, A> {
    fn drop(&mut self) {
        self.uart.access.fence();
    }
}
```

Because `TxOwnerGuard::drop` runs before its `SpinLockGuard` field is dropped,
the MMIO fence completes before ownership is published to another hart.

- [ ] **Step 4: Run the focused UART suite and observe GREEN**

Run Task 1 Step 2 again.

Expected: the new preemption-disabled transmit test passes. The existing
Boolean/waitqueue failure tests still pass unchanged.

### Task 3: Prove single-owner failure reporting

**Files:**

- Modify tests and implementation:
  `kernel/comps/uart/src/arch/riscv/dw_apb.rs`

- [ ] **Step 1: Add the failing state-transition test**

Add:

```rust
#[ktest]
fn dw_apb_failure_elects_one_reporter_and_blocks_later_mmio() {
    let access = ScriptedAccess::new([Err(())]);
    let observation = access.clone();
    let uart = DwApbUart::new(access);

    assert_eq!(uart.try_send(b"A"), Err(TxError::Mmio));
    assert_eq!(
        uart.failure_state.load(Ordering::Acquire),
        TX_FAILURE_REPORTING
    );
    assert_eq!(uart.try_send(b"B"), Err(TxError::Failed));
    assert_eq!(
        observation.operations(),
        vec![
            Operation::Fence,
            Operation::Read32(LINE_STATUS_OFFSET_BYTES),
            Operation::Fence,
        ]
    );

    uart.mark_failure_reported();
    assert_eq!(
        uart.failure_state.load(Ordering::Acquire),
        TX_FAILURE_REPORTED
    );
}
```

- [ ] **Step 2: Run the focused suite and observe RED**

Expected: compilation fails because `failure_state`, the three state constants,
`TxError::Failed`, and the adapted reporting methods do not exist yet.

- [ ] **Step 3: Replace the Boolean/waitqueue protocol**

Change the atomic import to:

```rust
use core::sync::atomic::{AtomicU8, Ordering};
```

Remove `WaitQueue`, `is_failed`, `failure_reported`, and `failure_waiters`.
Define:

```rust
const TX_HEALTHY: u8 = 0;
const TX_FAILURE_REPORTING: u8 = 1;
const TX_FAILURE_REPORTED: u8 = 2;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum TxError {
    Failed,
    Mmio,
    TimedOut,
}

struct DwApbUart<A> {
    access: A,
    tx_lock: SpinLock<(), LocalIrqDisabled>,
    failure_state: AtomicU8,
}
```

Initialize `failure_state` to `TX_HEALTHY`. At the beginning of
`try_send_with_timeout` and again after acquiring `tx_lock`, return
`TxError::Failed` when the state is not healthy. When the owning sender sees
an MMIO error or timeout, publish `TX_FAILURE_REPORTING` with `Release` before
returning the error. Remove the old `is_failed` stores from `send_byte`.

- [ ] **Step 4: Implement reporting publication and secondary waiting**

Use:

```rust
fn mark_failure_reported(&self) {
    self.failure_state
        .store(TX_FAILURE_REPORTED, Ordering::Release);
}

fn wait_for_failure_report(&self) {
    while self.failure_state.load(Ordering::Acquire) == TX_FAILURE_REPORTING {
        hint::spin_loop();
    }
}
```

In `Uart::send`:

```rust
match self.try_send(buf) {
    Ok(()) => {}
    Err(TxError::Mmio) => {
        let _irq_guard = irq::disable_local();
        ostd::early_println!("fatal DW APB UART MMIO failure");
        ostd::panic::print_stack_trace();
        self.mark_failure_reported();
        ostd::panic::abort();
    }
    Err(TxError::TimedOut) => {
        let _irq_guard = irq::disable_local();
        // The SBI console may poll the same stuck UART indefinitely.
        self.mark_failure_reported();
        ostd::panic::abort();
    }
    Err(TxError::Failed) => {
        self.wait_for_failure_report();
        let _irq_guard = irq::disable_local();
        ostd::panic::abort();
    }
}
```

The `try_send` call returns only after its owner guard has been dropped, so the
fatal reporting path holds no UART spinlock. For a readiness timeout, do not
use the SBI early console because it may poll the same stuck UART forever.

- [ ] **Step 5: Run the focused suite and observe GREEN**

Adapt the existing ownership-release and timeout-disable tests to inspect
`failure_state` and expect `TxError::Failed` from later sends.

Expected: all ownership, timeout, MMIO, and failure-state tests pass without
performing MMIO after `TX_FAILURE_REPORTING`.

### Task 4: Fail closed on non-identity ancestor buses

**Files:**

- Modify tests and implementation:
  `kernel/comps/uart/src/arch/riscv/mod.rs`

- [ ] **Step 1: Add failing pure path tests**

Add:

```rust
#[ktest]
fn stdout_selector_resolves_alias_to_an_absolute_path() {
    assert_eq!(
        resolve_selected_path("serial0", |alias| {
            (alias == "serial0").then_some("/soc/serial@50900000")
        }),
        Some("/soc/serial@50900000")
    );
    assert_eq!(
        resolve_selected_path("/soc/serial@50900000", |_| None),
        Some("/soc/serial@50900000")
    );
}

#[ktest]
fn uart_selection_accepts_only_identity_mapped_ancestors() {
    assert!(ancestor_buses_are_identity_mapped(
        "/soc/serial@50900000",
        |path| (path == "/soc").then_some(&[][..]),
    ));
    assert!(!ancestor_buses_are_identity_mapped(
        "/soc/serial@50900000",
        |_| None,
    ));
    assert!(!ancestor_buses_are_identity_mapped(
        "/soc/serial@50900000",
        |path| (path == "/soc").then_some(&[0, 0, 0, 1][..]),
    ));
}
```

- [ ] **Step 2: Run the focused suite and observe RED**

Expected: compilation fails because `resolve_selected_path` and
`ancestor_buses_are_identity_mapped` do not exist.

- [ ] **Step 3: Add the allocation-free helpers**

Add:

```rust
fn resolve_selected_path<'a>(
    selector: &'a str,
    resolve_alias: impl FnOnce(&str) -> Option<&'a str>,
) -> Option<&'a str> {
    if selector.starts_with('/') {
        Some(selector)
    } else {
        resolve_alias(selector)
    }
}

fn ancestor_buses_are_identity_mapped<'a>(
    selected_path: &str,
    mut ranges_of: impl FnMut(&str) -> Option<&'a [u8]>,
) -> bool {
    let mut child_path = selected_path;

    loop {
        let Some((parent_path, child_name)) = child_path.rsplit_once('/') else {
            return false;
        };
        if child_name.is_empty() {
            return false;
        }
        if parent_path.is_empty() {
            return true;
        }
        if !ranges_of(parent_path).is_some_and(<[u8]>::is_empty) {
            return false;
        }
        child_path = parent_path;
    }
}
```

- [ ] **Step 4: Apply the helpers to selected `stdout-path` nodes**

In the selected-node resolver closure passed by `init()`:

```rust
let Some(selected_path) = resolve_selected_path(selector, |alias| {
    device_tree.aliases().and_then(|aliases| aliases.resolve(alias))
}) else {
    ostd::info!("failed to resolve UART alias from 'stdout-path'");
    return None;
};
if !ancestor_buses_are_identity_mapped(selected_path, |parent_path| {
    device_tree
        .find_node(parent_path)
        .and_then(|node| node.property("ranges"))
        .map(|property| property.value)
}) {
    ostd::info!("selected UART is not on identity-mapped buses");
    return None;
}
device_tree.find_node(selected_path)
```

Keep the absent-`stdout-path` NS16550A fallback unchanged. Do not apply the
selected-node ancestor contract to that legacy fallback in this PR.

- [ ] **Step 5: Run the focused suite and observe GREEN**

Expected: alias, absolute-path, identity-range, and existing selector tests all
pass.

### Task 5: Tighten the DW APB configuration type

**Files:**

- Modify tests and implementation:
  `kernel/comps/uart/src/arch/riscv/dw_apb.rs`

- [ ] **Step 1: Change test calls to concrete layout values**

Replace calls such as:

```rust
DwApbConfig::validate(None, Some(2), Some(4), 0x1000, 0x18)
```

with:

```rust
DwApbConfig::validate(None, 2, 4, 0x1000, 0x18)
```

Remove `dw_apb_rejects_missing_access_layout`; missing properties remain
covered by `DwApbConfig::from_node`.

- [ ] **Step 2: Run the focused suite and observe RED**

Expected: compilation fails because `validate` still expects `Option<usize>`.

- [ ] **Step 3: Tighten `validate`**

Use:

```rust
fn validate(
    status: Option<&str>,
    reg_shift: usize,
    reg_io_width: usize,
    reg_base: usize,
    reg_size_bytes: usize,
) -> Result<Self, DwApbConfigError> {
    if !matches!(status, None | Some("ok" | "okay")) {
        return Err(DwApbConfigError::Disabled);
    }
    if reg_shift != REGISTER_SHIFT {
        return Err(DwApbConfigError::UnsupportedRegShift);
    }
    if reg_io_width != REGISTER_IO_WIDTH_BYTES {
        return Err(DwApbConfigError::UnsupportedRegIoWidth);
    }
    // Keep the existing size and checked-end validation unchanged.
```

Pass the concrete parsed values from `from_node`.

- [ ] **Step 4: Run the focused suite and observe GREEN**

Expected: all configuration tests pass.

### Task 6: Remove the sleeping console prerequisite from the net diff

**Files:**

- Restore to base: `kernel/comps/console/src/lib.rs`
- Restore to base: `kernel/comps/logger/src/console.rs`
- Restore to base: `ostd/src/task/atomic_mode.rs`

- [ ] **Step 1: Restore the three files to their `8a9543109` contents**

Apply the inverse of commit `a3bd9731d` using an explicit patch. Do not use
`git checkout`, `git reset`, or a new revert commit.

Expected:

- `aster-console` again exposes `all_devices_lock`;
- the logger again holds its IRQ-disabling registry guard for the complete
  formatted record; and
- `atomic_mode::is_atomic` is no longer public because D3 no longer needs it.

- [ ] **Step 2: Run compile checks for affected crates**

Run:

```bash
docker exec codex-riscv-d3 bash -lc '
  cd /root/asterinas &&
  OSDK_TARGET_ARCH=riscv64 cargo osdk check -p aster-console &&
  OSDK_TARGET_ARCH=riscv64 cargo osdk check -p aster-logger &&
  OSDK_TARGET_ARCH=riscv64 cargo osdk check -p aster-uart &&
  OSDK_TARGET_ARCH=riscv64 cargo osdk check --ktests -p aster-uart
'
```

Expected: every command exits zero.

- [ ] **Step 3: Inspect scope**

Run:

```bash
git diff --stat
git diff --check
git diff -- \
  kernel/comps/console/src/lib.rs \
  kernel/comps/logger/src/console.rs \
  ostd/src/task/atomic_mode.rs \
  kernel/comps/uart/src/arch/riscv \
  ostd/src/arch/riscv/device
```

Expected: the first three files are exact inverse changes relative to the
current branch, while the final reconstructed PR will omit them entirely.

### Task 7: Run the D3 host verification matrix

**Files:**

- No additional source files
- Evidence: `/tmp/riscv-d3-qemu-smp4.log`

- [ ] **Step 1: Format and run focused tests**

Run:

```bash
docker exec codex-riscv-d3 bash -lc '
  cd /root/asterinas &&
  cargo fmt --all &&
  cd kernel/comps/uart &&
  cargo osdk test --target-arch riscv64 --scheme riscv
'
```

Expected: the complete `aster-uart` kernel-test set passes.

- [ ] **Step 2: Run repository quality checks and cross-architecture builds**

Run:

```bash
docker exec codex-riscv-d3 bash -lc '
  cd /root/asterinas &&
  cargo fmt --all -- --check &&
  git diff --check &&
  make check &&
  TARGET_ARCH=riscv64 make kernel &&
  TARGET_ARCH=loongarch64 make kernel
'
```

Expected: every command exits zero.

- [ ] **Step 3: Run the four-hart QEMU fallback boot**

Run:

```bash
docker exec codex-riscv-d3 bash -lc '
  cd /root/asterinas
  set +e
  timeout 180s make run_kernel TARGET_ARCH=riscv64 SMP=4 \
    > /tmp/riscv-d3-qemu-smp4.log 2>&1
  run_status=$?
  set -e
  test "$run_status" -eq 0 -o "$run_status" -eq 124
  grep -q "Successfully booted." /tmp/riscv-d3-qemu-smp4.log
'
docker cp codex-riscv-d3:/tmp/riscv-d3-qemu-smp4.log \
  /tmp/riscv-d3-qemu-smp4.log
```

Expected: QEMU retains the legacy NS16550A fallback, starts four harts, and
reaches `Successfully booted.`.

### Task 8: Run the current-source Megrez gate

**Files:**

- Reuse only: `tools/riscv/paced_serial_bridge.py`
- Follow: `tools/riscv/README.md`
- Evidence: a new uniquely named directory under `target/megrez-board-runs/`

- [ ] **Step 1: Freeze and identify the uncommitted candidate**

Record:

- `git diff` SHA-256;
- Image and initramfs size/SHA-256;
- the live RockOS DTB `stdout-path`, alias, `ranges`, compatible,
  `reg-shift`, and `reg-io-width`; and
- the exact volatile bootargs.

Do not overwrite an existing `/boot` artifact, edit `extlinux.conf`, call
`saveenv`, or modify the persistent DTB.

- [ ] **Step 2: Reuse the controlled RAM-only boot procedure**

Follow the command sequence in `tools/riscv/README.md` using
`tools/riscv/paced_serial_bridge.py`, one uniquely named candidate, and:

```text
cpu_no_boost_1_6ghz loglevel=info init=/init \
asterinas.first_process_diag=1 asterinas.reboot_after=400
```

Issue exactly one:

```text
booti 0x80200000 0x83000000:${initrd_size} 0xf0000000
```

- [ ] **Step 3: Require the acceptance markers**

The candidate epoch must contain exactly one DW APB registration message and
one deterministic userspace write marker, with no panic, exception, MMIO
failure, timeout, empty-console diagnostic, or unexpected serial owner.

Allow the armed reboot to return the board to a fresh U-Boot prompt. Stop the
serial bridge and any scoped transfer server afterward.

### Task 9: Review checkpoint

**Files:**

- Review the complete uncommitted D3 diff

- [ ] **Step 1: Run Asterinas persona review**

Review through maintainability, development, security, and hardware personas.
Verify every finding against the code, RISC-V hardware contract, and
Devicetree rules.

- [ ] **Step 2: Present evidence to the user**

Present:

- final file and line counts;
- the inverse `a3bd9731d` changes;
- focused test names/counts;
- `make check` and cross-architecture results;
- QEMU SMP=4 markers;
- current-source Megrez evidence; and
- all remaining findings.

Do not commit, rebase, push, or create a PR. Wait for explicit user approval.
