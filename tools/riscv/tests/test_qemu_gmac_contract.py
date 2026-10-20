#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Source-level gate for the EIC7700 GMAC fail-closed QEMU model."""

from __future__ import annotations

import os
from pathlib import Path
import unittest

from tools.riscv.qemu_gmac_contract import (
    QemuGmacContractError,
    parse_qemu_gmac_probe_offsets,
    validate_qemu_gmac_source,
)


DEFAULT_QEMU_SOURCE = Path(
    "/home/ubuntu/.codex/qemu-eic7700-10.2.1/tests/qtest/eic7700-dwc3-test.c"
)


class QemuGmacContractTests(unittest.TestCase):
    def test_parser_extracts_the_six_probe_offsets_in_order(self) -> None:
        source = """
        #define GMAC_MAC_CONFIGURATION 0x0000
        #define GMAC_MAC_INTERRUPT_STATUS 0x00b0
        #define GMAC_MAC_VERSION 0x0110
        #define GMAC_MAC_HW_FEATURE1 0x0120
        #define GMAC_DMA_MODE 0x1000
        #define GMAC_DMA_STATUS 0x1008
        static const uint32_t readonly_offsets[] = {
            GMAC_MAC_CONFIGURATION,
            GMAC_MAC_INTERRUPT_STATUS,
            GMAC_MAC_VERSION,
            GMAC_MAC_HW_FEATURE1,
            GMAC_DMA_MODE,
            GMAC_DMA_STATUS,
        };
        """
        self.assertEqual(
            parse_qemu_gmac_probe_offsets(source),
            (0x0000, 0x00B0, 0x0110, 0x0120, 0x1000, 0x1008),
        )

    def test_parser_rejects_missing_or_reordered_whitelist(self) -> None:
        source = """
        #define GMAC_MAC_CONFIGURATION 0x0000
        #define GMAC_MAC_INTERRUPT_STATUS 0x00b0
        #define GMAC_MAC_VERSION 0x0110
        static const uint32_t readonly_offsets[] = {
            GMAC_MAC_VERSION,
            GMAC_MAC_CONFIGURATION,
            GMAC_MAC_INTERRUPT_STATUS,
        };
        """
        with self.assertRaisesRegex(QemuGmacContractError, "six GMAC"):
            parse_qemu_gmac_probe_offsets(source)

    def test_checked_source_matches_asterinas_and_registers_fail_closed_tests(self) -> None:
        path = Path(os.environ.get("EIC7700_QEMU_DWC3_TEST", DEFAULT_QEMU_SOURCE))
        if not path.is_file():
            self.skipTest(f"QEMU source is unavailable: {path}")
        result = validate_qemu_gmac_source(path.read_text(encoding="utf-8"))
        self.assertEqual(
            result.probe_offsets,
            (0x0000, 0x00B0, 0x0110, 0x0120, 0x1000, 0x1008),
        )
        self.assertEqual(result.gmac_bases, (0x50400000, 0x50410000))
        self.assertTrue(result.has_gated_read_write_check)
        self.assertTrue(result.has_readonly_probe_check)
        self.assertTrue(result.has_mmio_timeout_check)
        self.assertTrue(result.has_platform_sequence_check)
        self.assertTrue(result.has_reset_gate_check)
        self.assertTrue(result.has_phy_absent_check)
        self.assertTrue(result.has_unreachable_descriptor_check)
