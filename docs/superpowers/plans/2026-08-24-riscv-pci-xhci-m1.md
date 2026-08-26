# RISC-V PCI xHCI M1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On the M0-converged branch, make QEMU RISC-V `virt` with `smp=4`, `qemu-xhci`, and one `usb-kbd` enumerate an ordinary USB Boot Keyboard and deliver a deterministic press/release sequence through the Asterinas evdev ABI, with no VirtIO or i8042 keyboard fallback.

**Architecture:** Keep current main's xHCI, DMA, PCI BAR, PLIC deferral, input, and evdev implementations authoritative. Add a small RISC-V PCI-platform contract that accepts only coherent identity DMA and an INTx route proven exclusive among currently present PCI functions; add a USB-side PCI adapter that consumes those validated resources and invokes the existing xHCI keyboard worker. Validate the result with a static RISC-V guest probe and a bounded host gate that injects keys through QEMU HMP and requires `BUS_USB`, the expected device name, exact evdev events, complete process-group cleanup, and no panic.

**Tech Stack:** Rust nightly, Asterinas `aster-pci`/`aster-usb`/OSTD/input/evdev, Device Tree PCI `interrupt-map`, QEMU RISC-V `virt`, xHCI, USB HID Boot Protocol, C static initramfs probe, Python `unittest`, HMP over Unix sockets, Docker.

---

## Scope and frozen baseline

```text
M0_HEAD        074c0c42c26df303a529c1a5586b20593e68bac6
TOPIC_DESIGN   4cc608b070923ac4a10b9c30ed937f0b9e35d188
WORKTREE       /home/ubuntu/.config/superpowers/worktrees/asterinas/megrez-usb-keyboard-convergence
BRANCH         codex/megrez-usb-keyboard-main-convergence
```

M1 includes one controller, one ordinary Boot Protocol keyboard, initial enumeration,
interrupt-driven reports, evdev registration, and basic press/release delivery. It does
not include hotplug, generic HID descriptors, hubs, LED output, repeat policy, TTY/Xorg
acceptance, or physical Megrez evidence. Those remain M2-M5 in the approved design.

The only runtime gate in this plan that may take minutes is the final new xHCI QEMU
run. Do not repeat the already-passing 291-test OSTD QEMU suite because M1 does not
modify OSTD. Remote CI is not monitored.

## File map

| Path | Responsibility |
|---|---|
| `kernel/comps/pci/src/arch/riscv/intx.rs` | Parse the PCI interrupt map, resolve one BDF/pin, and prove the route is exclusive among present functions. |
| `kernel/comps/pci/src/arch/riscv/mod.rs` | Retain ECAM/BAR allocation and expose the validated RISC-V DMA/INTx resource contract. |
| `kernel/comps/pci/src/lib.rs` | Export only the target-gated resource API needed by RISC-V PCI drivers. |
| `kernel/comps/usb/src/arch/riscv/pci.rs` | Match PCI xHCI, acquire BAR0, disable INTx during setup, and retain one validated host. |
| `kernel/comps/usb/src/arch/riscv/mod.rs` | Select PCI before DWC3 and run both through one current-main keyboard lifecycle. |
| `kernel/comps/usb/Cargo.toml`, `Cargo.lock` | Add the target-only `aster-pci` dependency. |
| `tools/riscv/xhci/input_gate_init.c` | Identify the sole USB evdev keyboard and verify exact key events in the guest. |
| `tools/riscv/xhci/build_input_gate.sh` | Reproducibly build the static RISC-V initramfs. |
| `tools/riscv/xhci/input_gate.py` | Stage artifacts, launch bounded QEMU/U-Boot, inject HMP keys, classify evidence, and clean up. |
| `tools/riscv/tests/test_xhci_input_gate.py` | Host unit tests for argv, HMP, evidence, timeouts, and cleanup. |
| `Makefile`, `tools/riscv/README.md`, `tools/riscv/xhci/README.md` | Focused local entry points and truthful M1 instructions. |

### Task 1: Commit this M1 execution contract

**Files:**
- Create: `docs/superpowers/plans/2026-08-24-riscv-pci-xhci-m1.md`
- Reference: `docs/superpowers/specs/2026-08-24-riscv-xhci-usb-keyboard-convergence-design.md`

- [ ] **Step 1: Check the plan against the approved design and M0**

