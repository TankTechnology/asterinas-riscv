# Megrez QEMU Basic and Device Matrix Design

## Status

Approved design for the first no-hardware Megrez validation stage. The design is
based on `TankTechnology/asterinas-riscv` commit
`a536ac75917044e4e96f9d871b67941e8b50e1d9` and QEMU 10.2.1 from the local
`asterinas-env:uboot-sim` image.

## Problem

The last controlled Milk-V Megrez run reached PID 1, but the physical board is
not currently available. The downstream therefore needs a repeatable local gate
that exercises as much of the Megrez software contract as QEMU can represent,
without presenting virtual-platform results as physical-board evidence.

The repository already has immutable Sv48/Svade and Sv48/Svadu Megrez contract
profiles, guarded U-Boot `booti` execution, serial-interaction primitives, a
RISC-V firmware-framebuffer path, and several device drivers. These facilities
are fragmented: the guarded runner is headless, the display runners duplicate
boot control, and no maintained target builds a BusyBox controlling-terminal
rootfs and publishes one combined result.

QEMU `virt` uses an NS16550 UART and QEMU 10.2.1 does not expose a mountable DW
APB UART device. The physical Megrez UART must therefore remain a separate
fake-MMIO and DTB contract test rather than being relabelled or falsely claimed
as an end-to-end QEMU result.

## Goals

1. Provide one local command, `make test_riscv_megrez_qemu_basic`, that boots a
   four-hart default-Sv48 Asterinas Image through one guarded U-Boot `booti` and
   reaches an interactive BusyBox shell.
2. Drive a fixed shell command sequence over the serial controlling terminal
   and require a unique completion marker.
3. Adopt a registered QEMU bochs framebuffer through a fixed
   `simple-framebuffer` DTB handoff, capture a screenshot, and audit its pixels.
4. Run isolated probes for several QEMU devices already supported by Asterinas:
   virtio block, entropy, network, input, GPU, and NVMe.
5. Retain compatibility runs for the Sv48/Svadu envelope, generic Sv39, and
   QEMU SiFive U.
6. Publish immutable, run-owned evidence that distinguishes QEMU execution,
   contract-only validation, unsupported devices, and untested physical-board
   behavior.
7. Reuse the guarded runner's typed profiles, input identity checks, timeouts,
   process-group cleanup, and serial auditing. Do not introduce a second ad hoc
   U-Boot controller.

## Non-goals

- Emulating the EIC7700 SoC, its clocks, reset controller, cache controller,
  PCIe topology, DWC3 integration, or HDMI scanout hardware.
- Claiming that a QEMU PASS is a current Megrez board PASS.
- Adding a private QEMU machine or carrying a QEMU patch set in this stage.
- Merging the open Megrez USB/xHCI branch or treating `qemu-xhci + usb-kbd` as
  supported before that work is separately reviewed and integrated.
- Booting a full NixOS desktop or browser in this stage. The gate establishes
  the smaller shell and framebuffer foundation required by those later stages.
- Accepting arbitrary QEMU arguments, arbitrary guest commands, or reusable
  output directories from callers.

## Fidelity model

Every result has one of these scopes:

| Scope | Meaning |
| --- | --- |
| `QEMU_EXECUTION` | The declared profile and device were executed by QEMU and reached the registered terminal condition. |
| `CONTRACT_ONLY` | Host or kernel tests validated a board-derived contract, but QEMU did not model the physical device. |
| `UNSUPPORTED` | The exact device is deliberately absent from the current mainline integration. This is a registered fact, not a runtime skip. |
| `PHYSICAL_UNTESTED` | The result requires a future controlled Megrez session and is not inferred from other scopes. |

The top-level report must always state that the matrix is a contract
approximation. A QEMU execution result cannot upgrade a physical item from
`PHYSICAL_UNTESTED`.

## Architecture

### Registered device contracts

Extend the immutable QEMU profile layer with typed, registered device sets.
Each set owns the exact QEMU arguments, required guest observations, and
evidence requirements. The command line renderer accepts only registered sets;
there is no generic extra-argument escape hatch.

The existing profile identity remains a composition of machine, boot flow, and
validation scenario. A device set is an additional immutable input to launch
rendering and result identity. Existing profiles render byte-for-byte identical
commands when no device set is selected.

