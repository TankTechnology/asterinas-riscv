#!/usr/bin/env python3

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from qemu_recovery_faults import (  # noqa: E402
    RecoveryFault,
    RecoveryFaultError,
    build_fault_matrix_receipt,
    classify_fault,
    run_fault,
    run_fault_matrix,
)


class QemuRecoveryFaultTests(unittest.TestCase):
    def test_all_faults_are_classified_and_contained_without_retry(self) -> None:
        runs = run_fault_matrix()
        self.assertEqual(
            {run.fault for run in runs},
            set(RecoveryFault),
        )
        for run in runs:
            self.assertTrue(run.result.timed_out)
            self.assertTrue(run.result.cleanup_complete)
            self.assertFalse(run.result.recovery_complete)
            expected_booti = 0 if run.fault is RecoveryFault.UBOOT_WAIT else 1
            self.assertEqual(run.result.booti_sent_count, expected_booti)
            self.assertEqual(run.receipt["recovery_state"], "not-recovered")
            self.assertEqual(run.receipt["execution_scope"], "host-serial-peer")
            self.assertEqual(run.receipt["watchdog_state"], "not-modeled")
            self.assertEqual(
                run.receipt["boot_ids"], ["01234567-89ab-cdef-0123-456789abcdef"]
            )
            self.assertTrue(run.receipt["contained"])
            self.assertEqual(run.receipt["serial_bytes"], len(run.serial))

        by_fault = {run.fault: run.receipt["classification"] for run in runs}
        self.assertEqual(by_fault[RecoveryFault.TIMER_DISABLED], "kernel-hang:timer-disabled")
        self.assertEqual(
            by_fault[RecoveryFault.SBI_RESET_FAILURE], "sbi-reset-failure"
        )
        self.assertEqual(
            by_fault[RecoveryFault.MMIO_PERMANENT_BLOCK],
            "kernel-hang:mmio-permanent-block",
        )
        self.assertEqual(by_fault[RecoveryFault.UBOOT_WAIT], "uboot-wait")

    def test_sbi_failure_is_recovery_timeout_after_single_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run = run_fault(RecoveryFault.SBI_RESET_FAILURE, Path(root))
        self.assertEqual(run.result.failure, "recovery-timeout:opensbi")
        self.assertIn(b"ASTERINAS_REBOOT_TIMER_READY", run.serial)
        self.assertEqual(run.receipt["classification"], "sbi-reset-failure")

    def test_missing_marker_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run = run_fault(RecoveryFault.TIMER_DISABLED, Path(root))
        with self.assertRaises(RecoveryFaultError):
            classify_fault(RecoveryFault.MMIO_PERMANENT_BLOCK, run.result, run.serial)

    def test_matrix_receipt_preserves_scope_and_no_recovery_contract(self) -> None:
        runs = run_fault_matrix()
        receipt = build_fault_matrix_receipt(runs)
        self.assertEqual(
            receipt["schema"], "asterinas.qemu-recovery-fault-matrix-receipt.v1"
        )
        self.assertEqual(receipt["execution_scope"], "host-serial-peer")
        self.assertEqual(receipt["watchdog_state"], "not-modeled")
        self.assertFalse(receipt["passed"])
        self.assertEqual(receipt["fault_count"], len(RecoveryFault))
        self.assertEqual(
            [item["fault"] for item in receipt["faults"]],
            [fault.value for fault in RecoveryFault],
        )
        self.assertTrue(
            all(
                item["contained"]
                and item["recovery_state"] == "not-recovered"
                and item["booti_sent_count"] <= 1
                for item in receipt["faults"]
            )
        )


if __name__ == "__main__":
    unittest.main()
