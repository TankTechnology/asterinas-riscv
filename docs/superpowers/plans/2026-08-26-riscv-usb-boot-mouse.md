# RISC-V USB Boot Mouse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve the working USB keyboard while adding a USB HID Boot Mouse that drives Xorg through evdev on QEMU PCI xHCI and the Megrez DWC3/xHCI host.

**Architecture:** Replace the keyboard-only OSTD controller owner with one shared, bounded HID host that owns the controller and optional keyboard/mouse sessions. Decode Boot Mouse reports in safe Rust in `aster-usb`, feed both devices from the existing deferred IRQ worker, extend the focused xHCI gate to inject and verify pointer events, then boot the same Sv39/SMP4 kernel on Megrez.

**Tech Stack:** Rust nightly, OSTD/CrabUSB, Asterinas input and evdev, USB HID Boot Protocol, QEMU RISC-V `virt`/PCI xHCI/HMP, C guest probe, Python `unittest`, Megrez U-Boot/HTTP boot.

---

## Scope and file map

The milestone supports one pre-attached Boot Keyboard and one pre-attached Boot
Mouse. It does not claim generic report descriptors, hotplug, reconnect,
wireless/Bluetooth transports, or arbitrary composite HID devices.

| Path | Responsibility |
| --- | --- |
| `ostd/src/bus/usb/report_queue.rs` | Const-generic, fixed-depth interrupt-IN queue with actual-length evidence. |
| `ostd/src/bus/usb.rs` | Single xHCI owner, keyboard/mouse discovery, claim, endpoint ownership, and bounded report delivery. |
| `kernel/comps/usb/src/mouse.rs` | Safe Boot Mouse decoder and evdev capability registration. |
| `kernel/comps/usb/src/lib.rs` | Compile the mouse module. |
| `kernel/comps/usb/src/arch/riscv/mod.rs` | Register and drain keyboard and mouse from one deferred IRQ loop. |
| `tools/riscv/xhci/input_gate_init.c` | Guest proof for one USB keyboard and one USB relative pointer. |
| `tools/riscv/xhci/input_gate.py` | Attach `usb-mouse`, inject HMP movement/buttons, and classify complete evidence. |
| `tools/riscv/tests/test_xhci_input_gate.py` | Host and native regression tests for the combined gate. |
| `tools/riscv/xhci/README.md`, `tools/riscv/README.md` | Exact QEMU and real-board acceptance commands and scope. |

### Task 1: Commit this execution contract

**Files:**
- Create: `docs/superpowers/plans/2026-08-26-riscv-usb-boot-mouse.md`
- Reference: `docs/superpowers/specs/2026-08-26-riscv-usb-boot-mouse-design.md`

- [ ] **Step 1: Verify the plan and approved design are self-consistent**

```bash
git merge-base --is-ancestor b9e3003c941c642dc60def59751511591a1d2440 HEAD
! rg -n 'T[B]D|T[O]DO|F[I]XME|[p]laceholder' \
  docs/superpowers/specs/2026-08-26-riscv-usb-boot-mouse-design.md \
  docs/superpowers/plans/2026-08-26-riscv-usb-boot-mouse.md
git diff --check
```

Expected: all commands exit zero.

- [ ] **Step 2: Commit only the plan**

```bash
git add docs/superpowers/plans/2026-08-26-riscv-usb-boot-mouse.md
git diff --cached --check
git commit -m "docs(riscv): plan USB boot mouse support"
```

Expected: one Markdown file is committed and the worktree is clean.

### Task 2: Decode and register a USB Boot Mouse in safe Rust

**Files:**
- Create: `kernel/comps/usb/src/mouse.rs`
- Modify: `kernel/comps/usb/src/lib.rs`

- [ ] **Step 1: Add RED kernel tests for report semantics**

Add `#[cfg(ktest)]` cases that instantiate `HidBootMouse::new()` and require:

