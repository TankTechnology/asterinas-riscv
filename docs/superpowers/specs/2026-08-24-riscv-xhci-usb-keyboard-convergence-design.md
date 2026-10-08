# RISC-V xHCI USB Boot Keyboard Convergence Design

## Objective

Converge the existing `codex/megrez-usb-keyboard` branch with current
`origin/main`, then complete an ordinary USB Boot Keyboard path through QEMU
PCI xHCI, the Asterinas input stack, TTY, and Xorg.

The work preserves the topic branch's public history and its useful engineering
evidence, but does not treat historical source code as authoritative.
Current `main` owns the IRQ, xHCI lifecycle, MMIO, DMA, MSI, PCI BAR, and input
contracts.
Historical work supplies requirements, test vectors, known failure modes, and
platform behavior that may be reimplemented against those contracts.

The baseline recorded on 2026-08-24 is:

- topic branch: `ecdea5a39` (`codex/megrez-usb-keyboard`);
- downstream main: `8cd69a7d5` (`origin/main`);
- merge base: `1ed8a46c5`;
- divergence: 40 topic-only commits and 315 main-only commits;
- the topic worktree has pre-existing uncommitted USB reconciliation changes
  and generated logs that must not be overwritten or accidentally committed.

Before implementation begins, fetch `origin` once and record the resulting
`origin/main` object as `INTEGRATION_MAIN` in the implementation plan.
All uses of "current main" below mean that immutable object, not a moving
remote-tracking ref.
If `origin/main` advances later, absorbing the newer object is a separate,
reviewed merge rather than an implicit change to this milestone.

## Decisions

### Preserve history without preserving obsolete implementation

Use a normal merge of `INTEGRATION_MAIN` into an isolated synchronization branch
created from the topic history.
Do not rebase, squash, or force-push the topic branch.

The merge is not permission to resolve USB conflicts with a blanket `ours`
choice.
When historical code disagrees with a current main-side contract, keep the
main-side contract and re-express only the missing behavior.
Do not cherry-pick an old USB patch merely because Git reports it as
non-equivalent.

### Limit the device scope to an ordinary keyboard

This milestone supports USB HID Boot Protocol keyboards with interface class
3, subclass 1, and protocol 1.
It accepts a selected configuration containing exactly one interface that
satisfies that tuple and exposes exactly one interrupt-IN endpoint.
It covers every usage from the Boot Keyboard Keyboard/Keypad page that maps to
the checked-in Linux keycode oracle, the eight modifier bits, six-key rollover,
lock LEDs, key repeat, disconnect, and reconnect.

Generic HID report descriptors, NKRO keyboards, media keys, vendor controls,
composite devices with additional interfaces, and alternate endpoint layouts
are out of scope.
Enumeration skips such devices without preventing other supported devices from
being examined.

### Make QEMU PCI xHCI the executable platform gate

QEMU `virt` with `qemu-xhci` and `usb-kbd` is the first complete runtime gate.
It validates the generic PCI xHCI path on RISC-V with `smp=4`.

The Megrez DWC3 path remains part of the shared architecture and retains its
simulation and handoff checks.
QEMU cannot validate the EIC7700 clocks, resets, cache behavior, DMA hardware,
or physical DWC3 block, so no new physical-board claim is made without a real
board run.

## Historical Progress Ledger

Historical work is classified by behavior, not by commit count.
The implementation plan must record each relevant topic-side change in one of
the following categories before resolving its files.

### Superseded by current main

Current main is authoritative for:

- masked RISC-V IRQ mapping and deferred `rearm()`;
- moving USB event-ring work out of interrupt context;
- xHCI interrupt enable, disable, and ownership ordering;
- event-ring ownership and callback teardown;
- xHCI MMIO and capability validation;
- DMA allocation, mapping, and host-boundary hardening;
- unsafe MSI capability rejection;
- fail-closed PCI BAR discovery and allocation;
- input-device and evdev unregister support.

