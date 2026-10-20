#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import ctypes
import unittest
from pathlib import Path

from tools.riscv.debian.rootfs.desktop_drm_libdrm_probe import DrmDevice, DrmVersion


class DesktopDrmLibdrmProbeAbiTests(unittest.TestCase):
    def test_virtio_bus_compatibility_accepts_libdrm_pci_result(self) -> None:
        source = Path(
            "tools/riscv/debian/rootfs/desktop_drm_libdrm_probe.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'emit("DRM_LIBDRM_DEVICE PASS bustype=pci-compat transport=virtio")',
            source,
        )
        self.assertIn('bustype == 0x10', source)
        self.assertIn('stage=device result=', source)

    def test_public_drm_version_layout_keeps_pointer_offsets(self) -> None:
        # libdrm's drmVersion (xf86drm.h) uses int lengths. The kernel ioctl
        # struct uses size_t, but the probe consumes libdrm's heap object.
        fields = dict(DrmVersion._fields_)
        self.assertIs(fields["name_len"], ctypes.c_int)
        self.assertIs(fields["date_len"], ctypes.c_int)
        self.assertIs(fields["desc_len"], ctypes.c_int)
        self.assertEqual(DrmVersion.name.offset, 16)
        self.assertEqual(DrmVersion.date.offset, 32)
        self.assertEqual(DrmVersion.desc.offset, 48)

    def test_device_layout_matches_libdrm_64_bit_prefix(self) -> None:
        self.assertEqual(DrmDevice.nodes.offset, 0)
        self.assertEqual(DrmDevice.available_nodes.offset, 8)
        self.assertEqual(DrmDevice.bustype.offset, 12)
        self.assertEqual(DrmDevice.businfo.offset, 16)
        self.assertEqual(DrmDevice.deviceinfo.offset, 24)


if __name__ == "__main__":
    unittest.main()