```rust
assert_eq!(
    mouse.decode([0b001, 5, (-3_i8) as u8, 0], 3),
    vec![
        InputEvent::from_key_and_status(KeyCode::BtnLeft, KeyStatus::Pressed),
        InputEvent::from_relative_move(RelCode::X, 5),
        InputEvent::from_relative_move(RelCode::Y, -3),
        InputEvent::from_sync_event(SynEvent::Report),
    ],
);
assert_eq!(
    mouse.decode([0, 0, 0, 0], 3),
    vec![
        InputEvent::from_key_and_status(KeyCode::BtnLeft, KeyStatus::Released),
        InputEvent::from_sync_event(SynEvent::Report),
    ],
);
```

Also require independent right/middle transitions, no duplicate button events,
signed X/Y limits, an optional signed wheel byte for length four, rejection of
lengths outside `3..=4`, and exact `BUS_USB` KEY/REL/SYN capabilities from the
registered device description.

- [ ] **Step 2: Compile the focused RED tests**

```bash
docker run --rm --network=host -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  cargo osdk check --ktests -p aster-usb --target riscv64imac-unknown-none-elf
```

Expected: compilation fails because `mouse`/`HidBootMouse` do not exist.

- [ ] **Step 3: Implement the minimal decoder and device registration**

Use this private interface:

```rust
pub(super) struct HidBootMouse {
    previous_buttons: u8,
}

impl HidBootMouse {
    pub(super) const fn new() -> Self;
    pub(super) fn decode(
        &mut self,
        report: [u8; 4],
        actual_length: usize,
    ) -> Result<Vec<InputEvent>, MouseReportError>;
}

pub(super) fn register(
    vendor_id: u16,
    product_id: u16,
) -> RegisteredInputDevice;
```

The device name is exactly `usb_boot_mouse`, its physical path is
`usb/input0`, and its ID uses `InputId::BUS_USB`. Report byte zero contains the
three buttons; bytes one and two are signed relative X/Y; byte three is an
optional signed wheel value. Emit `SYN_REPORT` only when another event exists.

- [ ] **Step 4: Run GREEN and commit**

```bash
docker run --rm --network=host -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  cargo osdk check --ktests -p aster-usb --target riscv64imac-unknown-none-elf
cargo fmt --all -- --check
git diff --check
git add kernel/comps/usb/src/mouse.rs kernel/comps/usb/src/lib.rs
git commit -m "feat(usb): decode HID boot mouse reports"
```

Expected: the new mouse tests compile and the commit contains only the safe
decoder/registration slice.

### Task 3: Share one xHCI lifecycle between keyboard and mouse

**Files:**
- Modify: `ostd/src/bus/usb/report_queue.rs`
- Modify: `ostd/src/bus/usb.rs`
- Modify: `kernel/comps/usb/src/arch/riscv/mod.rs`

- [ ] **Step 1: Add RED OSTD tests for descriptor and queue contracts**

Extend existing ktests to require:

```rust
assert_eq!(classify_boot_interface(0x03, 0x01, 0x01), Some(BootHidKind::Keyboard));
assert_eq!(classify_boot_interface(0x03, 0x01, 0x02), Some(BootHidKind::Mouse));
assert_eq!(classify_boot_interface(0x03, 0x00, 0x02), None);

let completed = queue.poll(&mut endpoint, &mut context).unwrap().unwrap();
assert_eq!(completed.actual_length(), 3);
assert_eq!(completed.bytes()[..3], [1, 4, 0]);
```

Require one interrupt-IN endpoint, mouse maximum packet size at least three,
duplicate keyboard/mouse rejection, FIFO replenishment for both 8-byte and
4-byte queues, preserved keyboard exact-length behavior, and propagation of
short/oversized mouse evidence to the caller without decoding it.

- [ ] **Step 2: Run the focused RED compile**

```bash
docker run --rm --network=host -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  cargo osdk check --ktests -p ostd -p aster-usb \
  --target riscv64imac-unknown-none-elf
```

Expected: compilation fails on the missing generic queue/shared-host API.

- [ ] **Step 3: Generalize the bounded report queue**

Replace the keyboard-specific queue with:

```rust
pub(super) struct CompletedReport<const N: usize> {
    bytes: [u8; N],
    actual_length: usize,
}

pub(super) struct BootReportQueue<const N: usize> {
    slots: [ReportSlot<N>; REPORT_QUEUE_DEPTH],
    next_completion: usize,
}
```

