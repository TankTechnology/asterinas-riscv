# RISC-V USB Boot Mouse Design

## Goal

Add the first usable pointer path for the Asterinas RISC-V desktop:

```text
xHCI interrupt-IN -> USB HID Boot Mouse -> evdev -> Xorg
```

The first gate uses QEMU `virt`, `qemu-xhci`, `usb-kbd`, and `usb-mouse` with
`smp=4`. After it passes, the same kernel is tested on the Megrez board with
the attached wired USB mouse, existing USB keyboard, HDMI display, and serial
console.

Success means that keyboard input remains operational while the mouse exposes
relative X/Y movement, left/right/middle buttons, and a `SYN_REPORT` boundary
through a second evdev device. Wheel support is accepted when the device sends
a compatible fourth boot-report byte, but it is not required for the initial
three-byte Boot Mouse gate.

## Current Constraint

The current OSTD USB implementation owns one xHCI host inside
`PollingUsbKeyboard`. Opening a second independent mouse object would acquire
the same MMIO range and reset the same controller. That would violate the
single-owner controller contract and break the working keyboard path.

Keyboard and mouse discovery, device ownership, event pumping, IRQ enablement,
and failure retention must therefore share one host lifecycle.

## Considered Approaches

### One shared xHCI HID host — selected

Generalize the existing OSTD object into one bounded HID host that owns the
xHCI controller and up to one Boot Keyboard and one Boot Mouse. Each device
retains its own CrabUSB `Device`, interrupt-IN endpoint, and fixed-depth report
queue. The existing IRQ worker drains both queues after each controller event.

This preserves the proven MMIO, DMA, PCI/DWC3, PLIC, timeout, and leak-on-unsafe-
teardown contracts. It is the smallest design that supports a real keyboard
and mouse simultaneously.

### A second independent mouse controller object — rejected

This duplicates xHCI initialization and MMIO ownership. It can reset or race
the controller already serving the keyboard and is not a safe extension.

### Generic HID report-descriptor support — deferred

Generic HID would cover gaming mice, composite devices, extra buttons, and
vendor-specific reports. It is substantially broader than the basic desktop
milestone. The first implementation accepts only HID class 3, subclass 1,
protocol 2 and the Boot Mouse report format.

## Architecture

### OSTD controller and USB ownership

Refactor the current keyboard-specific host without adding another controller
lifecycle:

- retain the existing validated xHCI MMIO and DMA setup;
- enumerate until both a Boot Keyboard and Boot Mouse are found, or until the
  bounded discovery deadline expires;
- keep keyboard-only operation valid when no mouse is attached;
- claim each selected interface and issue HID `SET_PROTOCOL(BOOT)`;
- keep one interrupt-IN endpoint and bounded report queue per selected device;
- enable and disable xHCI interrupts once for the shared host;
- pump the xHCI event handler once and then drain both report queues;
- preserve DMA-visible objects on transfer failures when CrabUSB cannot prove
  a safe controller shutdown.

The report queue becomes const-generic over report capacity while preserving
the existing eight-slot bound and FIFO/replenishment tests. Keyboard requires
exactly eight bytes. Mouse accepts three bytes and may expose an optional
fourth wheel byte when the endpoint completes four bytes.

### Safe-Rust input integration

Add a USB mouse module in `kernel/comps/usb` that:

- registers `BUS_USB` identity and KEY/REL/SYN capabilities;
- advertises `BTN_LEFT`, `BTN_RIGHT`, `BTN_MIDDLE`, `REL_X`, `REL_Y`, and
  optionally `REL_WHEEL`;
- compares button state with the previous report and emits only transitions;
- sign-extends X/Y bytes and keeps USB Y direction consistent with Linux evdev;
- emits one `SYN_REPORT` after every non-empty decoded report.

The RISC-V USB worker retains both registered input devices and submits decoded
keyboard and mouse events from the same deferred IRQ loop. A mouse failure must
not silently unregister or corrupt the keyboard device.

### QEMU gate

The focused QEMU gate must attach exactly one `qemu-xhci`, `usb-kbd`, and
`usb-mouse`; it must not use VirtIO input or i8042 fallback. A static guest
probe identifies the USB pointer by evdev capability, then requires:

- relative X and Y movement;
- left-button press and release;
- a complete `SYN_REPORT` packet after injected events;
- the existing keyboard marker still passing in the same boot;
- no panic/fatal marker and bounded process-group cleanup.

QEMU input is injected through its monitor, with total deadlines and complete
serial evidence. The gate runs at `smp=4` only.

### Megrez gate

After QEMU passes, build the normal Sv39/SMP4 kernel and use the existing
Megrez U-Boot/HTTP boot path. The serial gate requires xHCI discovery, both USB
device registrations, and two evdev nodes. HDMI acceptance is manual and
observable: the Xorg pointer moves, clicks a window, and the keyboard still
types in xterm.

No Linux kernel is used to boot the desktop. Linux may only be used as an
offline reference for the attached mouse's descriptors if Asterinas logs are
insufficient.

## Failure Policy

- Reject ambiguous duplicate Boot Mouse interfaces rather than selecting by
  enumeration accident.
- A missing mouse logs a stable reason but preserves the working keyboard.
- Short reports, oversized reports, failed transfers, and controller timeout
  are explicit errors; they are not decoded as input.
- No hotplug claim is made in this milestone. Devices are expected to be
  attached before boot.
- Generic report descriptors, wireless/Bluetooth transports, horizontal
  wheel, extra buttons, and runtime reconnect are follow-up work.

## Verification and Scope

Implementation follows test-first slices:

1. pure Boot Mouse interface selection and report decoding;
2. const-generic bounded report queues and shared-host ownership;
3. safe-Rust evdev registration and combined keyboard/mouse worker;
4. one focused QEMU xHCI runtime gate at `smp=4`;
5. one Megrez real-board boot and HDMI interaction check.

Only the focused OSTD/USB kernel checks, the new QEMU gate, and the final board
boot are required. Remote CI and unrelated full-system suites are excluded.
