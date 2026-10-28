#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Desktop evdev identity tests split from the rootfs contract suite."""

from __future__ import annotations

import unittest
from unittest import mock

from tools.riscv.debian.rootfs import desktop_input_identity


class DesktopInputIdentityTests(unittest.TestCase):
    def test_reads_only_supported_evdev_ioctls_and_closes_descriptor(self) -> None:
        def ioctl(_descriptor: int, code: int, buffer: bytearray, mutate: bool) -> None:
            self.assertTrue(mutate)
            payloads = {
                0x80084502: b"\x03\0\x01\0\x02\0\x03\0",
                0x81004506: b"usb_boot_keyboard\0",
                0x81004507: b"xhci/input0\0",
            }
            buffer[: len(payloads[code])] = payloads[code]

        with (
            mock.patch.object(
                desktop_input_identity.os, "open", return_value=42
            ) as opened,
            mock.patch.object(desktop_input_identity.os, "close") as closed,
            mock.patch.object(
                desktop_input_identity.fcntl, "ioctl", side_effect=ioctl
            ) as queried,
        ):
            self.assertEqual(
                desktop_input_identity.read_identity("/dev/input/event9"),
                (3, "usb_boot_keyboard", "xhci/input0"),
            )
        self.assertEqual(opened.call_args.args[0], "/dev/input/event9")
        self.assertEqual(
            [call.args[1] for call in queried.call_args_list],
            [
                0x80084502,
                0x81004506,
                0x81004507,
            ],
        )
        closed.assert_called_once_with(42)

    def test_both_physical_evdev_enumeration_orders_keep_the_same_roles(self) -> None:
        physical = (
            (3, "usb_boot_keyboard", "xhci/input0"),
            (3, "usb_boot_mouse", "xhci/input1"),
        )
        for identities, expected in (
            (physical, ("/dev/null", "/dev/zero")),
            (physical[::-1], ("/dev/zero", "/dev/null")),
        ):
            with (
                self.subTest(identities=identities),
                mock.patch.object(
                    desktop_input_identity.glob,
                    "glob",
                    return_value=["/dev/null", "/dev/zero"],
                ),
            ):
                observed = dict(zip(("/dev/null", "/dev/zero"), identities))
                self.assertEqual(
                    desktop_input_identity.resolve_input_nodes(
                        identity_reader=observed.__getitem__
                    ),
                    expected,
                )

    def test_virtio_evdev_and_duplicate_or_missing_identities(self) -> None:
        keyboard = (6, "QEMU Virtio Keyboard", "virtio/input0")
        pointer = (6, "QEMU Virtio Tablet", "virtio/input0")
        with mock.patch.object(
            desktop_input_identity.glob,
            "glob",
            return_value=["/dev/null", "/dev/zero"],
        ):
            for identities, expected in (
                ((keyboard, pointer), ("/dev/null", "/dev/zero")),
                ((keyboard, keyboard), None),
                ((pointer, pointer), None),
                (((3, "usb_boot_keyboard", "wrong/port"), pointer), None),
            ):
                if identities == (keyboard, keyboard) or identities == (
                    pointer,
                    pointer,
                ):
                    with (
                        self.subTest(identities=identities),
                        self.assertRaisesRegex(
                            ValueError, "ambiguous desktop input device identity"
                        ),
                    ):
                        observed = dict(zip(("/dev/null", "/dev/zero"), identities))
                        desktop_input_identity.resolve_input_nodes(
                            identity_reader=observed.__getitem__
                        )
                else:
                    observed = dict(zip(("/dev/null", "/dev/zero"), identities))
                    with self.subTest(identities=identities):
                        self.assertEqual(
                            desktop_input_identity.resolve_input_nodes(
                                identity_reader=observed.__getitem__
                            ),
                            expected,
                        )


if __name__ == "__main__":
    unittest.main()