### BusyBox initramfs

Add a reproducible builder for a static riscv64 BusyBox rootfs. Its `/init`:

1. mounts `proc`, `sysfs`, and `devtmpfs`, treating any mount failure as a
   shell-gate failure;
2. opens `/dev/ttyS0` as the controlling terminal;
3. creates `/tmp` with mode 01777;
4. prints one fixed shell-ready line;
5. executes BusyBox `ash` without a login/password path.

The guarded interaction waits for both the ready line and shell prompt before
sending a fixed command program. The program validates `pwd`, `ls`, `cd`, file
creation/readback/removal, procfs, and basic mount visibility, then emits one
fixed completion line. No caller-provided shell text is accepted.

### Display handoff and capture

The primary Megrez approximation selects a registered bochs-display contract.
The contract adds the fixed QEMU device and a run-private monitor socket. The
U-Boot command renderer injects a fixed, validated `simple-framebuffer` node
using the reviewed bochs BAR, size, resolution, stride, and `x8r8g8b8` format.
It prints the resulting node before the single `booti` command.

After shell completion, the controller asks the monitor for a PPM screendump.
The display auditor validates P6 structure, dimensions, payload length,
non-black pixel count, color diversity, and a minimum non-background bounding
box. It also requires the serial framebuffer-registration marker. A screenshot
alone is not sufficient evidence.

### Device probes

Device probes run independently so one failure identifies one subsystem. Each
probe uses its own QEMU process, snapshot disks, output directory, and result.
The initial matrix is:

| Probe | QEMU model | Required evidence |
| --- | --- | --- |
| Boot block | `virtio-blk-device` | U-Boot reads the boot disk; the guest reaches the shell. |
| Scratch block | second `virtio-blk-device` | Guest writes, reads, compares, and removes a token on a fresh scratch filesystem. |
| Entropy | `virtio-rng-device` | Driver registration plus a bounded non-empty read from the registered entropy device. |
| Network | `virtio-net-device` with user networking | Driver/link evidence and a deterministic guest-to-host request to a run-private host endpoint. |
| Firmware display | `bochs-display` | DTB proof, framebuffer registration, and pixel-audited PPM. |
| Input enumeration | `virtio-keyboard-device` | Virtio input registration and handler connection; injected key navigation is outside this stage. |
| GPU | `virtio-gpu-device` | Device registration and a separate 2D scanout/framebuffer smoke result. |
| NVMe | `nvme` plus one namespace | PCI/NVMe enumeration and bounded scratch read/write verification. |

`qemu-xhci + usb-kbd` is recorded as `UNSUPPORTED` until the USB stack is
integrated. `ramfb` and additional QEMU machines are future candidates, not
silent skips in this matrix.

### Compatibility profiles

The matrix keeps these independent boots:

| Profile | Purpose |
| --- | --- |
| `megrez-sv48-svade-fast`, 4 harts | Primary shell and display gate; software-managed A/D path. |
| `megrez-sv48-svadu-fast`, 4 harts | Hardware A/D update envelope. |
| `generic-sv39` | Sv39 compatibility and generic virt baseline. |
| `sifive-u-asterinas-smoke` | SiFive UART and MMC transport coverage. |

Only the primary profile carries the complete device set. Compatibility boots
remain small so their failures identify profile or transport regressions.

### DW APB UART contract

The matrix invokes the existing Rust fake-MMIO tests for register width,
register shift, transmit readiness, receive paths, busy detection, interrupt
masking, and bounded failure behavior. It also validates the reviewed Megrez
DTB properties and firmware-selected `stdout-path` classification.

This result is reported as `CONTRACT_ONLY`. The QEMU serial shell uses the
virtual platform's NS16550 and cannot satisfy the DW APB item.

## Data flow

```text
current source commit
  -> default-Sv48 primary Image + Sv39 compatibility Image
  -> one static BusyBox initramfs shared only after its identity is verified
  -> hash and validate immutable inputs
  -> prepare registered U-Boot disk and generated payload DTB
  -> start one registered QEMU profile/device-set process
  -> issue guarded U-Boot commands and exactly one booti
  -> observe ordered kernel/rootfs/shell milestones
  -> send fixed BusyBox command interaction
  -> capture serial, device observations, and framebuffer PPM
  -> audit profile, device, shell, image, and cleanup results
  -> atomically publish JSON summaries and SHA256SUMS
```