The queue continues to pin eight heap buffers, preserves FIFO order, rejects a
completion larger than `N`, replenishes before delivery, and returns the actual
length. The keyboard caller requires eight; the mouse caller requires three or
four.

- [ ] **Step 4: Implement the shared OSTD host**

Expose one public owner and report enum:

```rust
pub struct PollingUsbHidHost { /* one xHCI host and optional sessions */ }

pub enum UsbHidReport {
    Keyboard([u8; 8]),
    Mouse { bytes: [u8; 4], actual_length: usize },
}

pub struct UsbHidInfo {
    pub keyboard: Option<UsbDeviceInfo>,
    pub mouse: Option<UsbDeviceInfo>,
}
```

`PollingUsbHidHost::open` performs the existing bounded xHCI initialization,
discovers at most one interface for each boot protocol, opens and claims both
devices, sends `SET_PROTOCOL(BOOT)`, fills both queues, and succeeds when a
keyboard exists even if no mouse is present at the discovery deadline.
`poll_report` pumps the event handler once and returns at most one ready report
per call; repeated calls drain both queues. `enable_irq` and `disable_irq`
remain controller-wide. Retain/leak DMA-visible failed objects under the same
fail-safe policy as the current keyboard implementation.

- [ ] **Step 5: Connect both devices to the deferred RISC-V worker**

Replace `DeferredKeyboardState` with state containing optional keyboard and
mouse decoder/registration pairs. Match `UsbHidReport` and submit to the
corresponding registered device. Stable logs are:

```text
USB boot keyboard registered: VVVV:PPPP bus=usb name=usb_boot_keyboard
USB boot mouse registered: VVVV:PPPP bus=usb name=usb_boot_mouse
USB HID interrupt-driven loop started
```

Preserve PCI-before-DWC3 selection, one IRQ mapping, the current enable/disable
guard, keyboard-only operation, and the existing keyboard event tests.

- [ ] **Step 6: Run GREEN and commit**

```bash
docker run --rm --network=host -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  cargo osdk check --ktests -p ostd -p aster-usb \
  --target riscv64imac-unknown-none-elf
cargo fmt --all -- --check
git diff --check
git add ostd/src/bus/usb.rs ostd/src/bus/usb/report_queue.rs \
  kernel/comps/usb/src/arch/riscv/mod.rs
git commit -m "feat(riscv): share xHCI keyboard and mouse host"
```

Expected: existing keyboard/IRQ/queue tests and new shared-host tests compile.

### Task 4: Extend the deterministic QEMU xHCI gate

**Files:**
- Modify: `tools/riscv/xhci/input_gate_init.c`
- Modify: `tools/riscv/xhci/input_gate.py`
- Modify: `tools/riscv/tests/test_xhci_input_gate.py`
- Modify: `tools/riscv/xhci/README.md`
- Modify: `tools/riscv/README.md`

- [ ] **Step 1: Write host/native RED tests**

Require QEMU argv order `qemu-xhci` before one `usb-kbd` and one `usb-mouse`,
with no VirtIO/i8042/tablet fallback. Extend the native probe cases so the
valid topology contains exactly one BUS_USB keyboard named
`usb_boot_keyboard` and one BUS_USB relative pointer named `usb_boot_mouse`.
Require this normalized pointer sequence:

```text
XHCI_POINTER_EVENT type=2 code=0 value=17
XHCI_POINTER_EVENT type=2 code=1 value=-9
XHCI_POINTER_EVENT type=0 code=0 value=0
XHCI_POINTER_EVENT type=1 code=272 value=1
XHCI_POINTER_EVENT type=0 code=0 value=0
XHCI_POINTER_EVENT type=1 code=272 value=0
XHCI_POINTER_EVENT type=0 code=0 value=0
```

Add HMP tests for bounded `mouse_move 17 -9`, `mouse_button 1`, and
`mouse_button 0`. Require keyboard PASS and pointer PASS markers once, in
order, and reject missing release, wrong bustype/name, duplicate pointers,
partial reads, fatal serial markers, and incomplete cleanup.