```bash
git rev-parse HEAD
git merge-base --is-ancestor 4cc608b070923ac4a10b9c30ed937f0b9e35d188 HEAD
git diff --check
! rg -n 'T[B]D|T[O]DO|F[I]XME' \
  docs/superpowers/plans/2026-08-24-riscv-pci-xhci-m1.md
```

Expected: `HEAD` is the M0 head, the approved design is an ancestor, and all checks pass.

- [ ] **Step 2: Commit only the plan**

```bash
git add docs/superpowers/plans/2026-08-24-riscv-pci-xhci-m1.md
git diff --cached --check
git commit -m "docs(riscv): plan PCI xHCI keyboard M1"
```

Expected: one Markdown file is committed and the worktree is clean.

### Task 2: Define the fail-closed RISC-V PCI resource contract

**Files:**
- Create: `kernel/comps/pci/src/arch/riscv/intx.rs`
- Modify: `kernel/comps/pci/src/arch/riscv/mod.rs`
- Modify: `kernel/comps/pci/src/lib.rs`

- [ ] **Step 1: Add RED kernel tests for QEMU's map and unsafe topologies**

Add pure parser tests under `#[cfg(ktest)]` using the actual QEMU `virt` shape:

```rust
#[ktest]
fn qemu_slot_one_inta_resolves_to_plic_33() {
    let route = resolve_intx_cells(
        PciDeviceLocation { bus: 0, device: 1, function: 0 },
        1,
        &QEMU_INTERRUPT_MAP_MASK,
        &QEMU_INTERRUPT_MAP,
        |_| Some(ParentInterruptSpec { address_cells: 0, interrupt_cells: 1 }),
    )
    .unwrap();
    assert_eq!(route, PciIntxRoute { interrupt_parent: 9, interrupt: 33 });
}

#[ktest]
fn route_must_be_unique_among_present_functions() {
    let target = PciIntxEndpoint::new(bdf(0, 1, 0), 1);
    assert_eq!(exclusive_route(target, [target]), Ok(route(9, 33)));
    assert_eq!(
        exclusive_route(target, [target, PciIntxEndpoint::new(bdf(0, 5, 0), 1)]),
        Err(RiscvPciResourceError::SharedIntx),
    );
}

#[ktest]
fn dma_contract_rejects_translation_iommu_and_noncoherency() {
    assert_eq!(
        validate_dma_contract(HostDmaFields::qemu_virt()),
        Ok(DmaWindow::new(0, 0, usize::MAX).unwrap()),
    );
    assert_eq!(
        validate_dma_contract(HostDmaFields { dma_ranges: Some(&[0; 24]), ..HostDmaFields::qemu_virt() }),
        Err(RiscvPciResourceError::DmaTranslation),
    );
    assert_eq!(
        validate_dma_contract(HostDmaFields { has_iommu: true, ..HostDmaFields::qemu_virt() }),
        Err(RiscvPciResourceError::Iommu),
    );
}
```

Also cover truncated masks/maps, unknown parent phandles, parent interrupt-cell counts
other than one, invalid pins (`0` or `>4`), duplicate target entries, and a second
present BDF/pin resolving to the target route.

- [ ] **Step 2: Run the focused RED compile**

```bash
docker run --rm --network=host -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702 \
  cargo osdk check --ktests -p aster-pci --target riscv64imac-unknown-none-elf
```

Expected: compile failure names the missing parser/resource types, not an environment error.

- [ ] **Step 3: Implement the minimal public contract**

Use these exact public types; parsing helpers remain private:

```rust
#[derive(Clone, Copy, Debug)]
pub struct RiscvPciHostResources {
    pub dma_window: DmaWindow,
    pub interrupt_source: InterruptSourceInFdt,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RiscvPciResourceError {
    MissingHost,
    NonCoherentDma,
    DmaTranslation,
    Iommu,
    MissingIntx,
    InvalidIntxMap,
    SharedIntx,
}

pub fn riscv_host_resources(
    location: PciDeviceLocation,
) -> Result<RiscvPciHostResources, RiscvPciResourceError>;
```

`riscv_host_resources` must:

1. find exactly one `pci-host-ecam-generic` node;
2. require `dma-coherent`;
3. reject `iommus`, `iommu-map`, and every non-empty `dma-ranges` property;
4. return identity `DmaWindow` only after those checks;
5. read and validate the target `InterruptPin` as `1..=4`;
6. parse `interrupt-map-mask` and every variable-length `interrupt-map` entry using
   each referenced parent's `#address-cells`/`#interrupt-cells`;