The concrete authorities are `ostd/src/bus/usb.rs` and its report queue,
`ostd/src/arch/riscv/irq/`, `kernel/comps/usb/`, `kernel/comps/pci/`,
`kernel/comps/input/`, and `kernel/src/device/evdev/` as they exist at
`INTEGRATION_MAIN`.
The implementation plan may name a narrower API from those modules, but may not
replace their ownership model with a topic-side variant.

The topic implementations of these behaviors are historical references only.
They must not be restored over main.

### Reimplement against current main

The following topic results remain valuable, but their old implementation is
not carried forward verbatim:

- generic PCI discovery of a QEMU xHCI controller;
- validated BAR and DMA attachment of that controller;
- PCI interrupt selection and routing;
- USB keyboard delivery to the input, evdev, TTY, and Xorg paths;
- runtime disconnect and re-enumeration;
- HID LED output reports;
- ordinary keyboard repeat behavior.

The new PCI adapter should be small.
It discovers and validates platform resources, then delegates controller and
keyboard behavior to the shared main-side USB implementation.

### Reuse as compatibility artifacts after fresh verification

The following artifacts may be retained when their provenance and assertions
remain valid:

- Linux-derived HID keyboard translation vectors;
- deterministic keyboard report-oracle tests;
- QEMU launch and monitor scenarios;
- Megrez Device Tree patching and board-session tools;
- board handoff checklists and prior failure records.

Historical runtime logs are evidence of an earlier baseline, not evidence for
the converged branch.
Every claimed software behavior is rerun locally.

## Merge and Conflict Policy

### Isolation and recovery

The current dirty topic worktree remains untouched during the merge.
Before integration, record the exact branch head, status, and a recoverable
snapshot of the three pre-existing modified files.
Create the synchronization branch in a separate worktree from the recorded
topic commit.

The uncommitted changes are inputs to an audit, not implicit implementation
requirements.
They are compared after main is merged and applied only when they express a
still-missing behavior under the approved architecture.

### Main-authoritative conflict areas

Resolve the following areas in favor of current main, then add focused missing
behavior in later commits:

- OSTD IRQ APIs and PLIC claim/mask/complete ordering;
- xHCI host initialization, event pumping, and teardown;
- MMIO range and capability validation;
- DMA ownership and cache coherency;
- MSI/MSI-X and shared INTx safety policy;
- PCI BAR allocation and Device Tree window validation;
- input device and evdev lifecycle;
- current component and workspace wiring.

Do not preserve two controller lifecycles or two IRQ state machines in the
merged tree.

### Historical-only conflict areas

Old desktop, NixOS, framebuffer, reboot, board-management, and LTP commits are
not pulled into the USB milestone merely because they share the topic history.
They remain present as existing branch history after the merge, but USB
follow-up commits must not modify or validate them unless a direct dependency
is demonstrated.

## Runtime Architecture

```text
                         shared main-side USB/xHCI core
                         ┌────────────────────────────┐
QEMU PCI host adapter ──▶│ MMIO + DMA + event ring    │
                         │ IRQ enable/disable         │
Megrez DWC3 adapter ────▶│ device enumeration         │
                         └─────────────┬──────────────┘
                                       │ interrupt-IN reports
                                       ▼
                         USB Boot Keyboard session
                         ├── report decoding
                         ├── LED output report
                         ├── disconnect/reconnect
                         └── input device lifetime
                                       │
                                       ▼
                         Asterinas input core / evdev
                         ├── TTY and line discipline
                         └── Xorg evdev consumer
```

### Controller and platform boundary

The OSTD USB layer owns the hardware-facing xHCI object, DMA objects, event
handler, and Boot Keyboard session.
`kernel/` remains safe Rust.

Each RISC-V platform adapter is responsible only for validated resource
discovery and lifecycle wiring:

- the Megrez adapter resolves the selected enabled DWC3 node, validates its
  MMIO, interrupt, and DMA contract, and places the wrapper in host mode;