The top-level matrix driver aggregates child result files. It does not infer a
PASS from console text alone and does not rewrite an individual child result.

## Evidence layout

Each invocation uses a caller-independent run ID and publishes below:

```text
target/megrez-qemu-basic/results/<run-id>/
  matrix.json
  summary.txt
  manifest.json
  SHA256SUMS
  primary-svade/
    result.json
    serial.log
    marker-event.txt
    dtb-audit.json
    display-audit.json
    screenshot.ppm
    artifacts.json
  svadu/
  generic-sv39/
  sifive-u/
  devices/
    scratch-block/
    entropy/
    network/
    input/
    gpu/
    nvme/
  contracts/
    dw-apb-uart.json
```

Every child result records the source commit, QEMU version, profile and device
set identities, input SHA-256 values, terminal marker, cleanup state, scope,
and verdict. `SHA256SUMS` is finalized for every available regular evidence
file on success or failure.

## Failure handling and safety

- Allocate each result directory exactly once; reject symlinks and reuse.
- Stage all mutable launch inputs and verify their identities before and after
  QEMU.
- Use snapshot mode for boot and scratch disks so probes cannot mutate shared
  images.
- Use a run-private monitor socket contained below the run directory.
- Reject commas and path escapes in structured QEMU path arguments.
- Apply finite startup, command, boot, post-terminal, and termination limits.
- Kill and reap the whole QEMU process group on timeout, parser failure, or
  monitor failure.
- Treat a second `booti`, reordered milestone, panic after the terminal marker,
  changed input, missing screenshot, or residual child as a failure.
- Publish retained evidence and final checksums from one outer lifecycle even
  when preparation or auditing raises an exception.
- Classify `UNSUPPORTED` only from the checked-in registry. A device that was
  launched and failed is `FAIL`, never `UNSUPPORTED` or `SKIP`.

## Test strategy

Implementation follows test-driven development:

1. Unit tests cover registered device sets, immutable command rendering,
   unchanged legacy argv, initramfs layout, fixed shell interaction, monitor
   protocol, PPM parsing, result classification, and evidence finalization.
2. Dry-run tests assert the complete matrix composition, four-hart Sv48
   profiles, storage transports, device isolation, and absence of arbitrary
   QEMU arguments.
3. Existing U-Boot/booti unit tests remain green and guard legacy behavior.
4. Component runs exercise the primary shell/display gate and every launched
   device probe separately.
5. The final local matrix runs in the project container. Remote CI is not used
   as a completion gate.

## Acceptance criteria

The first no-hardware stage is complete only when:

- `make test_riscv_megrez_qemu_basic` returns zero on the exact reviewed head;
- the primary Sv48/Svade run uses four harts, one U-Boot `booti`, reaches the
  BusyBox controlling-terminal shell, and completes the fixed command program;
- the Sv48/Svadu, generic Sv39, and SiFive U compatibility boots pass;
- the firmware display run produces a serial-confirmed, pixel-audited PPM;
- virtio block, entropy, network, and input probes pass;
- GPU and NVMe produce actual executed PASS or FAIL results, with failures
  fixed or explicitly left as visible blockers before merge;
- the DW APB UART contract tests pass and remain labelled `CONTRACT_ONLY`;
- xHCI/USB keyboard is labelled `UNSUPPORTED`, not PASS;
- every launched QEMU process is reaped and every result has finalized
  checksums; and
- documentation states that EIC7700 hardware and current Megrez board behavior
  remain `PHYSICAL_UNTESTED`.

## Delivery order

1. Add typed display/device contracts and preserve existing runner behavior.
2. Add the reproducible BusyBox shell initramfs and fixed serial interaction.
3. Integrate framebuffer commands, private monitor capture, and pixel audit.
4. Land the primary Sv48/Svade basic gate.
5. Add isolated block, entropy, network, input, GPU, and NVMe probes.
6. Add compatibility-profile aggregation and DW APB contract reporting.
7. Run the complete local matrix, review the evidence, and update the live
   Megrez status without upgrading physical-board claims.
