#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import os
from pathlib import Path
import unittest

from tools.riscv.qemu_dwc3_contract import (
    QemuDwc3ContractError,
    validate_qemu_dwc3_source,
)


DEFAULT_QEMU_SOURCE = Path(
    "/home/ubuntu/.codex/qemu-eic7700-10.2.1/tests/qtest/eic7700-dwc3-test.c"
)


class QemuDwc3ContractTests(unittest.TestCase):
    def test_checked_qemu_source_contains_registered_isolation_faults(self) -> None:
        path = Path(os.environ.get("EIC7700_QEMU_DWC3_TEST", DEFAULT_QEMU_SOURCE))
        if not path.is_file():
            self.skipTest(f"QEMU source is unavailable: {path}")
        result = validate_qemu_dwc3_source(path.read_text(encoding="utf-8"))
        self.assertEqual(
            result.functions,
            (
                "test_dwc3_fault_reset_isolates_xhci",
                "test_dwc3_fault_irq_without_event_ring_isolated",
                "test_dwc3_fault_register_read_is_fail_closed",
            ),
        )
        self.assertTrue(result.no_device_or_dma_in_faults)

    def test_fault_function_rejects_device_enumeration(self) -> None:
        source = (
            "static void test_dwc3_fault_reset_isolates_xhci(void) {"
            " qtest_qmp_device_add(qts, \"usb-kbd\", \"kbd0\", \"{}\"); }"
            "static void test_dwc3_fault_irq_without_event_ring_isolated(void) {}"
            "static void test_dwc3_fault_register_read_is_fail_closed(void) {}"
            "qtest_add_func(\"/eic7700/dwc3/fault-reset-isolates-xhci\","
            " test_dwc3_fault_reset_isolates_xhci);"
            "qtest_add_func(\"/eic7700/dwc3/fault-irq-without-event-ring\","
            " test_dwc3_fault_irq_without_event_ring_isolated);"
            "qtest_add_func(\"/eic7700/dwc3/fault-register-read-fail-closed\","
            " test_dwc3_fault_register_read_is_fail_closed);"
        )
        with self.assertRaisesRegex(QemuDwc3ContractError, "enumerates"):
            validate_qemu_dwc3_source(source)


if __name__ == "__main__":
    unittest.main()