7. enumerate the already-recorded PCI bus range, resolve every present function with a
   nonzero pin, and require exactly one present endpoint on the target `(parent, irq)`;
8. never fall back to `InterruptLine` or a synthetic phandle.

Store the validated bus range in a private `Once<RangeInclusive<u8>>` during the
existing PCI host initialization so the exclusivity scan neither assumes `0..=255` nor
reparses an unrelated host node.

Keep the top-level export target-gated:

```rust
#[cfg(target_arch = "riscv64")]
pub use arch::{RiscvPciHostResources, RiscvPciResourceError, riscv_host_resources};
```

- [ ] **Step 4: Run GREEN and commit**

```bash
docker run --rm --network=host -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702 \
  cargo osdk check --ktests -p aster-pci --target riscv64imac-unknown-none-elf
cargo fmt --all -- --check
git diff --check
git add kernel/comps/pci/src/arch/riscv kernel/comps/pci/src/lib.rs
git commit -m "feat(riscv): validate PCI DMA and INTx routes"
```

Expected: all focused parser/resource tests compile, formatting passes, and the commit
contains only PCI resource-contract work.

### Task 3: Add the current-main PCI xHCI adapter

**Files:**
- Create: `kernel/comps/usb/src/arch/riscv/pci.rs`
- Modify: `kernel/comps/usb/src/arch/riscv/mod.rs`
- Modify: `kernel/comps/usb/Cargo.toml`
- Modify: `Cargo.lock`

- [ ] **Step 1: Write RED tests for matching and ownership selection**

Place pure tests in the new module:

```rust
#[ktest]
fn matches_only_xhci_programming_interface() {
    assert!(is_xhci(PciDeviceId { class: 0x0c, subclass: 0x03, prog_if: 0x30, ..ID }));
    assert!(!is_xhci(PciDeviceId { prog_if: 0x20, ..XHCI_ID }));
    assert!(!is_xhci(PciDeviceId { subclass: 0x02, ..XHCI_ID }));
}

#[ktest]
fn first_valid_host_wins_without_replacing_owned_resources() {
    let first = fake_host(0, 1, 0);
    let second = fake_host(0, 2, 0);
    assert_eq!(store_host(&HOST, first), Ok(()));
    assert_eq!(store_host(&HOST, second), Err(PciAdapterError::AlreadyClaimed));
    assert_eq!(take_host(&HOST).unwrap().location, bdf(0, 1, 0));
}

#[ktest]
fn input_device_is_registered_before_the_first_report() {
    let before = aster_input::count_devices();
    let state = DeferredKeyboardState::new(UsbKeyboardInfo {
        vendor_id: 0x0627,
        product_id: 0x0001,
    });
    assert_eq!(aster_input::count_devices(), before + 1);
    assert_eq!(state.registered.device().id().bustype(), InputId::BUS_USB);
    drop(state);
    assert_eq!(aster_input::count_devices(), before);
}
```

Use a test-local storage instance so tests do not mutate the production static.

- [ ] **Step 2: Verify RED**

Run the same pinned-container `cargo osdk check --ktests` command for
`-p aster-usb -p aster-kernel`; expect missing `pci` adapter symbols.

- [ ] **Step 3: Implement adapter and shared worker boundary**

Add `aster-pci.workspace = true` only under the RISC-V target dependency table.
The adapter must retain this state:

```rust
pub(crate) struct PciHostConfig {
    pub(crate) location: PciDeviceLocation,
    pub(crate) mmio: IoMem,
    pub(crate) dma_window: DmaWindow,
    pub(crate) interrupt_source: InterruptSourceInFdt,
}
```

`PciXhciDriver::probe` must match `(class, subclass, prog_if) == (0x0c, 0x03,
0x30)`, validate `riscv_host_resources` before enabling the function, acquire only a
memory BAR0, set `BUS_MASTER | MEMORY_SPACE | INTERRUPT_DISABLE`, store one host, and
return one `Arc<PciXhciDevice>` containing the location and ID. Any unsafe DMA/INTx,
missing BAR, I/O BAR, or second controller returns a construction error without
replacing the retained host.

Refactor `mod.rs` to one helper:

```rust
fn run_keyboard_interrupt_driven(
    resources: HostResources,
    pci_location: Option<PciDeviceLocation>,
) { /* existing current-main lifecycle */ }
```

