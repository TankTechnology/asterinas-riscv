# Debian RISC-V Input Gate Design

## Goal

Establish a deterministic QEMU gate that proves the Asterinas RISC-V VirtIO
keyboard path is suitable for a Debian userspace before the project starts
assembling a full Debian desktop image.

The first milestone validates the kernel-facing input contract. The probe is
distro-neutral so the same binary and marker protocol can also run inside a
Debian root filesystem without changing the kernel test.

## Context

The current desktop image is a hand-assembled Asterinas initramfs. It uses a
Debian-compatible RISC-V glibc toolchain, but it is not a Debian root
filesystem. Xorg currently names `/dev/input/event1` explicitly because the
tablet registers before the keyboard.

Debian 13 publishes official `riscv64` packages and `generic`/`nocloud` cloud
images. That makes Debian the practical distribution target, but booting the
whole cloud disk immediately would mix input validation with root block,
stage-2 init, package activation, and systemd compatibility.

## Approaches Considered

### 1. Distro-neutral input gate first (selected)

Boot a minimal static RISC-V `/init`, scan all event nodes by capability, and
inject a fixed key sequence through QEMU. This isolates the input contract,
does not depend on event-node numbering, and can be reused unchanged in a
Debian root filesystem.

### 2. Implement Linux input sysfs and uevents first

This would let udev and libinput discover devices automatically. It is needed
for full hotplug integration, but it crosses the input core, sysfs, kobject
uevent, and device-removal subsystems. It is too broad for the first Debian
keyboard milestone.

### 3. Boot an official Debian cloud disk first

This gives the most realistic userspace immediately, but failures would not
distinguish the keyboard path from root-device mounting, stage-2 init, systemd,
or disk-image layout problems. It follows the isolated input gate.

## Architecture

The gate has four focused pieces:

1. `input_gate_init.c` is a static guest program used as `/init`. It scans
   `/dev/input/event0` through `/dev/input/event31`, queries event/key
   capabilities with Linux-compatible evdev ioctls, and selects the device
   that advertises the required keyboard keys. It never assumes a minor
   number.
2. `build_input_gate.sh` cross-compiles the guest program and creates a small,
   deterministic raw `newc` initramfs.
3. `input_gate.py` prepares a private boot disk, boots through the existing
   U-Boot `booti` chain with a VirtIO tablet followed by a VirtIO keyboard,
   waits for a guest-ready marker, injects the reviewed key sequence, and
   classifies the transcript.
4. `test_debian_input_gate.py` validates the host-side command, key sequence,
   marker classification, and build contract without launching QEMU.

The gate defaults to four vCPUs, matching the repository's preferred RISC-V
test configuration.

## Event Contract

The host injects these actions in order:

1. `a`
2. `shift-b`
3. `backspace`
4. `ctrl-c`

The guest accepts unrelated synchronization events but requires each key and
modifier press/release pair. It prints a ready marker only after identifying a
keyboard by capabilities. It prints a pass marker only after the complete
ordered contract is observed.

The result is a failure if the ready marker or pass marker is absent, QEMU
exits early, the timeout expires, or the transcript contains a kernel panic.

## Isolation and Safety

- Build and run artifacts live below `target/debian-riscv/input-gate/`.
- The prepared U-Boot cache may be reused, but the gate creates its own boot
  disk and monitor socket.
- Existing desktop disks and prior evidence are never overwritten.
- The runner terminates the complete QEMU process group on every exit path.
- No network access is required by the input gate.

## Verification

The implementation follows red-green TDD for host-side contracts. Completion
requires:

- all new Python unit tests pass;
- the guest source cross-compiles as a static RISC-V executable;
- the generated initramfs contains only the reviewed guest payload;
- a local QEMU SMP=4 run reaches both ready and pass markers;
- the run records the serial transcript and a machine-readable result below
  the gate's target directory.

Previously completed USB HID oracle and OSTD KTest suites are not rerun because
their inputs are unchanged and this milestone exercises the VirtIO path.

## Follow-up Milestones

After this gate passes:

1. extract a minimal Debian 13 `riscv64` userspace from official packages;
2. run the same input probe against Debian's libc and device nodes;
3. start Xorg with an explicit evdev configuration as the compatibility path;
4. add input sysfs/uevent support for udev/libinput discovery and hotplug;
5. validate the existing DWC3 USB keyboard path on Megrez hardware when the
   board is available.
