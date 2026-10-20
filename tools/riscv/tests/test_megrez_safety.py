"""Tests for the Megrez host-side recovery latch."""

import json
import os
import tempfile
import unittest
from pathlib import Path

import megrez_safety as safety


BOOT_ID = "01234567-89ab-cdef-0123-456789abcdef"
RECEIPT_NONCE = "0123456789abcdef" * 2


def _receipt() -> dict[str, object]:
    return {
        "schema_version": 2,
        "observer": "rockos-sha256sum",
        "rockos_boot_id": BOOT_ID,
        "measurement_nonce": RECEIPT_NONCE,
        "rockos_recovered": True,
    }


class MegrezSafetyTests(unittest.TestCase):
    def test_missing_latch_allows_start(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(
                safety.assert_start_allowed(Path(directory) / "state.json")
            )

    def test_mark_required_writes_bounded_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            safety.mark_recovery_required(path, "automatic-recovery-not-observed")
            state = safety.read_state(path)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state["schema_version"], 1)
            self.assertEqual(state["status"], "recovery-required")
            self.assertEqual(state["phase"], "recovery")
            self.assertEqual(state["reason"], "automatic-recovery-not-observed")
            self.assertRegex(state["run_id"], r"^[0-9a-f]{32}$")

    def test_recovery_required_latch_blocks_without_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            safety.mark_recovery_required(path, "automatic-recovery-not-observed")
            with self.assertRaisesRegex(
                safety.SafetyLatchError, "physical reset receipt is required"
            ):
                safety.assert_start_allowed(path)

    def test_malformed_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"status":"recovery-required"}\n')
            with self.assertRaisesRegex(safety.SafetyLatchError, "state"):
                safety.assert_start_allowed(path)

    def test_stale_receipt_does_not_clear_latch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.json"
            receipt_path = root / "deployment-attestation.json"
            receipt_path.write_text(json.dumps(_receipt()) + "\n")
            safety.mark_recovery_required(state_path, "automatic-recovery-not-observed")
            state_path.touch()
            os.utime(
                receipt_path,
                ns=(state_path.stat().st_atime_ns, state_path.stat().st_mtime_ns - 1),
            )
            with self.assertRaisesRegex(safety.SafetyLatchError, "newer"):
                safety.assert_start_allowed(state_path, receipt_path)

    def test_valid_newer_receipt_clears_latch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.json"
            receipt_path = root / "deployment-attestation.json"
            safety.mark_recovery_required(state_path, "automatic-recovery-not-observed")
            receipt_path.write_text(json.dumps(_receipt()) + "\n")
            os.utime(
                receipt_path,
                ns=(
                    state_path.stat().st_atime_ns + 1,
                    state_path.stat().st_mtime_ns + 1_000_000,
                ),
            )
            result = safety.assert_start_allowed(state_path, receipt_path)
            self.assertEqual(result["rockos_boot_id"], BOOT_ID)
            self.assertEqual(safety.read_state(state_path)["status"], "cleared")
            self.assertIsNone(safety.assert_start_allowed(state_path))

    def test_invalid_receipt_fields_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.json"
            receipt_path = root / "deployment-attestation.json"
            safety.mark_recovery_required(state_path, "automatic-recovery-not-observed")
            receipt = _receipt()
            receipt["rockos_recovered"] = False
            receipt_path.write_text(json.dumps(receipt) + "\n")
            os.utime(
                receipt_path,
                ns=(
                    state_path.stat().st_atime_ns + 1,
                    state_path.stat().st_mtime_ns + 1_000_000,
                ),
            )
            with self.assertRaisesRegex(safety.SafetyLatchError, "receipt"):
                safety.assert_start_allowed(state_path, receipt_path)


if __name__ == "__main__":
    unittest.main()