Initialization registers the PCI driver and separately preserves the selected DWC3
resource. Runtime takes PCI first, then DWC3. Enabling uses one composite guard whose
`Drop` order is controller INTE first, PCI INTx second, and PLIC mapping last. The
initial order is handler installed -> controller INTE enabled -> PCI INTx enabled ->
PLIC source rearmed. An error restores `INTERRUPT_DISABLE` and disables controller
INTE before returning.

After `PollingUsbKeyboard::open` returns the USB identity, register the input device
immediately, before waiting for the first report. `DeferredKeyboardState` owns a
non-optional `RegisteredInputDevice`; report processing only decodes and submits to
that device. This is required so the guest can identify the USB evdev node and print
READY before the host injects its first key. Any later setup error drops the
registration and must not leave a usable evdev node.

Add bounded identity markers at info level, once per lifecycle:

```text
PCI xHCI selected: 0000:00:01.0 1b36:000d irq-parent=9 irq=33
USB boot keyboard registered: 0627:0001 bus=usb name=usb_boot_keyboard
```

Do not add per-report or per-interrupt logging.

- [ ] **Step 4: Run focused GREEN and commit**

```bash
python3 -m unittest discover -s tools/usb-hid/tests -p 'test_*.py' -v
docker run --rm --network=host -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702 sh -lc \
  'RUSTFLAGS=-Dwarnings cargo clippy -p aster-pci -p aster-usb --target riscv64imac-unknown-none-elf --no-deps && cargo osdk check --ktests -p aster-pci -p aster-usb -p aster-kernel --target riscv64imac-unknown-none-elf'
cargo fmt --all -- --check
git diff --check
git add Cargo.lock kernel/comps/usb kernel/comps/pci
git commit -m "feat(riscv): attach USB keyboard through PCI xHCI"
```

Expected: 49 HID oracle tests pass; strict PCI/USB clippy and the RISC-V ktest
compile pass; no OSTD source changed.

### Task 4: Build the USB-only evdev guest oracle

**Files:**
- Create: `tools/riscv/xhci/input_gate_init.c`
- Create: `tools/riscv/xhci/build_input_gate.sh`
- Modify: `tools/riscv/tests/test_xhci_input_gate.py`

- [ ] **Step 1: Write RED native tests for device and event classification**

Compile the C source with `-DXHCI_INPUT_GATE_SELF_TEST`. The self-test feeds synthetic
devices and events and must require exactly one keyboard with:

```c
id.bustype == BUS_USB
strcmp(name, "usb_boot_keyboard") == 0
events == {
    { EV_KEY, KEY_A, 1 }, { EV_SYN, SYN_REPORT, 0 },
    { EV_KEY, KEY_A, 0 }, { EV_SYN, SYN_REPORT, 0 },
    { EV_KEY, KEY_1, 1 }, { EV_SYN, SYN_REPORT, 0 },
    { EV_KEY, KEY_1, 0 }, { EV_SYN, SYN_REPORT, 0 },
};
```

The tests must reject zero matches, two matching keyboards, a VirtIO keyboard, missing
release/sync events, reordering, `SYN_DROPPED`, partial `struct input_event` reads,
panic text, and a deadline overrun.

- [ ] **Step 2: Verify RED**

```bash
python3 -m unittest tools.riscv.tests.test_xhci_input_gate.XhciGuestProbeTests -v
```

Expected: failure because the C source/build script does not exist.

- [ ] **Step 3: Implement the smallest static probe and deterministic builder**

The real `/init` mounts `devtmpfs`, scans `/dev/input/event0..31`, uses `EVIOCGID` and
`EVIOCGNAME`, rejects every second keyboard-like evdev node, prints
`XHCI_INPUT_READY path=... bustype=3 name=usb_boot_keyboard`, reads only complete
events with a 30-second monotonic deadline, and prints exactly
`XHCI_INPUT_PASS events=8` before entering an EINTR-safe `pause()` loop. It prints one
`XHCI_INPUT_FAIL reason=...` and exits nonzero on any failure.
Before PASS it also prints one canonical `XHCI_INPUT_EVENT type=... code=... value=...`
line for each of the eight normalized events so host evidence can record what the guest
actually accepted rather than inferring events from the injected HMP commands.

The builder must use `riscv64-linux-gnu-gcc -static -Wall -Wextra -Werror`, create raw
`newc` entries exactly `.` and `init`, normalize owner/time/mode, publish atomically,
and reject unsafe output paths. `SOURCE_DATE_EPOCH` defaults to zero and must be a
decimal newc-u32 value.

