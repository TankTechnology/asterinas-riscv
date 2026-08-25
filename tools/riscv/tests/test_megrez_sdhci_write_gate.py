# SPDX-License-Identifier: MPL-2.0

import unittest

from tools.riscv.megrez_sdhci_write_gate import (
    EXPECTED_GPT_SHA256,
    P2_NR_SECTORS,
    P2_START_LBA,
    TEST_PARTITION_SECTOR,
    GateError,
    Preflight,
    exercise,
)


class FakeSectorOperations:
    def __init__(self, *, fail_at: str | None = None):
        self.original = b"o" * 512
        self.current = self.original
        self.fail_at = fail_at
        self.calls: list[str] = []

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_at == name:
            raise GateError(f"injected-{name}")

    def preflight(self) -> Preflight:
        self._record("preflight")
        return Preflight(P2_START_LBA, P2_NR_SECTORS, EXPECTED_GPT_SHA256, True)

    def read_test_sector(self) -> bytes:
        self._record("read")
        return self.current

    def write_test_sector(self, payload: bytes) -> None:
        self._record("write")
        self.current = payload

    def sync(self) -> None:
        self._record("sync")


class MegrezSdhciWriteGateTests(unittest.TestCase):
    def test_uses_last_sector_inside_exact_partition_two(self):
        self.assertEqual(TEST_PARTITION_SECTOR, P2_NR_SECTORS - 1)
        self.assertEqual(
            P2_START_LBA + TEST_PARTITION_SECTOR,
            P2_START_LBA + P2_NR_SECTORS - 1,
        )

    def test_success_restores_original_sector(self):
        operations = FakeSectorOperations()
        nonce = b"n" * 512

        result = exercise(operations, nonce)

        self.assertTrue(result.passed)
        self.assertEqual(operations.current, operations.original)
        self.assertNotEqual(result.original_sha256, result.nonce_sha256)
        self.assertEqual(result.original_sha256, result.restored_sha256)
        self.assertEqual(
            operations.calls,
            ["preflight", "read", "write", "sync", "read", "write", "sync", "read"],
        )

    def test_rejects_contract_mismatch_before_write(self):
        mismatches = [
            Preflight(P2_START_LBA + 1, P2_NR_SECTORS, EXPECTED_GPT_SHA256, True),
            Preflight(P2_START_LBA, P2_NR_SECTORS - 1, EXPECTED_GPT_SHA256, True),
            Preflight(P2_START_LBA, P2_NR_SECTORS, "0" * 64, True),
            Preflight(P2_START_LBA, P2_NR_SECTORS, EXPECTED_GPT_SHA256, False),
        ]
        for preflight in mismatches:
            with self.subTest(preflight=preflight):
                operations = FakeSectorOperations()
                operations.preflight = lambda: preflight
                with self.assertRaises(GateError):
                    exercise(operations, b"n" * 512)
                self.assertNotIn("write", operations.calls)
                self.assertEqual(operations.current, operations.original)

    def test_rejects_bad_nonce_before_preflight(self):
        for nonce in [b"", b"n" * 511, b"n" * 513, b"o" * 512]:
            with self.subTest(length=len(nonce)):
                operations = FakeSectorOperations()
                if nonce == operations.original:
                    with self.assertRaises(GateError):
                        exercise(operations, nonce)
                    self.assertNotIn("write", operations.calls)
                else:
                    with self.assertRaises(GateError):
                        exercise(operations, nonce)
                    self.assertEqual(operations.calls, [])

    def test_write_or_verification_failure_restores_original(self):
        for fail_at in ["sync", "read"]:
            with self.subTest(fail_at=fail_at):
                operations = FakeSectorOperations(fail_at=fail_at)
                with self.assertRaises(GateError):
                    exercise(operations, b"n" * 512)
                self.assertEqual(operations.current, operations.original)

    def test_reports_restore_failure_as_terminal(self):
        class RestoreFailureOperations(FakeSectorOperations):
            def write_test_sector(self, payload: bytes) -> None:
                if payload == self.original:
                    raise GateError("restore-write-failed")
                super().write_test_sector(payload)

            def read_test_sector(self) -> bytes:
                payload = super().read_test_sector()
                if payload != self.original:
                    raise GateError("verification-read-failed")
                return payload

        operations = RestoreFailureOperations()
        with self.assertRaisesRegex(GateError, "restore-write-failed"):
            exercise(operations, b"n" * 512)
        self.assertNotEqual(operations.current, operations.original)


if __name__ == "__main__":
    unittest.main()