- [ ] **Step 2: Run the focused RED suite**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_xhci_input_gate -v
```

Expected: failures identify missing USB mouse argv/protocol behavior.

- [ ] **Step 3: Implement the combined guest and HMP protocol**

The guest scans `/dev/input/event0..31`, classifies capabilities with
`EVIOCGBIT`, and keeps separate keyboard/pointer fds. It prints READY only
after both exact BUS_USB/name identities exist, then validates complete
`struct input_event` records until both state machines pass. After printing a
single combined PASS marker it flushes and holds as PID 1.

The host attaches `usb-mouse,id=usb-mouse,bus=xhci.0`, sends the existing key
commands followed by the three registered pointer commands, maintains total
HMP deadlines/byte caps, and classifies the fully drained transcript.

- [ ] **Step 4: Run unit GREEN and commit**

```bash
make test_riscv_xhci_input_unit
python3 -m py_compile tools/riscv/xhci/input_gate.py \
  tools/riscv/tests/test_xhci_input_gate.py
ruff check tools/riscv/xhci/input_gate.py tools/riscv/tests/test_xhci_input_gate.py
ruff format --check tools/riscv/xhci/input_gate.py tools/riscv/tests/test_xhci_input_gate.py
git diff --check
git add tools/riscv/xhci tools/riscv/tests/test_xhci_input_gate.py tools/riscv/README.md
git commit -m "test(riscv): gate PCI xHCI keyboard and mouse"
```

Expected: the focused host/native suite passes without QEMU.

- [ ] **Step 5: Run exactly one QEMU runtime gate**

Inside the pinned development container, build the updated gate initramfs and
Sv39/SMP4 kernel, prepare the private U-Boot disk, and run:

```bash
timeout 180s python3 tools/riscv/xhci/input_gate.py \
  --uboot target/qemu-uboot/cache/u-boot-build/u-boot \
  --boot-disk target/qemu-uboot/xhci-input/boot.ext4 \
  --manifest target/qemu-uboot/xhci-input/artifacts.json \
  --serial-log target/riscv-xhci-input/serial.log \
  --result target/riscv-xhci-input/result.json \
  --smp 4
```

Expected: exit zero; `result.json` has `passed: true`, both USB identities,
the exact keyboard and pointer sequences, `smp: 4`, and complete cleanup.

### Task 5: Validate the same pointer path on Megrez

**Files:**
- Modify only if evidence requires correction: `tools/riscv/debian/rootfs/desktop_m3_evidence.sh`
- Create: `docs/porting/evidence/2026-08-26-megrez-usb-mouse.md`

- [ ] **Step 1: Build the normal board kernel once**

```bash
make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
sha256sum target/osdk/aster-kernel/aster-kernel-osdk-bin.Image
```

Expected: non-empty current-HEAD Sv39 Image and recorded SHA-256.

- [ ] **Step 2: Boot through the existing Megrez U-Boot/HTTP workflow**

Use the already validated desktop root/initramfs/DTB, keep both USB devices
attached before reset, and use bootargs with `console=ttyS0`, the Debian stage1
entrypoint, and a bounded `asterinas.reboot_after` recovery value. Capture the
complete serial epoch after `Starting kernel ...`.

Expected serial evidence, once each:

```text
USB boot keyboard registered:
USB boot mouse registered:
USB HID interrupt-driven loop started
```

Also require `/dev/input/event0` and `/dev/input/event1`, Xorg adding both
`Asterinas keyboard` and `Asterinas pointer`, no panic/oops, and no xHCI
transfer-stop marker.

- [ ] **Step 3: Perform the HDMI acceptance check**

Move the pointer, left-click the xterm window, type a short line with the USB
keyboard, and verify pointer/keyboard remain responsive together. Record the
observed device IDs, event-node mapping, kernel hash, DTB hash, rootfs hash,
serial log hash, and which actions passed in the evidence Markdown file.

- [ ] **Step 4: Run final focused verification and commit evidence**

```bash
make test_riscv_xhci_input_unit
docker run --rm --network=host -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  cargo osdk check --ktests -p ostd -p aster-usb \
  --target riscv64imac-unknown-none-elf
cargo fmt --all -- --check
git diff --check
git status --short
```

Expected: focused software gates pass, the evidence accurately distinguishes
QEMU from physical-board claims, and only the intended evidence file remains
to commit.