- the QEMU adapter discovers the PCI xHCI function, validates or allocates its
  BAR through the current PCI layer, establishes a supported DMA window, and
  acquires a safe interrupt route.

The QEMU adapter must not assume that PCI bus addresses equal CPU physical
addresses.
It must reject unsupported translations, IOMMUs, MSI capabilities, or shared
INTx arrangements unless current main provides an explicit safe contract for
them.

### IRQ lifecycle

Initialization follows this order:

1. validate MMIO, DMA, and interrupt resources;
2. map the RISC-V interrupt in a masked state;
3. install the deferred callback and its teardown ownership;
4. initialize xHCI and enumerate the keyboard while controller IRQs are off,
   using the existing synchronous event pump;
5. enable xHCI interrupt generation;
6. rearm the platform interrupt.

The synchronous event pump is permitted only during startup and control
operations that precede runtime IRQ ownership.
It polls the future, handles pending controller events, yields task context,
and uses main's five-second host-operation timeout and 30-second discovery
timeout.
After runtime IRQs are enabled, an idle keyboard must not be serviced by a
polling loop.

The top half masks and acknowledges the source, records bounded work, and
wakes task context.
Task context drains at most 64 completed input reports per wake, then either
reschedules remaining work or rearms the mapping.
No enumeration, allocation, input registration, or blocking lock acquisition
runs in interrupt context.

Shutdown runs in reverse ownership order:

1. stop accepting new keyboard work;
2. disable xHCI interrupt generation;
3. mask and quiesce the platform interrupt;
4. drain or cancel owned deferred work;
5. unregister the input device;
6. release the controller, DMA, MMIO, and IRQ objects.

### Keyboard input and rollover behavior

The keyboard session requests Boot Protocol and consumes standard eight-byte
reports.
It compares each report with the previous report to emit key press and release
events exactly once.
Modifier changes are emitted as ordinary Linux-compatible key events.

Boot Protocol rollover/error usages do not become keys.
The decoder records one diagnostic only when a device session enters rollover,
records no additional diagnostic for repeated rollover reports, and rearms the
diagnostic only after a valid report permits recovery.
It preserves a coherent pressed-key state during that interval.
A malformed or stalled device disables only that device session.

### Lock LEDs

Current main accepts and discards evdev `EV_LED` writes and reports no LED
state.
The converged design replaces that no-op for capable physical input devices
with an output callback owned by the registered USB keyboard.

`EV_LED` updates are accumulated until `SYN_REPORT`, converted to the Boot
Keyboard LED bitmap, and sent through the HID output-report path in sleepable
context.
Output failure is reported and bounded; it does not block input delivery or
panic the kernel.
The desired software state remains queryable through evdev.
Only a successful HID transfer updates the separately tracked
last-confirmed-device state; QEMU or physical evidence must not claim that the
LED changed from desired state alone.

### Key repeat

Repeat is an input policy, not an xHCI or HID transport event.
The USB driver emits one press and one release for each physical transition.

The Asterinas input core is the single repeat owner for devices that advertise
`EV_REP`.
It defaults to a 250-ms delay and a 33-ms period, emits Linux-compatible
`EV_KEY` value 2 events, supports `EVIOCGREP` and `EVIOCSREP`, and cancels the
timer on release, disconnect, or device teardown.
Modifiers, lock keys, and keys no longer present in the current physical report
are not repeated.

TTY and evdev consume this one stream.
The isolated QEMU Xorg gate disables the server-wide repeat source with
`xset r off`; the gate attaches no other keyboard.
With the default input-core settings, holding one printable key for one second
must produce one initial character and 20 to 26 repeated characters in xterm;
the tolerance covers scheduler and display timing without allowing a doubled
repeat stream.

### Disconnect and reconnect

Controller port-change events trigger device-lifecycle work in task context.
A disconnect cancels pending input and LED work, releases all logically pressed
keys, unregisters the input device, and removes the evdev node.

A later connection starts a fresh enumeration session.
It must not reuse stale endpoint, slot, DMA, key-state, LED-state, or repeat
state.
Repeated attach/detach cycles must not create duplicate input registrations.

