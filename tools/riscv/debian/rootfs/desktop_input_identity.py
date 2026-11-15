#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Resolve desktop evdev roles from the input ioctls Asterinas implements."""

from __future__ import annotations

import fcntl
import glob
import os
import stat
import struct
import sys
from collections.abc import Callable


EVIOCGID = 0x80084502
EVIOCGNAME_256 = 0x81004506
EVIOCGPHYS_256 = 0x81004507

KEYBOARD_IDENTITIES = frozenset(
    {
        (3, "usb_boot_keyboard", "xhci/input0"),
        (6, "QEMU Virtio Keyboard", "virtio/input0"),
    }
)
POINTER_IDENTITIES = frozenset(
    {
        (3, "usb_boot_mouse", "xhci/input1"),
        (6, "QEMU Virtio Tablet", "virtio/input0"),
    }
)


def read_identity(path: str) -> tuple[int, str, str]:
    """Read bus, name, and physical path from one character-device evdev node."""

    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:

        def ioctl_bytes(code: int, length: int) -> bytes:
            buffer = bytearray(length)
            fcntl.ioctl(descriptor, code, buffer, True)
            return bytes(buffer)

        bus = struct.unpack_from("=H", ioctl_bytes(EVIOCGID, 8))[0]
        name = ioctl_bytes(EVIOCGNAME_256, 256).split(b"\0", 1)[0].decode("utf-8")
        phys = ioctl_bytes(EVIOCGPHYS_256, 256).split(b"\0", 1)[0].decode("utf-8")
        return bus, name, phys
    finally:
        os.close(descriptor)


def resolve_input_nodes(
    directory: str = "/dev/input",
    *,
    identity_reader: Callable[[str], tuple[int, str, str]] = read_identity,
) -> tuple[str, str] | None:
    """Select one keyboard and pointer, or report missing/ambiguous identities."""

    keyboards: list[str] = []
    pointers: list[str] = []
    for path in sorted(glob.glob(os.path.join(directory, "event*"))):
        try:
            if not stat.S_ISCHR(os.lstat(path).st_mode):
                continue
            identity = identity_reader(path)
        except (OSError, UnicodeError):
            continue
        if identity in KEYBOARD_IDENTITIES:
            keyboards.append(path)
        if identity in POINTER_IDENTITIES:
            pointers.append(path)
    if len(keyboards) > 1 or len(pointers) > 1:
        raise ValueError("ambiguous desktop input device identity")
    if not keyboards or not pointers:
        return None
    if keyboards[0] == pointers[0]:
        raise ValueError("ambiguous desktop input device identity")
    return keyboards[0], pointers[0]


def main() -> int:
    try:
        nodes = resolve_input_nodes(
            os.environ.get("ASTERINAS_DESKTOP_M3_INPUT_DIRECTORY", "/dev/input")
        )
    except ValueError as error:
        print(f"ASTERINAS_DESKTOP_DEVICE_ACCESS failed: {error}", file=sys.stderr)
        return 2
    if nodes is None:
        return 1
    print(*nodes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
