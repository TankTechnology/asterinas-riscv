# Linux USB Boot Keyboard Oracle

This directory contains a maintenance-only generation oracle for Asterinas's
USB HID Boot Keyboard compatibility work.
It creates a temporary Linux UHID keyboard, sends complete eight-byte reports,
and records the resulting evdev events in
`kernel/comps/usb/src/keyboard_linux_vectors.rs`.

Linux, UHID, `hid-tools`, and `evdev` are generation dependencies only.
They are not Asterinas runtime dependencies, and the generated Rust file is
private kernel-test data compiled only by the `ktest` configuration.
Normal unit tests are offline and import neither third-party package.

The oracle intentionally covers fixed eight-byte USB Boot Keyboard reports.
It is not a general HID report descriptor parser.
Future Report Protocol work should reuse a maintained parser,
follow Linux HID/input layering,
and add Linux-derived scenarios here before changing runtime behavior.

## Environment

Create a disposable virtual environment and install the exact pinned versions:

```bash
python3 -m venv /tmp/asterinas-usb-hid-oracle
/tmp/asterinas-usb-hid-oracle/bin/python -m pip install \
    -r tools/usb-hid/requirements.txt
```

The invoking user needs read/write access to `/dev/uhid` and to the evdev node
created under `/dev/input`.
Use a privileged development container or an appropriate udev rule; do not
make those device files world-writable.

Generate the fixture from the repository root:

```bash
/tmp/asterinas-usb-hid-oracle/bin/python \
    tools/usb-hid/boot_keyboard_oracle.py
```

Regenerate from the current Linux host and fail if its exact output differs
from the committed fixture:

```bash
/tmp/asterinas-usb-hid-oracle/bin/python \
    tools/usb-hid/boot_keyboard_oracle.py --check
```

`--output PATH` overrides the default fixture path.
Publication uses a same-directory temporary file, flushes and synchronizes it,
and atomically replaces the destination.
Check mode never rewrites the destination, including when it is missing or
different.

## Event normalization

The fixture preserves every `EV_KEY` event in the exact order read from evdev.
That order, followed by a real terminal `EV_SYN/SYN_REPORT` with value zero, is
the authoritative Linux behavior.
An unchanged report produces an empty event list.

Linux also emits data that is not part of the compatibility contract:

- `EV_MSC/MSC_SCAN` repeats information already represented by `EV_KEY` and is
  dropped.
- `EV_SYN/SYN_REPORT` with value one is a synthetic input-core buffer flush or
  autorepeat boundary, not the HID report's terminal sync, and is dropped.
- An empty terminal frame is dropped.
- Any other event makes generation fail rather than silently broadening the
  oracle.

Capture has a hard 200 ms deadline starting at report injection.
A quiet report returns no events; a deadline reached after any partial frame is
an error.
Incoming autorepeat batches cannot extend that deadline.

Linux's input core reserves a small estimated value buffer for keyboard
packets.
Exactly four changed keys produce eight `MSC_SCAN`/`EV_KEY` values, fill that
buffer, and can leave only a synthetic value-one sync visible.
The scenarios preserve their intended ordinary-key behavior while avoiding
that unobservable terminal boundary:

- `all_modifiers` includes A with all eight modifier bits.
- `chord_4` includes Left Ctrl while retaining four ordinary key slots.
- `replace_subset` stages `ABC -> LeftCtrl+ADE -> LeftCtrl-only -> empty`.
- `reserved_byte_change` uses
  `LeftCtrl -> LeftCtrl+reserved(0x7f) -> empty`, proving that a reserved-only
  change preserves held key state without entering a second autorepeat window.

The normalizer never invents a value-zero terminal sync and never accepts a
partial frame.

## Provenance and drift

The generated header records the canonical generation command, Linux kernel
release, pinned tool versions, USB HID and HID Usage Tables versions, and
SHA-256 hashes of both the descriptor and canonical scenario JSON.
It deliberately contains no timestamp or absolute path.

`--check` performs a fresh UHID capture and byte-for-byte comparison.
Changes in Linux mappings, event ordering, kernel version, tools, descriptor,
scenarios, or formatting therefore produce explicit drift.
Review the cause and regenerate intentionally; do not hand-edit the fixture.

## References

- [USB Device Class Definition for HID 1.11](https://www.usb.org/sites/default/files/hid1_11.pdf)
- [HID Usage Tables 1.7](https://usb.org/document-library/hid-usage-tables-17)
- [Linux UHID documentation](https://docs.kernel.org/hid/uhid.html)
- [Linux HID introduction](https://docs.kernel.org/hid/hidintro.html)
- [Asterinas USB Boot Keyboard compatibility design](../../docs/superpowers/specs/2026-07-23-usb-boot-keyboard-compatibility-design.md)
