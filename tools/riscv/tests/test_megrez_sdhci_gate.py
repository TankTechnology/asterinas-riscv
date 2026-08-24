# SPDX-License-Identifier: MPL-2.0

import json
import tempfile
import unittest
from pathlib import Path

from tools.riscv.megrez_sdhci_gate import MAX_TRANSCRIPT_BYTES, classify, publish


CONTROLLER = "[mmc] controller 0x50460000 irq=81 read-only"
CARD = "[mmc] SDHC rca=43690 sectors=249737216 sector0=55aa"
BLOCK = "[mmc] mmcblk0 registered read-only"
HASH = "[mmc] partition-table sha256=" + "a" * 64


class MegrezSdhciGateTests(unittest.TestCase):
    def transcript(self, *lines: str) -> bytes:
        return ("\n".join(lines) + "\n").encode()

    def test_accepts_one_complete_ordered_read_only_run(self):
        result = classify(self.transcript(CONTROLLER, CARD, BLOCK, HASH))
        self.assertTrue(result.passed)
        self.assertEqual(result.sectors, 249737216)
        self.assertEqual(result.partition_sha256, "a" * 64)

    def test_rejects_missing_duplicate_or_out_of_order_markers(self):
        cases = {
            "missing": self.transcript(CONTROLLER, CARD, BLOCK),
            "duplicate": self.transcript(CONTROLLER, CARD, CARD, BLOCK, HASH),
            "out-of-order": self.transcript(CONTROLLER, BLOCK, CARD, HASH),
        }
        for name, transcript in cases.items():
            with self.subTest(name=name):
                self.assertFalse(classify(transcript).passed)

    def test_rejects_fatal_or_write_enabled_evidence(self):
        for line in [
            "Uncaught panic: block read failed",
            "fatal exception",
            "[mmc] probe failed at host-handoff-or-card",
            "[mmc] mmcblk0 write-enabled",
        ]:
            with self.subTest(line=line):
                result = classify(self.transcript(CONTROLLER, CARD, BLOCK, HASH, line))
                self.assertFalse(result.passed)

    def test_rejects_invalid_capacity_hash_and_oversized_transcript(self):
        self.assertFalse(
            classify(
                self.transcript(CONTROLLER, "[mmc] SDHC rca=1 sectors=0", BLOCK, HASH)
            ).passed
        )
        self.assertFalse(
            classify(
                self.transcript(
                    CONTROLLER, CARD, BLOCK, "[mmc] partition-table sha256=xyz"
                )
            ).passed
        )
        self.assertFalse(classify(b"x" * (MAX_TRANSCRIPT_BYTES + 1)).passed)

    def test_publishes_complete_log_and_atomic_json_result(self):
        transcript = self.transcript(CONTROLLER, CARD, BLOCK, HASH)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "evidence"
            result = publish(transcript, output)
            self.assertTrue(result.passed)
            self.assertEqual((output / "serial.log").read_bytes(), transcript)
            payload = json.loads((output / "result.json").read_text())
            self.assertTrue(payload["passed"])
            self.assertEqual(payload["sectors"], 249737216)
            self.assertEqual(payload["partition_sha256"], "a" * 64)
            self.assertEqual(list(output.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