- [ ] **Step 4: Run GREEN and commit**

```bash
python3 -m unittest tools.riscv.tests.test_xhci_input_gate.XhciGuestProbeTests -v
bash -n tools/riscv/xhci/build_input_gate.sh
git diff --check
git add tools/riscv/xhci tools/riscv/tests/test_xhci_input_gate.py
git commit -m "test(riscv): add PCI xHCI evdev guest oracle"
```

Expected: native state-machine tests pass and two builds one second apart are
byte-identical.

### Task 5: Add a bounded QEMU/HMP xHCI gate

**Files:**
- Create: `tools/riscv/xhci/input_gate.py`
- Modify: `tools/riscv/tests/test_xhci_input_gate.py`
- Modify: `Makefile`

- [ ] **Step 1: Write host RED tests**

Define injected dependencies for artifact staging, launch, serial protocol, HMP, and
process-group cleanup. Tests must assert:

- argv contains `-smp 4`, `-device qemu-xhci,id=xhci,msi=off,msix=off`, and
  `-device usb-kbd,id=usb-kbd,bus=xhci.0` in that order;
- argv contains no `virtio-keyboard`, `i8042`, user networking, or extra USB device;
- the HMP socket is mode-0700-directory-contained, bounded, and receives `sendkey a`
  followed by `sendkey 1` only after `XHCI_INPUT_READY`;
- PASS requires the exact PCI BDF/route marker, exact USB identity marker,
  `XHCI_INPUT_PASS events=8`, no fallback keyboard marker, and no panic/oops;
- missing, duplicated, stale-before-READY, or reversed markers fail;
- preparation, connect, read, launch, signal, timeout, and classifier errors invalidate
  old evidence and terminate the complete QEMU process group;
- `serial.log` and `result.json` are atomically replaced only from this run.

- [ ] **Step 2: Verify RED**

```bash
python3 -m unittest tools.riscv.tests.test_xhci_input_gate.XhciHostGateTests -v
```

Expected: missing host-gate API failures.

- [ ] **Step 3: Implement the host gate**

Expose this CLI:

```text
python3 tools/riscv/xhci/input_gate.py \
  --uboot PATH --boot-disk PATH --manifest PATH \
  --serial-log PATH --result PATH --smp 4
```

Pin regular inputs with `O_NOFOLLOW`, copy them into a private mode-0700 run directory,
create HMP there, and launch QEMU with `start_new_session=True`. Use the registered
generic-Sv39 CPU/DTB contract already documented by the U-Boot tools. Set total
deadlines for boot, HMP prompt/commands, and post-input evidence; cap serial and HMP
bytes. On every path close HMP, disable further actions, TERM the process group, wait a
bounded grace, KILL survivors, drain serial to EOF/deadline, restore signal handlers,
then classify and atomically publish evidence.

`result.json` must include `passed`, `reason`, `smp`, QEMU version, all input SHA-256
hashes, exact argv, PCI BDF/route, USB vendor/product/bus/name, observed normalized
events, cleanup status, and serial SHA-256. No old result survives a failed start.

Add `make test_riscv_xhci_input_unit` for the focused Python/native tests. Do not add
the runtime QEMU gate to default `make test`.

- [ ] **Step 4: Run host GREEN and commit**

```bash
make test_riscv_xhci_input_unit
python3 -m py_compile tools/riscv/xhci/input_gate.py tools/riscv/tests/test_xhci_input_gate.py
git diff --check
git add Makefile tools/riscv/xhci/input_gate.py tools/riscv/tests/test_xhci_input_gate.py
git commit -m "test(riscv): automate PCI xHCI keyboard gate"
```

Expected: focused host/native tests pass quickly without launching QEMU.

### Task 6: Run the new M1 runtime gate once

**Files:**
- Create ignored evidence under: `target/riscv-xhci-input/`
- Verify: normal RISC-V kernel and prepared generic-Sv39 U-Boot artifacts

- [ ] **Step 1: Build only inputs changed by M1**

In the pinned container, build the guest initramfs and normal kernel serially:

```bash
mkdir -p target/riscv-xhci-input
docker run --rm --network=host -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702 sh -lc '
    tools/riscv/xhci/build_input_gate.sh target/riscv-xhci-input/initramfs.cpio &&
    make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode'
```

Expected: a static RISC-V `/init` archive and nonempty normal kernel artifacts. Do not
run the full OSTD ktest suite.

- [ ] **Step 2: Prepare the generic-Sv39 boot disk with the M1 initramfs**