## Validation Strategy

Validation is local and staged from inexpensive checks to full QEMU desktop
interaction.
Remote CI is not monitored as a progress mechanism.
RISC-V runtime gates use `smp=4` unless a narrower unit test cannot involve
SMP.

### Structural and unit gates

- prove the merge has two parents and no unresolved paths;
- run formatting, whitespace, license, component, and Cargo metadata checks;
- run the complete Linux keyboard translation oracle;
- test report deltas, modifiers, rollover recovery, and exact press/release
  ordering;
- test LED bitmap conversion, output coalescing, and output failure;
- test repeat scheduling and cancellation;
- test disconnect cleanup and fresh reconnect state;
- test PCI BAR, DMA-window, and interrupt-route rejection cases.

For this design, "bounded" has the following executable meaning:

- startup host operations time out after five seconds and keyboard discovery
  after 30 seconds, matching the pinned main constants;
- one deferred wake handles at most 64 reports before yielding;
- one LED transfer uses the five-second host-operation timeout and is not
  retried indefinitely;
- the idle-runtime test observes the USB worker for one second and requires no
  top-half or worker-count increase after the controller has settled;
- the burst test injects 256 press/release pairs, or 512 reports, and requires
  exactly 256 presses, 256 releases, and ordered synchronization markers;
- the hotplug test completes 20 remove/add cycles and requires input-device,
  evdev-node, controller-slot, IRQ-worker, and DMA-allocation counts to return
  to their pre-cycle baselines;
- the synthetic IRQ-pressure test keeps work pending beyond one 64-report
  budget and proves that another task turn is scheduled without an unbounded
  top half or diagnostic loop;
- the top half performs one claim/mask/acknowledge/wake sequence and no logging
  loop per delivered interrupt;
- 100 consecutive wakes that produce neither a completed report nor lifecycle
  progress disable that controller session as a storm, with at most one warning
  per second;
- disconnect cleanup and HID control operations share the five-second host
  operation timeout, while a reconnect gets a fresh 30-second discovery
  deadline.

Test-only counters may expose these invariants to kernel tests, but they are not
part of the user-visible ABI.

### QEMU PCI xHCI gate

Boot QEMU RISC-V `virt` with a four-hart Device Tree, `qemu-xhci`, and
`usb-kbd`.
Do not attach a VirtIO keyboard or another input source.
The test must assert the discovered controller bus and USB device identity so
input from a fallback device cannot produce a false pass.

Concretely, the gate checks the QEMU command line, the xHCI PCI class/BDF marker,
the evdev device's `BUS_USB` identifier and USB Boot Keyboard name, and the
absence of any other keyboard-capable evdev node before injecting input.

Exercise:

- enumeration and exactly one evdev registration;
- letters, digits, punctuation, Enter, Tab, Escape, and Backspace;
- Shift, Control, Alt, and multi-key combinations;
- key press, release, hold, and configured repeat;
- Caps Lock, Num Lock, and Scroll Lock state and HID LED output;
- rapid input without dropped or duplicated transitions;
- QEMU monitor `device_del` followed by `device_add`;
- evdev removal, clean key release, re-enumeration, and resumed input;
- no panic, IRQ storm, idle polling loop, or unbounded diagnostic output.

### TTY and Xorg gate

Prove the same USB device through both user-visible paths:

- enter and edit commands in the console TTY;
- start Xorg with the evdev device;
- type into xterm;
- enter text in PCManFM and NetSurf fields;
- confirm modifier, repeat, and lock-key behavior in the graphical session;
- perform one unplug/replug cycle and resume graphical input without reboot.

Screenshots are supporting evidence for the desktop state.
Serial logs, evdev identity, monitor commands, and deterministic input markers
remain the authoritative pass/fail evidence.

### Megrez boundary

