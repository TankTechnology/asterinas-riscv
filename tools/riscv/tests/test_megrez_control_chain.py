#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

import hashlib
import tempfile
import unittest
from pathlib import Path

from tools.riscv.megrez_control_chain import (
    ControlChain,
    ControlChainBudget,
    ControlChainError,
    ControlChainOutput,
    ControlReceipt,
    ControlStage,
    build_handshake_command,
    parse_handshake_frame,
    parse_recovery_marker,
    parse_transcript_frame,
)


OLD_BOOT = "11111111-1111-1111-1111-111111111111"
NEW_BOOT = "22222222-2222-2222-2222-222222222222"
NONCES = {
    stage: hashlib.sha256(stage.value.encode()).hexdigest()[:32]
    for stage in ControlStage
}


def frame(stage: ControlStage, nonce: str, boot_id: str, uid: int = 0) -> str:
    return (
        "ASTERINAS_CONTROL v=1 "
        f"stage={stage.value} nonce={nonce} uid={uid} "
        f"boot_id={boot_id} status={'pass' if uid == 0 else 'fail'}\n"
    )


class ControlChainTests(unittest.TestCase):
    def test_recovery_marker_rejects_stale_duplicate_and_invalid_identity(self):
        prefix = "ASTERINAS_DISPLAY_RECOVERY"
        nonce = "a" * 32
        valid = f"{prefix} nonce={nonce} uid=0 boot_id={NEW_BOOT}\n"
        self.assertEqual(parse_recovery_marker(valid, prefix=prefix, nonce=nonce).boot_id, NEW_BOOT)
        for payload in (
            valid + valid,
            valid.replace(nonce, "b" * 32),
            valid.replace("uid=0", "uid=1000"),
            valid.replace(NEW_BOOT, "invalid"),
            valid.rstrip() + " extra=1\n",
        ):
            with self.subTest(payload=payload), self.assertRaises(ControlChainError):
                parse_recovery_marker(payload, prefix=prefix, nonce=nonce)

    def test_packet_recovery_uses_the_same_root_contract(self):
        nonce = "c" * 32
        prefix = "ASTERINAS_SINGLE_PACKET_RECOVERY"
        with self.assertRaisesRegex(ControlChainError, "UID 0"):
            parse_recovery_marker(
                f"{prefix} nonce={nonce} uid=1000 boot_id={NEW_BOOT}\n",
                prefix=prefix, nonce=nonce,
            )

    def test_handshake_command_is_bounded_and_nonce_bound(self) -> None:
        nonce = "a" * 32
        command = build_handshake_command(ControlStage.SERIAL_BEFORE_REBOOT, nonce)
        self.assertLessEqual(len(command.encode()), 1024)
        self.assertIn(f"nonce={nonce}", command)
        self.assertIn("id -u", command)
        self.assertIn("/proc/sys/kernel/random/boot_id", command)

    def test_parser_rejects_unknown_or_duplicate_fields(self) -> None:
        valid = frame(ControlStage.SERIAL_BEFORE_REBOOT, NONCES[ControlStage.SERIAL_BEFORE_REBOOT], OLD_BOOT)
        receipt = parse_handshake_frame(valid)
        self.assertEqual(receipt.boot_id, OLD_BOOT)
        with self.assertRaises(ControlChainError):
            parse_handshake_frame(valid.rstrip("\n") + " extra=1\n")
        with self.assertRaises(ControlChainError):
            parse_handshake_frame(valid.rstrip("\n") + " uid=0\n")

    def test_transcript_parser_allows_logs_but_rejects_stale_or_duplicate_frames(self) -> None:
        stage = ControlStage.SERIAL_BEFORE_REBOOT
        nonce = NONCES[stage]
        expected = frame(stage, nonce, OLD_BOOT)
        receipt = parse_transcript_frame(
            b"boot log\r\n" + expected.replace("\n", "\r\n").encode() + b"shell echo\r\n",
            stage=stage,
            nonce=nonce,
        )
        self.assertEqual(receipt.boot_id, OLD_BOOT)
        with self.assertRaises(ControlChainError):
            parse_transcript_frame(
                expected.encode() + expected.encode(), stage=stage, nonce=nonce
            )
        with self.assertRaises(ControlChainError):
            parse_transcript_frame(
                frame(stage, "b" * 32, OLD_BOOT), stage=stage, nonce=nonce
            )

    def test_state_machine_requires_new_boot_after_reboot_and_reopen(self) -> None:
        chain = ControlChain()
        chain.record(
            parse_handshake_frame(
                frame(
                    ControlStage.SERIAL_BEFORE_REBOOT,
                    NONCES[ControlStage.SERIAL_BEFORE_REBOOT],
                    OLD_BOOT,
                )
            )
        )
        chain.mark_reboot_sent()
        with self.assertRaises(ControlChainError):
            chain.record(
                parse_handshake_frame(
                    frame(ControlStage.SERIAL_AFTER_REBOOT, NONCES[ControlStage.SERIAL_AFTER_REBOOT], OLD_BOOT)
                )
            )
        chain.record(
            parse_handshake_frame(
                frame(ControlStage.SERIAL_AFTER_REBOOT, NONCES[ControlStage.SERIAL_AFTER_REBOOT], NEW_BOOT)
            )
        )
        chain.record(
            parse_handshake_frame(
                frame(ControlStage.SERIAL_REOPEN, NONCES[ControlStage.SERIAL_REOPEN], NEW_BOOT)
            )
        )
        chain.record(
            parse_handshake_frame(
                frame(ControlStage.SSH_AFTER_REBOOT, NONCES[ControlStage.SSH_AFTER_REBOOT], NEW_BOOT)
            )
        )
        evidence = chain.finish()
        self.assertEqual(evidence["old_boot_id"], OLD_BOOT)
        self.assertEqual(evidence["new_boot_id"], NEW_BOOT)
        self.assertEqual(evidence["stages"], [stage.value for stage in ControlStage])
        self.assertNotIn(NONCES[ControlStage.SERIAL_REOPEN], str(evidence))

    def test_state_machine_does_not_trust_manually_constructed_non_root_receipt(self) -> None:
        chain = ControlChain()
        with self.assertRaises(ControlChainError):
            chain.record(
                ControlReceipt(
                    ControlStage.SERIAL_BEFORE_REBOOT,
                    NONCES[ControlStage.SERIAL_BEFORE_REBOOT],
                    1000,
                    OLD_BOOT,
                )
            )

    def test_budget_requires_watchdog_headroom(self) -> None:
        budget = ControlChainBudget(boot_timeout=120, drain_timeout=2, headroom=30)
        self.assertEqual(budget.minimum_reboot_after, 152)
        with self.assertRaises(ControlChainError):
            budget.validate_reboot_after(151)
        budget.validate_reboot_after(152)

    def test_output_preflight_preserves_existing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            output = repository / "target" / "megrez-control-chain" / "run"
            output.mkdir(parents=True)
            (output / "result.json").write_text("old", encoding="utf-8")
            with ControlChainOutput(output, repository=repository) as run:
                with self.assertRaises(ControlChainError):
                    run.preflight()
                self.assertEqual((output / "result.json").read_text(), "old")

    def test_output_preflight_rejects_parent_traversal_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            output = repository / "target" / ".." / "escape"
            with self.assertRaises(ControlChainError):
                with ControlChainOutput(output, repository=repository):
                    pass
            self.assertFalse((repository / "escape").exists())

    def test_output_preflight_locks_a_fresh_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            output = repository / "target" / "megrez-control-chain" / "run"
            with ControlChainOutput(output, repository=repository) as run:
                run.preflight()
                run.write("serial.log", b"serial\n")
                self.assertEqual((output / "serial.log").read_bytes(), b"serial\n")


if __name__ == "__main__":
    unittest.main()
