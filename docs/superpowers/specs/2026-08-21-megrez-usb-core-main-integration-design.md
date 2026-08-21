# Megrez USB/xHCI Core Main Integration Design

## Objective

Move the still-useful USB keyboard and xHCI work from
`codex/megrez-usb-keyboard` onto the current local `main` without merging the
topic branch's obsolete history. The first milestone ends with the reviewed
Megrez DWC3/xHCI software path and a safe RISC-V PCI BAR foundation on `main`;
PCI xHCI runtime input, publication, and physical-board claims remain separate.

The integration baseline recorded on 2026-08-21 is:

- local `main`: `cc0d19383`;
- source topic: `ecdea5a39` (`codex/megrez-usb-keyboard`);
- divergence: 40 topic-side commits and 86 main-side commits;
- patch audit: 5 topic patches are already equivalent to `main`, while 33 are
  still non-equivalent and two are merge commits;
- `main` currently has no `kernel/comps/usb`, `ostd/src/bus/usb`, or
  `tools/usb-hid` tree.

## Scope

This milestone integrates only the coherent USB input path:

1. the `aster-usb` component and workspace/component wiring;
2. OSTD USB support, bounded HID report queues, and the DMA adapter required by
   the xHCI library;
3. fail-closed RISC-V PCI BAR allocation from Device Tree memory windows;
4. the Megrez-selected DWC3/xHCI MMIO path;
5. interrupt-driven event-ring handling, HID boot-keyboard decoding, Linux
   keycode translation, and input-device registration;
6. the minimum TTY/input changes needed to deliver and echo keyboard input
   without holding the line-discipline lock;
7. host-side keyboard oracle tests and focused kernel tests.

The following work is deliberately deferred to later milestones:

- AsterNixOS/XFCE/X11 configurations;
- framebuffer and other graphics changes;
- board-session publication tooling, persistent boot, and reboot policy;
- unrelated historical plans and evidence documents;
- physical Megrez DWC3 verification.
- PCI xHCI discovery and QEMU keyboard input, until the driver has a validated
  PCI DMA contract and either MSI/MSI-X or shared INTx dispatch;

## Integration Strategy

Work occurs on `codex/megrez-usb-core-main-integration` in the isolated
`megrez-usb-core-main` worktree. The source topic remains unchanged until the
candidate is verified.

The source commits are replayed in dependency order rather than merged as a
single history:

1. USB component, OSTD DMA/queue support, and HID translation;
2. event-ring interrupt support and the `aster-softirq` dependency;
3. RISC-V PCI BAR allocation and assignment preflight;
4. interrupt stabilization and TTY input delivery;
5. dependency and portability fixes that are still required on current
   `main`.

Each source commit is first checked against current `main`. A patch is skipped
when the behavior is already present, split when it mixes deferred scope, and
adapted when current interfaces have changed. Conflict resolution always keeps
the current main-side contract and ports only the missing USB behavior.

## Runtime Architecture

On Megrez, `/chosen/asterinas,usb-host` may select an enabled `snps,dwc3` node.
The kernel validates its MMIO, interrupt, and DMA-window contract, switches the
DWC3 wrapper to host mode, and starts the xHCI keyboard session. An invalid or
absent selector must emit a bounded warning and disable only USB input; it must
not panic or access an unvalidated MMIO range.

On RISC-V generic ECAM hosts, the PCI layer may allocate zero memory BARs only
when all ordinary and expansion-ROM memory BARs are unassigned. It accepts only
identity-mapped, non-prefetchable 32-bit Device Tree windows and fails closed on
mixed assignments, translated windows, or unsupported layouts. No PCI xHCI
driver is registered in this milestone.

USB interrupt-IN reports enter a bounded OSTD queue. A deferred handler decodes
HID boot-keyboard reports into Linux-compatible key events, registers exactly
one input device, and forwards the events through the existing input/TTY path.
No polling loop runs while the keyboard is idle.

## Safety and Failure Handling

- `kernel/` remains safe Rust; hardware-facing `unsafe` stays inside OSTD or
  existing audited dependency boundaries.
- MMIO ranges, capability lengths, BAR types, DMA windows, interrupt parents,
  and interrupt pins are validated before use.
- Queue and event processing are bounded so malformed or repeated events
  cannot monopolize an interrupt context.
- Startup failures are reported once and disable only the USB keyboard path;
  they do not prevent the rest of the kernel from reaching userspace.
- DWC3 selection failure leaves the rest of the kernel operational.
- Kernel builds and `cargo osdk test` run serially because the latter replaces
  the normal QEMU kernel artifact.

## Validation

Validation proceeds from cheap to expensive:

1. `git diff --check`, formatting, Cargo metadata, and component-wiring checks;
2. the complete `tools/usb-hid` Python oracle suite;
3. focused RISC-V Cargo checks for OSTD, PCI, USB, input, and kernel crates;
4. focused RISC-V kernel tests, including DMA, bounded report queues, HID
   translation, event-ring IRQ ownership, and PCI BAR parsing/allocation;
5. a complete RISC-V kernel build;
6. QEMU `virt` with `-smp 4` for the focused xHCI event-ring and PCI BAR kernel
   tests; attaching `qemu-xhci` exercises only the safe PCI foundation and is
   not recorded as keyboard runtime support;
7. the repository's RISC-V host-tool discovery suite and proportional
   `make check` coverage.

The historical 2026-08-10 source-branch evidence records QEMU keyboard
acceptance, safe invalid-selector fallback, 239/239 OSTD tests, and an Sv48
four-hart userspace marker. That evidence establishes the source baseline, but
all software gates are rerun on the new main-based candidate. QEMU cannot
validate Megrez clocks, resets, cache hardware, or the real DWC3 block, so no
new physical-board claim is made.

## Success Criteria

The milestone is complete when:

- the integration branch is a clean descendant of current local `main`;
- `aster-usb` is present exactly once in the workspace and component graph;
- the relevant host, build, and kernel-test gates pass locally;
- QEMU `smp=4` proves the focused event-ring IRQ and PCI BAR kernel tests;
- code review finds no unresolved Critical or Important issue;
- the verified commits can fast-forward local `main` without pulling in the
  deferred NixOS, graphics, board-tooling, or historical branch stack;
- no remote ref is changed without separate user approval.

## Review Amendment: PCI xHCI Deferred

The initial design included a direct PCI xHCI path on QEMU `virt`. Review found
that this would assume identity DMA without validating `dma-ranges`/IOMMU and
would map a potentially shared PCI INTx source as an exclusive deferred PLIC
line. That implementation was removed before integration. Future PCI xHCI work
must first establish a validated DMA window and a shared-INTx dispatcher or
MSI/MSI-X path; QEMU desktop/browser input remains a later milestone.