Run the existing Device Tree, Sv39/Sv48, DMA-window, invalid-selector, and board
session checks that do not require hardware.
The reproducible entry points are `tools/riscv/verify_megrez_sim.sh`,
`tools/riscv/megrez_patch_dtb.py`, and
`python3 -m unittest tools.riscv.tests.test_megrez_board_session` when those
artifacts survive the main-authoritative merge audit.
If an entry point is rejected as obsolete, the same milestone must add and
document its main-compatible replacement before claiming the corresponding
gate.
Removing the behavior or waiving the gate requires separate user approval; it
cannot happen implicitly during conflict resolution.
Record physical DWC3, real keyboard LED, port power, cache, reset, and reconnect
behavior as pending until the board is available.

## Milestones

### M0: Main convergence

- preserve the dirty worktree and create an isolated merge candidate;
- merge `INTEGRATION_MAIN` without rewriting history;
- resolve conflicts with the main-authoritative policy;
- remove duplicate or superseded USB/IRQ variants;
- restore a clean build and existing USB unit baseline.

### M1: QEMU PCI xHCI foundation

- reimplement the minimal PCI xHCI adapter;
- prove BAR, DMA, interrupt, and lifecycle rejection paths;
- enumerate one Boot Keyboard and deliver basic evdev input.

### M2: Ordinary keyboard lifecycle

- complete report semantics and rollover handling;
- implement disconnect, unregister, reconnect, and state reset;
- validate repeated hotplug cycles under `smp=4`.

### M3: LEDs and repeat

- connect evdev LED state to HID output reports;
- implement the input-core `EV_REP` owner for TTY and Xorg;
- prove cancellation and output-failure behavior.

### M4: TTY and graphical acceptance

- pass the full QEMU keyboard matrix;
- demonstrate console and Xorg applications with no fallback keyboard;
- archive deterministic logs and one visual desktop result.

### M5: Megrez hardware acceptance

- rerun the board handoff procedure when hardware is available;
- validate physical DWC3 enumeration, DMA, IRQs, LEDs, and reconnect;
- keep any board-specific fix behind the platform adapter boundary.

Each milestone ends with focused commits and local evidence before the next
begins.
Already-passing expensive gates are not repeated unless their inputs or
dependent code changed.

## Failure Handling

- A merge-induced failure is fixed before adding new keyboard behavior.
- A failure reproduced unchanged on `INTEGRATION_MAIN` is recorded as a baseline
  issue and is not hidden by unrelated USB changes.
- Unsupported PCI DMA or interrupt topology fails closed and disables only the
  PCI USB controller.
- Enumeration, transfer, LED, and disconnect timeouts are bounded and disable
  only the affected device session.
- An IRQ storm masks the source, records bounded diagnostics, and leaves the
  rest of the system operational.
- A failed reconnect cannot retain an evdev node that appears usable.
- No milestone claims success from historical logs or screenshots alone.

## Success Criteria

The software milestone is complete when:

- the synchronization branch contains `INTEGRATION_MAIN` and the topic
  history without rewriting either;
- final USB and IRQ code follows current main contracts rather than historical
  implementations;
- QEMU RISC-V `smp=4` drives an ordinary USB Boot Keyboard exclusively through
  PCI xHCI;
- standard keys, modifiers, rollover recovery, repeat, lock LEDs, disconnect,
  reconnect, evdev, TTY, and Xorg pass their local gates;
- teardown leaves no duplicate device, stuck key, live IRQ worker, or owned DMA
  object;
- code review has no unresolved Critical or Important issue;
- physical Megrez status is reported separately and truthfully.

## Non-goals

- Generic HID report-descriptor parsing, NKRO, media keys, or vendor features.
- USB mice, touchscreens, storage, audio, or hubs as part of this milestone.
- Copying the historical PCI xHCI adapter verbatim.
- Replacing main's IRQ, DMA, MMIO, MSI, or PCI BAR architecture.
- Fixing unrelated NixOS, graphics, browser, networking, LTP, or reboot work.
- Monitoring remote CI instead of running the relevant gates locally.
- Claiming physical Megrez verification without board evidence.