Use a private output directory so the current shared prepared artifacts are not
overwritten:

```bash
ASTERINAS_RISCV_BOOTI="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
ASTERINAS_INITRAMFS="$PWD/target/riscv-xhci-input/initramfs.cpio" \
QEMU_UBOOT_PROFILE=generic-sv39-ltp-smp4 \
QEMU_UBOOT_OUT_DIR="$PWD/target/qemu-uboot/xhci-input" \
QEMU_UBOOT_BUILD_DIR="$PWD/target/qemu-uboot/cache/u-boot-build" \
  tools/riscv/prepare_qemu_uboot_booti.sh prepare
python3 - <<'PY'
import hashlib, json
from pathlib import Path

manifest = json.loads(Path("target/qemu-uboot/xhci-input/artifacts.json").read_text())
inputs = {
    "kernel_sha256": Path("target/osdk/aster-kernel/aster-kernel-osdk-bin.Image"),
    "initrd_sha256": Path("target/riscv-xhci-input/initramfs.cpio"),
}
for field, path in inputs.items():
    assert manifest[field] == hashlib.sha256(path.read_bytes()).hexdigest(), (field, path)
PY
```

Expected: preparation succeeds and the manifest identifies the just-built kernel and
M1 initramfs; the generated DTB remains bound by the existing profile audit.

- [ ] **Step 3: Run the exact xHCI gate**

```bash
timeout 180s python3 tools/riscv/xhci/input_gate.py \
  --uboot target/qemu-uboot/cache/u-boot-build/u-boot \
  --boot-disk target/qemu-uboot/xhci-input/boot.ext4 \
  --manifest target/qemu-uboot/xhci-input/artifacts.json \
  --serial-log target/riscv-xhci-input/serial.log \
  --result target/riscv-xhci-input/result.json \
  --smp 4
```

Expected: exit zero; `result.json` says `passed: true`; serial contains exactly one PCI
xHCI selection, exactly one `BUS_USB` keyboard registration, READY then PASS, and no
panic. QEMU command evidence contains no alternative keyboard.

- [ ] **Step 4: Re-run only the cheap post-runtime checks**

```bash
make test_riscv_xhci_input_unit
RUSTFLAGS=-Dwarnings cargo clippy -p aster-pci -p aster-usb \
  --target riscv64imac-unknown-none-elf --no-deps
cargo fmt --all -- --check
git diff --check
```

Expected: all pass. Do not rerun the unchanged 291-test OSTD QEMU gate.

### Task 7: Document and review M1

**Files:**
- Create: `tools/riscv/xhci/README.md`
- Modify: `tools/riscv/README.md`
- Review: all commits after the plan commit

- [ ] **Step 1: Document the proved boundary**

Record the exact build/run commands, QEMU version, `smp=4`, controller BDF/ID, INTx
route, USB ID, evdev name/bus, input sequence, artifact/result hashes, and cleanup
status. State explicitly that hotplug, LEDs, repeat, TTY/Xorg, hubs, and physical
Megrez remain unproved.

- [ ] **Step 2: Run the Asterinas persona review**

Use the `aster-code-review` skill in diff mode from the M1 plan commit through HEAD.
Resolve every Critical or Important finding with a new RED/GREEN test and focused
commit. Do not broaden M1 to fix Minor recommendations unrelated to its contract.

- [ ] **Step 3: Final verification and milestone commit**

```bash
make test_riscv_xhci_input_unit
git diff --check
git status --short --branch
git log --oneline --decorate -8
```

Expected: clean worktree, all focused tests pass, runtime evidence is ignored, and the
review has no open Critical/Important issue. Report M1 separately; do not claim the
full keyboard subsystem or Megrez hardware complete.

## Stop conditions

- Stop before enabling the PCI function if coherent identity DMA cannot be proved.
- Stop if the target route is shared by another present PCI function; M1 does not add a
  generic shared-INTx dispatcher.
- Stop if QEMU exposes MSI/MSI-X-only behavior; do not implement RISC-V MSI as an
  unplanned shortcut.
- Stop if a second keyboard-capable evdev node is present in the runtime gate.
- Stop on any test that passes before its intended production behavior exists; repair
  the RED test before implementation.
- Do not copy `32979fab9` or `ba139ca91`; they are behavioral provenance only.
- Do not touch the original dirty worktree at `/home/ubuntu/xaj/Program/asterinas`.
- Do not push or monitor remote CI during M1.
