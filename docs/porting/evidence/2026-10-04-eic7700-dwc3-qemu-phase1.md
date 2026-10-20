# EIC7700 DWC3 QEMU Phase 1 evidence

Date: 2026-10-04

## Scope

This evidence closes the probe, reset, DMA-event, and interrupt portion of the
EIC7700 DWC3 QEMU work. It does not claim deterministic bulk USB transfer or
physical EIC7700 PHY fidelity; those remain Phase 2 and physical-board work.

The model is based on QEMU 10.2.1 at
`2d3df8abca265c9bcc9e438d691d561592060998`. The focused implementation is
commit `975cfa66ce4753424c835af443298ab26a92166d` on local branch
`codex/eic7700-dwc3-phase1` in
`/home/ubuntu/.codex/qemu-eic7700-10.2.1`.

## Implemented contract

- Optional `virt,eic7700-usb=on` machine property; the default `virt` machine
  remains unchanged.
- Two existing QEMU `usb-dwc3`/xHCI instances at `0x50480000` and
  `0x50490000`, connected to PLIC sources 85 and 86 and system memory DMA.
- Device-tree nodes compatible with `eswin,eic7700-dwc3` and `snps,dwc3`, with
  host mode, non-coherent DMA, IRQs, identity DMA ranges, and HSP/SYSCRG
  references.
- A deliberately small HSP/SYSCRG register bank for the offsets consumed by
  the current Asterinas platform contract. System reset clears this state.
- qtest coverage for DWC3 identity and instance isolation, glue reset, xHCI
  event-ring DMA, PLIC pending state, and IRQ assertion/deassertion.

The implementation directly creates QEMU's existing `usb-dwc3` devices from
the RISC-V `virt` machine instead of introducing a second wrapper type. The
EIC7700-specific MMIO placement, device tree, glue, DMA, and IRQ wiring remain
separately testable.

## Fresh verification

### QEMU build and qtest

```sh
docker exec qemu-eic7700-builder-20261004 bash -lc \
  'cd /root/qemu && nix-shell -p glib.dev pixman pkg-config meson ninja python3 \
   --run "ninja -C build -j2 qemu-system-riscv64 \
   tests/qtest/eic7700-dwc3-test && meson test -C build --print-errorlogs \
   qtest-riscv64/eic7700-dwc3-test"'
```

Result: one qtest executable passed with four subtests; zero failed, skipped,
or timed out. The post-commit QEMU binary SHA-256 is
`430ab49b7c31f1ea0f706ac5991f459ba9bf20ca775e6ac3ec209d852a52a9a5`.

`git diff --check` passed before commit. QEMU `checkpatch.pl --no-signoff`
reported zero errors and one manual warning asking whether the three new files
need a `MAINTAINERS` entry. This downstream focused model has no upstream
maintainer assignment yet.

### Default-machine regression

The QEMU binary was started with `-machine virt` without the opt-in property.
QMP capabilities, `query-status`, and `quit` returned normally; the state was
`prelaunch`. This proves the default machine can still be created and
controlled after the optional model was added.

### Asterinas capability regression

The focused RISC-V kernel test was rebuilt in the persistent development
container and booted manually so that the serial result was observable:

```text
test aster_usb::arch::capability::tests::accepts_extended_capabilities_inside_capability_register_space ... ok
test result: ok. 1 passed; 0 failed; 27 filtered out.
```

The fix accepts a valid xHCI extended-capability pointer at `0x20` when the
operational register block starts at `0x40`. This matches QEMU's xHCI layout
and the xHCI rule that xECP is relative to the capability-register base.

### Guest probe and HID transfer

A bounded 40-second guest run used the committed QEMU model, Asterinas kernel
SHA-256
`f8bd6400109ca196699570c49ea66498dd6da81201d6889db5299c5e7ab55300`,
a QEMU USB keyboard on `usb-bus.0`, and a QEMU USB mouse on `usb-bus.1`.
The serial log contains exactly one of each of these markers:

```text
Detected xHCI 0 0x0100: slots=2, ports=8, interrupters=1, AC64=true
Detected xHCI 1 0x0100: slots=2, ports=8, interrupters=1, AC64=true
USB boot keyboard registered: 0627:0001
USB boot mouse registered: 0627:0001
ASTERINAS_REBOOT_TIMER_READY
```

The guest log also records successful control-transfer events, short packets,
endpoint configuration, and interrupt-driven HID transfer rings. This validates
control and interrupt transfers across both hosts. It is not evidence for bulk
IN/OUT.

### Recovery boundary

A separate bounded reboot run recorded three fresh OpenSBI epochs and one
reboot-timer marker. This verifies QEMU's firmware reset boundary. Direct
kernel boot does not reload the kernel image into RAM after reset, so this is
not claimed as a complete second Asterinas userspace boot.

## Artifacts and limitations

Machine-readable summaries and local logs are under
`target/eic7700-qemu/phase1/`. `SHA256SUMS` covers the kernel, DTB, qtest log,
guest logs, focused ktest log, and default-machine QMP log. `result.json`
records the base/model commits, binary and DTB hashes, subtest count, guest
markers, and limitations.

The remaining Phase 2 acceptance work is a deterministic USB device plus
control, bulk IN/OUT, short-packet, and device-reset tests with payload hashes,
completion-event counts, IRQ counts, and structured results. Physical EIC7700
clock/reset, PHY, and electrical behavior is not modeled or validated here.
