#!/usr/bin/env python3

from __future__ import annotations

import sys
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from qemu_fallback_receipt import (  # noqa: E402
    FallbackKind,
    FallbackReceiptError,
    build_fallback_receipt,
)


BOOT_ID = "01234567-89ab-cdef-0123-456789abcdef"


def _serial(*tail: str) -> str:
    return (
        f"OpenSBI v1.7 boot_id={BOOT_ID}\n"
        "U-Boot 2026.07\n"
        "=> \n"
        "ASTERINAS_BOOT_ID boot_id="
        f"{BOOT_ID}\n"
        "ASTERINAS_REBOOT_TIMER_READY\n"
        f"OpenSBI v1.7 boot_id={BOOT_ID}\n"
        "U-Boot 2026.07\n"
        + "".join(tail)
    )


class QemuFallbackReceiptTests(unittest.TestCase):
    def test_linux_reference_chain_is_proven_and_scoped(self) -> None:
        receipt = build_fallback_receipt(
            _serial("ASTERINAS_LINUX_REFERENCE_READY\n"),
            fallback_kind=FallbackKind.LINUX_REFERENCE,
        )
        self.assertTrue(receipt["passed"])
        self.assertEqual(receipt["observed_fallback"], "linux-reference")
        self.assertEqual(receipt["recovery_state"], "recovered-to-fallback")
        self.assertEqual(receipt["execution_scope"], "qemu-guest")
        self.assertEqual(receipt["hardware_state"], "not-accessed")
        self.assertEqual(receipt["watchdog_kind"], "software-recovery-timer")
        self.assertEqual(receipt["boot_id_state"], "observed")
        self.assertEqual(receipt["opensbi_epochs"], 2)
        self.assertEqual(receipt["uboot_epochs"], 2)

    def test_rockos_login_requires_post_recovery_marker(self) -> None:
        receipt = build_fallback_receipt(
            _serial("rockos-eswin login:\n"),
            fallback_kind=FallbackKind.ROCKOS_LOGIN,
        )
        self.assertTrue(receipt["passed"])
        self.assertEqual(receipt["observed_fallback"], "rockos-login")

    def test_initial_prompt_or_login_is_not_a_fallback(self) -> None:
        serial = (
            "OpenSBI v1.7\nU-Boot 2026.07\n=> \n"
            "rockos-eswin login:\n"
        )
        receipt = build_fallback_receipt(
            serial,
            fallback_kind=FallbackKind.ROCKOS_LOGIN,
        )
        self.assertFalse(receipt["passed"])
        self.assertEqual(receipt["observed_fallback"], "unknown")
        self.assertEqual(receipt["failure"], "recovery-marker-missing")
        self.assertEqual(receipt["watchdog_state"], "missing")

    def test_wrong_fallback_kind_fails_closed(self) -> None:
        receipt = build_fallback_receipt(
            _serial("ASTERINAS_LINUX_REFERENCE_READY\n"),
            fallback_kind=FallbackKind.ROCKOS_LOGIN,
        )
        self.assertFalse(receipt["passed"])
        self.assertEqual(receipt["observed_fallback"], "linux-reference")
        self.assertEqual(receipt["failure"], "fallback-kind-mismatch")

    def test_unknown_expected_kind_is_rejected(self) -> None:
        with self.assertRaises(FallbackReceiptError):
            build_fallback_receipt("", fallback_kind="rockos")


if __name__ == "__main__":
    unittest.main()
