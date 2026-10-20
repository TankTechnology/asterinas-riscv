#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the shared EIC7700 MMIO and fail-closed contract."""

from __future__ import annotations

import unittest

from tools.riscv.eic7700_platform_contract import (
    DMA_DIRECTION_DEVICE_TO_GUEST,
    DMA_DIRECTION_GUEST_TO_DEVICE,
    EIC7700,
    FailureSemantics,
)


class Eic7700PlatformContractTests(unittest.TestCase):
    def test_gmac1_contract_freezes_probe_identity_and_platform_dependencies(self) -> None:
        gmac = EIC7700.peripheral("gmac-1")
        self.assertEqual((gmac.base, gmac.size, gmac.irq), (0x50410000, 0x10000, 70))
        self.assertEqual(
            gmac.probe_offsets,
            (0x0000, 0x00B0, 0x0110, 0x0120, 0x1000, 0x1008),
        )
        self.assertEqual(gmac.clock_dependencies, (
            ("syscrg", 0x148),
            ("syscrg", 0x14C),
            ("hsp", 0x200),
            ("hsp", 0x208),
        ))
        self.assertEqual(gmac.reset_dependencies, (
            ("syscrg", 0x15C),
            ("syscrg", 0x41C),
        ))

    def test_gmac_register_reset_values_and_dma_directions_are_explicit(self) -> None:
        gmac = EIC7700.peripheral("gmac-1")
        self.assertEqual(gmac.reset_value(0x0110), 0x52)
        self.assertEqual(gmac.reset_value(0x0120), 0x186)
        self.assertEqual(gmac.reset_value(0x0000), 0)
        self.assertEqual(gmac.dma_directions, (
            DMA_DIRECTION_GUEST_TO_DEVICE,
            DMA_DIRECTION_DEVICE_TO_GUEST,
        ))

    def test_gmac1_fail_closed_contract_suppresses_side_effects(self) -> None:
        failure = EIC7700.peripheral("gmac-1").failure
        self.assertEqual(failure.unavailable_read_value, 0xFFFFFFFF)
        self.assertEqual(failure.suppressed_side_effects, (
            "phy",
            "mdio",
            "dma",
            "tx",
            "rx",
            "irq",
        ))
        self.assertTrue(failure.drop_writes)
        self.assertTrue(failure.bounded)

    def test_contract_rejects_unregistered_peripherals(self) -> None:
        with self.assertRaises(KeyError):
            EIC7700.peripheral("gmac-2")

    def test_dwc3_contract_has_identity_reset_and_event_dma(self) -> None:
        dwc3 = EIC7700.peripheral("dwc3-1")
        self.assertEqual((dwc3.base, dwc3.size, dwc3.irq), (0x50490000, 0x10000, 86))
        self.assertEqual(dwc3.reset_value(0xC120), 0x5533330A)
        self.assertEqual(dwc3.reset_value(0xC110), 0x30C13004)
        self.assertEqual(dwc3.dma_directions, (
            DMA_DIRECTION_GUEST_TO_DEVICE,
            DMA_DIRECTION_DEVICE_TO_GUEST,
        ))
        self.assertEqual(dwc3.failure, FailureSemantics(
            unavailable_read_value=0,
            drop_writes=False,
            suppressed_side_effects=("xhci", "device-enumeration", "irq"),
            bounded=True,
        ))


if __name__ == "__main__":
    unittest.main()
